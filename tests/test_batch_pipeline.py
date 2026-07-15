#!/usr/bin/env python3

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import sanborn_batch
from sanborn_osm import import_osm, web_mercator


GRID_OSM = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="sanborn-batch-test">
  <node id="1" lat="33.7510" lon="-84.3812" />
  <node id="2" lat="33.7510" lon="-84.3800" />
  <node id="3" lat="33.7500" lon="-84.3812" />
  <node id="4" lat="33.7500" lon="-84.3800" />
  <way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="primary"/><tag k="name" v="Auburn Avenue"/></way>
  <way id="11"><nd ref="3"/><nd ref="4"/><tag k="highway" v="primary"/><tag k="name" v="Edgewood Avenue"/></way>
  <way id="12"><nd ref="1"/><nd ref="3"/><tag k="highway" v="secondary"/><tag k="name" v="Jesse Hill Junior Drive"/></way>
  <way id="13"><nd ref="2"/><nd ref="4"/><tag k="highway" v="secondary"/><tag k="name" v="Fort Street"/></way>
</osm>
"""


def queue(db, tile, status="queued", **fields):
    values = {
        "tile": tile,
        "status": status,
        "updated_utc": sanborn_batch.utc_now(),
        **fields,
    }
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    db.execute(
        f"INSERT INTO tiles({columns}) VALUES ({placeholders})",
        list(values.values()),
    )
    db.commit()


def work_args(database, tiles=None, *, osm_db=None, aliases=None, index_json=None):
    return argparse.Namespace(
        database=database,
        tiles=tiles or [],
        limit=None,
        delay=0,
        osm_db=osm_db or database.parent / "osm.sqlite3",
        aliases=aliases or database.parent / "aliases.json",
        index_json=index_json or database.parent / "missing-index-seeds.json",
    )


class BatchPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.database = self.folder / "queue.sqlite3"

    def tearDown(self):
        self.temporary.cleanup()

    def test_legacy_database_is_migrated_without_losing_queue(self):
        db = sqlite3.connect(self.database)
        db.executescript(
            """
            CREATE TABLE tiles (
                tile INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'queued',
                updated_utc TEXT NOT NULL
            );
            INSERT INTO tiles(tile, status, updated_utc)
            VALUES (154, 'queued', '2026-07-15T00:00:00+00:00');
            """
        )
        db.commit()
        db.close()

        with sanborn_batch.connect(self.database) as migrated:
            columns = {
                row["name"] for row in migrated.execute("PRAGMA table_info(tiles)")
            }
            version = migrated.execute("PRAGMA user_version").fetchone()[0]
            busy_timeout = migrated.execute("PRAGMA busy_timeout").fetchone()[0]
            history = migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='proposal_history'"
            ).fetchone()
            saved = migrated.execute("SELECT status FROM tiles WHERE tile=154").fetchone()

        self.assertEqual(version, sanborn_batch.DATABASE_SCHEMA_VERSION)
        self.assertEqual(busy_timeout, 30000)
        self.assertTrue(
            {
                "spatial_ocr_path",
                "proposal_path",
                "proposal_evidence_json",
                "proposal_corrections_json",
                "proposal_rejections_json",
                "target_seed_x",
                "target_seed_y",
                "target_seed_json",
                "target_seed_provenance_json",
                "failure_stage",
            }.issubset(columns)
        )
        self.assertIsNotNone(history)
        self.assertEqual(saved["status"], "queued")

    def test_state_machine_rejects_shortcuts_and_requires_explicit_reopen(self):
        with sanborn_batch.connect(self.database) as db:
            queue(db, 1)
            self.assertTrue(sanborn_batch.transition_tile(db, 1, "downloading"))
            with self.assertRaisesRegex(RuntimeError, "Illegal tile 1 state change"):
                sanborn_batch.transition_tile(db, 1, "approved")
            sanborn_batch.reopen_tile(db, 1, "queued", "Interrupted download inspected")
            self.assertEqual(
                db.execute("SELECT status FROM tiles WHERE tile=1").fetchone()["status"],
                "queued",
            )
            sanborn_batch.transition_tile(db, 1, "downloading")
            sanborn_batch.transition_tile(db, 1, "review-ready")
            sanborn_batch.transition_tile(db, 1, "awaiting-approval")
            spatial = self.folder / "tile-1-spatial.json"
            spatial.write_text("{}", encoding="utf-8")
            sanborn_batch.update_tile(
                db,
                1,
                source_path=str(self.folder / "tile-1.jp2"),
                source_sha256="source-hash",
                spatial_ocr_path=str(spatial),
                printed_number_seen=1,
                points_path=str(self.folder / "bad.points"),
                review_dir=str(self.folder / "bad-review"),
            )
            sanborn_batch.reopen_tile(
                db, 1, "review-ready", "Contact sheet showed a misplaced crosshair"
            )
            reopened = db.execute("SELECT * FROM tiles WHERE tile=1").fetchone()
            self.assertEqual(reopened["status"], "review-ready")
            self.assertIsNone(reopened["points_path"])
            self.assertIsNone(reopened["review_dir"])
            sanborn_batch.transition_tile(db, 1, "awaiting-approval")
            sanborn_batch.transition_tile(db, 1, "approved")
            sanborn_batch.reopen_tile(
                db, 1, "review-ready", "Approval withdrawn before final output"
            )
            sanborn_batch.transition_tile(db, 1, "awaiting-approval")
            sanborn_batch.transition_tile(db, 1, "approved")
            sanborn_batch.transition_tile(db, 1, "verified")
            sanborn_batch.reopen_tile(
                db, 1, "queued", "Final evidence was inspected and needs rebuilding"
            )
            self.assertEqual(
                db.execute("SELECT status FROM tiles WHERE tile=1").fetchone()["status"],
                "queued",
            )

    def test_worker_resume_is_idempotent_after_review_handoff(self):
        with sanborn_batch.connect(self.database) as db:
            queue(db, 1)

        def fake_prepare(db, tile):
            sanborn_batch.transition_tile(db, tile, "downloading")
            sanborn_batch.transition_tile(
                db,
                tile,
                "review-ready",
                printed_number_seen=1,
            )
            db.commit()
            return "review-ready"

        proposal_runs = []

        def fake_propose(db, tile, osm_db, aliases, **kwargs):
            if db.execute("SELECT status FROM tiles WHERE tile=?", (tile,)).fetchone()["status"] == "needs-chatgpt-review":
                return "needs-chatgpt-review"
            proposal_runs.append(tile)
            sanborn_batch.transition_tile(db, tile, "proposing")
            sanborn_batch.transition_tile(db, tile, "needs-chatgpt-review")
            db.commit()
            return "needs-chatgpt-review"

        args = work_args(self.database, ["1"])
        with mock.patch.object(sanborn_batch, "prepare_one", side_effect=fake_prepare) as prepare, mock.patch.object(
            sanborn_batch, "propose_one", side_effect=fake_propose
        ) as propose:
            self.assertEqual(sanborn_batch.cmd_work(args), 0)
            self.assertEqual(sanborn_batch.cmd_work(args), 0)
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual(propose.call_count, 2)
            self.assertEqual(proposal_runs, [1])

        with sanborn_batch.connect(self.database) as db:
            self.assertEqual(
                db.execute("SELECT status FROM tiles WHERE tile=1").fetchone()["status"],
                "needs-chatgpt-review",
            )

    def test_phase_one_text_ocr_is_upgraded_without_another_download(self):
        source = self.folder / "existing.jp2"
        source.write_bytes(b"already downloaded scan")
        old_ocr = self.folder / "old.txt"
        old_ocr.write_text("AUBURN", encoding="utf-8")
        preview = self.folder / "preview.png"
        Image.new("RGB", (20, 20), "white").save(preview)
        spatial = self.folder / "spatial.json"
        spatial.write_text("{}", encoding="utf-8")
        with sanborn_batch.connect(self.database) as db:
            queue(
                db,
                8,
                "review-ready",
                source_path=str(source),
                source_sha256=sanborn_batch.sha256(source),
                ocr_path=str(old_ocr),
                printed_number_seen=1,
            )
            row = db.execute("SELECT * FROM tiles WHERE tile=8").fetchone()
            with mock.patch.object(
                sanborn_batch,
                "create_preview",
                return_value=(preview, spatial, True),
            ) as create:
                result = sanborn_batch._ensure_spatial_ocr(db, row)
            updated = db.execute("SELECT * FROM tiles WHERE tile=8").fetchone()
        self.assertEqual(result, spatial)
        self.assertEqual(updated["spatial_ocr_path"], str(spatial))
        self.assertEqual(updated["ocr_path"], str(spatial))
        create.assert_called_once_with(8, source)

    def test_one_tile_failure_does_not_stop_the_range(self):
        with sanborn_batch.connect(self.database) as db:
            for tile in (1, 2, 3):
                queue(db, tile)

        visited = []

        def fake_prepare(db, tile):
            visited.append(tile)
            sanborn_batch.transition_tile(db, tile, "downloading")
            if tile == 2:
                raise RuntimeError("synthetic damaged scan")
            sanborn_batch.transition_tile(
                db,
                tile,
                "number-check-required",
                printed_number_seen=0,
            )
            db.commit()
            return "number-check-required"

        with mock.patch.object(sanborn_batch, "prepare_one", side_effect=fake_prepare):
            self.assertEqual(sanborn_batch.cmd_work(work_args(self.database)), 1)

        self.assertEqual(visited, [1, 2, 3])
        with sanborn_batch.connect(self.database) as db:
            statuses = {
                row["tile"]: row["status"]
                for row in db.execute("SELECT tile, status FROM tiles ORDER BY tile")
            }
            failure = db.execute(
                "SELECT failure_stage, error FROM tiles WHERE tile=2"
            ).fetchone()
        self.assertEqual(
            statuses,
            {1: "number-check-required", 2: "failed", 3: "number-check-required"},
        )
        self.assertEqual(failure["failure_stage"], "prepare")
        self.assertIn("damaged scan", failure["error"])

    def test_proposal_correction_and_rejection_are_persistent_but_never_approval(self):
        proposal = self.folder / "proposal.json"
        proposal.write_text("{}", encoding="utf-8")
        proposal_digest = sanborn_batch.sha256(proposal)
        with sanborn_batch.connect(self.database) as db:
            queue(
                db,
                9,
                "needs-chatgpt-review",
                proposal_path=str(proposal),
                proposal_sha256=proposal_digest,
            )
            db.execute(
                """
                INSERT INTO proposal_history(
                    tile, created_utc, proposal_path, proposal_sha256, input_sha256,
                    evidence_json, corrections_json, rejections_json
                ) VALUES (9, ?, ?, ?, 'inputs', '{}', '[]', '[]')
                """,
                (sanborn_batch.utc_now(), str(proposal), proposal_digest),
            )
            db.commit()

        args = argparse.Namespace(
            database=self.database,
            tile=9,
            correction=["Move control 2 to the visible curb intersection"],
            reject="Houston street axis follows the label, not the diagonal road",
        )
        self.assertEqual(sanborn_batch.cmd_review_proposal(args), 0)
        with sanborn_batch.connect(self.database) as db:
            row = db.execute("SELECT * FROM tiles WHERE tile=9").fetchone()
            history = db.execute(
                "SELECT * FROM proposal_history WHERE tile=9 ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["status"], "proposal-rejected")
        self.assertIn("curb intersection", row["proposal_corrections_json"])
        self.assertIn("diagonal road", row["proposal_rejections_json"])
        self.assertEqual(
            row["proposal_corrections_json"], history["corrections_json"]
        )
        self.assertIsNone(row["points_path"])
        self.assertNotEqual(row["status"], "approved")

    def test_local_osm_import_hook_never_contacts_the_network(self):
        source = self.folder / "extract.osm"
        source.write_text(GRID_OSM, encoding="utf-8")
        destination = self.folder / "streets.sqlite3"
        args = argparse.Namespace(
            source=source,
            osm_db=destination,
            bbox=[33.7, -84.4, 33.8, -84.3],
            replace=True,
        )
        with mock.patch.object(sanborn_batch.subprocess, "run") as run:
            self.assertEqual(sanborn_batch.cmd_osm_import(args), 0)
        command = run.call_args.args[0]
        self.assertIn("sanborn_osm.py", command[1])
        self.assertEqual(command[2], "import")
        self.assertEqual(command[3], str(source))
        self.assertIn(str(destination), command)
        self.assertIn("--replace", command)
        self.assertNotIn("refresh", command)

    def test_index_seed_is_stored_with_provenance_and_builds_final_gate_flags(self):
        seeds = self.folder / "tile-location-seeds.json"
        index_raster = self.folder / "index-evidence.tif"
        index_raster.write_bytes(b"stable index evidence")
        index_preview = self.folder / "index-preview.png"
        index_preview.write_bytes(b"stable index preview")
        seeds.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "created_utc": "2026-07-15T04:00:00+00:00",
                    "purpose": "independent wrong-neighborhood safeguard",
                    "recognizer": "synthetic fixture",
                    "index": {
                        "path": str(index_raster),
                        "sha256": sanborn_batch.sha256(index_raster),
                        "width": 1000,
                        "height": 1000,
                        "crs": "EPSG:3857",
                        "geotransform": [1, 2, 0, 3, 0, -2],
                    },
                    "preview": {
                        "path": str(index_preview),
                        "sha256": sanborn_batch.sha256(index_preview),
                        "bytes": index_preview.stat().st_size,
                        "width": 1000,
                        "height": 1000,
                    },
                    "seeds": [
                        {
                            "tile": 154,
                            "map_x": -9393040.25,
                            "map_y": 3996150.75,
                            "crs": "EPSG:3857",
                            "suggested_max_distance": 250.0,
                            "quality": "high",
                            "ambiguous": False,
                            "support": 4,
                        },
                        {
                            "tile": 155,
                            "map_x": -9390000.0,
                            "map_y": 3990000.0,
                            "crs": "EPSG:3857",
                            "suggested_max_distance": 250.0,
                            "quality": "review",
                            "ambiguous": True,
                            "support": 1,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with sanborn_batch.connect(self.database) as db:
            for tile in (154, 155, 156):
                queue(db, tile)
            selected = sanborn_batch.store_tile_seed(db, 154, seeds)
            ambiguous = sanborn_batch.store_tile_seed(db, 155, seeds)
            missing = sanborn_batch.store_tile_seed(db, 156, seeds)
            selected_row = db.execute("SELECT * FROM tiles WHERE tile=154").fetchone()
            ambiguous_row = db.execute("SELECT * FROM tiles WHERE tile=155").fetchone()

        self.assertEqual(selected["status"], "selected")
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertEqual(missing["status"], "missing-seed")
        self.assertEqual(selected_row["target_seed_x"], -9393040.25)
        self.assertIn(str(index_raster), selected_row["target_seed_provenance_json"])
        self.assertIsNone(ambiguous_row["target_seed_x"])
        self.assertEqual(ambiguous_row["target_seed_ambiguous"], 1)
        self.assertEqual(
            sanborn_batch._target_seed_command_args(selected_row),
            [
                "--expected-target-bbox",
                "-9393290.25",
                "3995900.75",
                "-9392790.25",
                "3996400.75",
                "--expected-target-seed",
                "-9393040.25",
                "3996150.75",
                "--max-target-seed-distance",
                "250.0",
            ],
        )
        with self.assertRaisesRegex(RuntimeError, "no unique independent"):
            sanborn_batch._target_seed_command_args(ambiguous_row)
        evidence = sanborn_batch._proposal_evidence(
            {"source": {}, "control_candidates": [], "ranked_triplets": []},
            selected,
        )
        self.assertEqual(evidence["target_seed"]["status"], "selected")
        self.assertEqual(evidence["target_seed"]["provenance"]["sha256"], sanborn_batch.sha256(seeds))

        # A later seed-file revision invalidates a proposal instead of allowing
        # review to continue against stale geography.
        with sanborn_batch.connect(self.database) as db:
            sanborn_batch.transition_tile(db, 154, "downloading")
            sanborn_batch.transition_tile(db, 154, "review-ready")
            sanborn_batch.transition_tile(db, 154, "proposing")
            sanborn_batch.transition_tile(
                db,
                154,
                "needs-chatgpt-review",
                proposal_path=str(self.folder / "proposal.json"),
            )
            db.commit()
        revised = json.loads(seeds.read_text(encoding="utf-8"))
        revised["seeds"][0]["map_x"] += 125.0
        seeds.write_text(json.dumps(revised), encoding="utf-8")
        with sanborn_batch.connect(self.database) as db:
            sanborn_batch.store_tile_seed(db, 154, seeds)
            refreshed = db.execute("SELECT * FROM tiles WHERE tile=154").fetchone()
        self.assertEqual(refreshed["status"], "proposal-stale")
        self.assertEqual(refreshed["target_seed_x"], -9392915.25)

    def test_synthetic_local_worker_reaches_review_without_approval(self):
        source = self.folder / "synthetic.jp2"
        source.write_bytes(b"source identity only")
        source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        ocr = self.folder / "spatial.json"
        preview = self.folder / "preview.png"
        Image.new("RGB", (100, 100), "white").save(preview)
        preview_digest = sanborn_batch.sha256(preview)
        labels = [
            ("Auburn", "AUBURN", 500, 200, "horizontal"),
            ("Edgewood", "EDGEWOOD", 500, 800, "horizontal"),
            ("Butler", "BUTLER", 200, 500, "vertical"),
            ("Fort", "FORT", 800, 500, "vertical"),
        ]
        ocr.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "expected_tile": 154,
                    "printed_tile_number_method": "apple-vision-title-region",
                    "printed_tile_number_seen": True,
                    "printed_tile_number_verification": {
                        "method": "apple-vision-title-region",
                        "status": "verified",
                        "provenance": {"preview_sha256": preview_digest},
                    },
                    "source": {
                        "path": str(source),
                        "sha256": source_digest,
                        "width": 1000,
                        "height": 1000,
                    },
                    "preview": {
                        "path": str(preview),
                        "sha256": preview_digest,
                        "bytes": preview.stat().st_size,
                        "width": 100,
                        "height": 100,
                    },
                    "street_labels": [
                        {
                            "text": text,
                            "name": text,
                            "normalized": normalized,
                            "source_x": x,
                            "source_y": y,
                            "orientation": orientation,
                            "confidence": 95,
                            "strength": "weak-token",
                        }
                        for text, normalized, x, y, orientation in labels
                    ],
                    "text_lines": [],
                }
            ),
            encoding="utf-8",
        )
        osm_source = self.folder / "grid.osm"
        osm_database = self.folder / "grid.sqlite3"
        osm_source.write_text(GRID_OSM, encoding="utf-8")
        import_osm(osm_source, osm_database)
        aliases = self.folder / "aliases.json"
        aliases.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "aliases": [
                        {
                            "historic": "Butler Street",
                            "modern": "Jesse Hill Junior Drive",
                            "evidence": "Reviewed synthetic fixture",
                            "status": "approved",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        seed_x, seed_y = web_mercator(-84.3806, 33.7505)
        index_json = self.folder / "tile-seeds.json"
        index_raster = self.folder / "proposal-index-evidence.tif"
        index_raster.write_bytes(b"stable proposal index evidence")
        index_preview = self.folder / "proposal-index-preview.png"
        index_preview.write_bytes(b"stable proposal index preview")
        index_json.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "created_utc": "2026-07-15T04:00:00+00:00",
                    "purpose": "synthetic independent location safeguard",
                    "recognizer": "test fixture",
                    "index": {
                        "path": str(index_raster),
                        "sha256": sanborn_batch.sha256(index_raster),
                        "width": 1000,
                        "height": 1000,
                        "geotransform": [-9394000, 1, 0, 3997000, 0, -1],
                        "crs": "EPSG:3857",
                    },
                    "preview": {
                        "path": str(index_preview),
                        "sha256": sanborn_batch.sha256(index_preview),
                        "bytes": index_preview.stat().st_size,
                        "width": 1000,
                        "height": 1000,
                    },
                    "seeds": [
                        {
                            "tile": 154,
                            "map_x": seed_x,
                            "map_y": seed_y,
                            "crs": "EPSG:3857",
                            "suggested_max_distance": 250.0,
                            "quality": "high",
                            "ambiguous": False,
                            "support": 4,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with sanborn_batch.connect(self.database) as db:
            queue(
                db,
                154,
                "review-ready",
                source_path=str(source),
                source_sha256=source_digest,
                width=1000,
                height=1000,
                spatial_ocr_path=str(ocr),
                spatial_ocr_sha256=sanborn_batch.sha256(ocr),
                printed_number_seen=1,
            )

        args = work_args(
            self.database,
            ["154"],
            osm_db=osm_database,
            aliases=aliases,
            index_json=index_json,
        )
        with mock.patch.object(sanborn_batch, "BATCH_DIR", self.folder / "batch"):
            self.assertEqual(sanborn_batch.cmd_work(args), 0)

        with sanborn_batch.connect(self.database) as db:
            row = db.execute("SELECT * FROM tiles WHERE tile=154").fetchone()
            history_count = db.execute(
                "SELECT COUNT(*) FROM proposal_history WHERE tile=154"
            ).fetchone()[0]
        self.assertEqual(row["status"], "needs-chatgpt-review")
        self.assertTrue(Path(row["proposal_path"]).is_file())
        self.assertGreater(len(json.loads(row["proposal_evidence_json"])["ranked_triplets"]), 0)
        proposal_record = json.loads(Path(row["proposal_path"]).read_text(encoding="utf-8"))
        proposal_evidence = json.loads(row["proposal_evidence_json"])
        self.assertEqual(proposal_record["target_seed"]["status"], "selected")
        self.assertEqual(proposal_record["expected_target_seed"], [seed_x, seed_y])
        self.assertEqual(proposal_record["max_seed_distance"], 250.0)
        self.assertEqual(proposal_evidence["target_seed"]["status"], "selected")
        self.assertAlmostEqual(row["target_seed_x"], seed_x)
        self.assertEqual(history_count, 1)
        self.assertIsNone(row["points_path"])
        self.assertIsNone(row["review_dir"])
        self.assertIsNone(row["output_path"])


if __name__ == "__main__":
    unittest.main()
