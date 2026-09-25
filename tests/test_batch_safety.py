#!/usr/bin/env python3

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import sanborn_batch
import sanborn_controls


def queue(db: sqlite3.Connection, tile: int, status: str = "queued", **fields) -> None:
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


class BatchSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.database = self.folder / "queue.sqlite3"
        self.locks = self.folder / "locks"

    def tearDown(self):
        self.temporary.cleanup()

    def _make_spatial_record(
        self,
        path: Path,
        source: Path,
        preview: Path,
        *,
        schema_version: int,
    ) -> dict:
        with Image.open(preview) as image:
            preview_width, preview_height = image.size
            image.verify()
        preview_digest = sanborn_batch.sha256(preview)
        record = {
            "schema_version": schema_version,
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
                "sha256": sanborn_batch.sha256(source),
                "width": 100,
                "height": 100,
            },
            "preview": {
                "path": str(preview),
                "sha256": preview_digest,
                "bytes": preview.stat().st_size,
                "width": preview_width,
                "height": preview_height,
            },
            "street_labels": [],
            "text_lines": [],
        }
        path.write_text(json.dumps(record), encoding="utf-8")
        return record

    def _make_seed_index(self, tile: int) -> Path:
        path = self.folder / f"seed-{tile}.json"
        index_raster = self.folder / "index-evidence.tif"
        if not index_raster.exists():
            index_raster.write_bytes(b"stable georeferenced index evidence")
        index_preview = self.folder / "index-preview.png"
        if not index_preview.exists():
            index_preview.write_bytes(b"stable index preview evidence")
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "created_utc": "2026-07-15T00:00:00+00:00",
                    "purpose": "test independent location safeguard",
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
                            "tile": tile,
                            "map_x": -9_393_000.0,
                            "map_y": 3_996_000.0,
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
        return path

    def _final_fixture(self, tile: int = 154, *, with_seed: bool = True):
        source = self.folder / f"tile-{tile}.jp2"
        points = self.folder / f"tile-{tile}.points"
        output = self.folder / f"tile-{tile}.tif"
        ledger = self.folder / f"tile-{tile}.georef.json"
        protected = self.folder / "protected.qgz"
        source.write_bytes(b"source scan")
        points.write_text(
            "#CRS: EPSG:3857\n"
            "mapX,mapY,sourceX,sourceY,enable\n"
            "-9393100,3996100,10,-10,1\n"
            "-9392900,3996100,90,-10,1\n"
            "-9393100,3995900,10,-90,1\n",
            encoding="utf-8",
        )
        output.write_bytes(b"synthetic lossless rgba geotiff identity")
        protected.write_bytes(b"protected project")
        fields = {
            "source_path": str(source),
            "source_sha256": sanborn_batch.sha256(source),
            "points_path": str(points),
            "target_seed_ambiguous": 0,
        }
        seed_x = -9_393_000.0
        seed_y = 3_996_000.0
        distance = 250.0
        if with_seed:
            index_path = self._make_seed_index(tile)
            seed_context = sanborn_batch.tile_seed_context(
                index_path, tile, missing_ok=False
            )
            fields.update(
                target_seed_x=seed_x,
                target_seed_y=seed_y,
                target_seed_max_distance=distance,
                target_seed_quality="high",
                target_seed_ambiguous=0,
                target_seed_json=json.dumps(seed_context, sort_keys=True),
                target_seed_provenance_json=json.dumps(
                    seed_context["provenance"], sort_keys=True
                ),
            )
        limits = {
            "max_scale_ratio": 1.15,
            "axis_angle_degrees": [85.0, 95.0],
            "min_triangle_coverage": 0.02,
            "min_x_span_fraction": 0.20,
            "min_y_span_fraction": 0.20,
            "expected_crs": "EPSG:3857",
            "expected_target_bbox": [
                seed_x - distance,
                seed_y - distance,
                seed_x + distance,
                seed_y + distance,
            ],
            "expected_target_seed": [seed_x, seed_y],
            "max_target_seed_distance": distance,
        }
        _, controls = sanborn_batch.read_points(points)
        diagnostics = sanborn_batch.affine_diagnostics(controls, 100, 100)
        diagnostics["target_location_check"] = sanborn_batch.target_location_diagnostics(
            controls,
            diagnostics,
            100,
            100,
            expected_target_bbox=limits["expected_target_bbox"],
            expected_target_seed=limits["expected_target_seed"],
            max_target_seed_distance=distance,
        )
        labels = [
            "Auburn Avenue x Butler Street",
            "Auburn Avenue x Fort Street",
            "Houston Street x Butler Street",
        ]
        recorded_controls = []
        for control, label in zip(controls, labels):
            recorded = dict(control)
            recorded["label"] = label
            recorded_controls.append(recorded)
        affine_signature = sanborn_batch.affine_provenance_signature(
            sanborn_batch.sha256(source),
            sanborn_batch.sha256(points),
            "EPSG:3857",
            diagnostics,
            limits,
        )
        record = {
            "schema_version": 1,
            "source": {
                "path": str(source),
                "sha256": sanborn_batch.sha256(source),
                "width": 100,
                "height": 100,
            },
            "points": {
                "path": str(points),
                "sha256": sanborn_batch.sha256(points),
                "controls": recorded_controls,
            },
            "transformation": {
                "target_crs": "EPSG:3857",
                "distortion_override": False,
                "safety_limits": limits,
                "diagnostics": diagnostics,
                "warnings": [],
                "affine_provenance_signature": affine_signature,
            },
            "output": {
                "path": str(output),
                "width": 10,
                "height": 10,
                "bytes": output.stat().st_size,
                "sha256": sanborn_batch.sha256(output),
                "bands": ["Red", "Green", "Blue", "Alpha"],
                "band_checksums": [11, 22, 33, 44],
                "alpha_min": 0,
                "alpha_max": 255,
                "compression": "DEFLATE",
                "predictor": 2,
            },
            "protected_project": {
                "path": str(protected),
                "mtime_ns_before": 123,
                "mtime_ns_after": 123,
                "unchanged_during_run": True,
            },
            "quality_note": "Synthetic verified fixture",
        }
        ledger.write_text(json.dumps(record), encoding="utf-8")
        return source, points, output, ledger, protected, fields, record

    def _live_geotiff_info(self, record=None):
        record = record or {}
        source_digest = record.get("source", {}).get("sha256", "0" * 64)
        points_digest = record.get("points", {}).get("sha256", "1" * 64)
        affine_signature = record.get("transformation", {}).get(
            "affine_provenance_signature", "2" * 64
        )
        return {
            "driverShortName": "GTiff",
            "size": [10, 10],
            "bands": [
                {
                    "colorInterpretation": name,
                    "checksum": checksum,
                    **({"minimum": 0, "maximum": 255} if name == "Alpha" else {}),
                }
                for name, checksum in zip(
                    ["Red", "Green", "Blue", "Alpha"], [11, 22, 33, 44]
                )
            ],
            "metadata": {
                "": {
                    sanborn_batch.SOURCE_METADATA_KEY: source_digest,
                    sanborn_batch.POINTS_METADATA_KEY: points_digest,
                    sanborn_batch.AFFINE_METADATA_KEY: affine_signature,
                    sanborn_batch.PIPELINE_METADATA_KEY: sanborn_batch.PIPELINE_METADATA_VALUE,
                },
                "IMAGE_STRUCTURE": {"COMPRESSION": "DEFLATE", "PREDICTOR": "2"},
            },
            "coordinateSystem": {"wkt": 'PROJCRS["WGS 84",ID["EPSG",3857]]'},
            "geoTransform": [-9393125.0, 25.0, 0.0, 3996125.0, 0.0, -25.0],
        }

    def _minimal_archive_fixture(self, tile=196, pixels=b"verified pixels"):
        batch_dir = self.folder / "archive-batch"
        output = self.folder / f"Sanborn 1911 -- Tile {tile}_georeferenced.tif"
        ledger = output.with_suffix(".georef.json")
        manifest = batch_dir / "qgis-import" / f"tile-{tile:04d}.json"
        output.write_bytes(pixels)
        ledger.write_text(
            json.dumps({"schema_version": 1, "output": {"path": str(output)}}),
            encoding="utf-8",
        )
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "tile": tile,
                    "path": str(output),
                    "ledger_path": str(ledger),
                }
            ),
            encoding="utf-8",
        )
        return batch_dir, output, ledger, manifest

    def test_per_tile_lock_blocks_same_tile_but_not_another_tile(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        same_tile_entered = threading.Event()
        other_tile_entered = threading.Event()
        errors: list[BaseException] = []

        def hold_first():
            try:
                with sanborn_batch.tile_file_lock(154):
                    first_entered.set()
                    release_first.wait(3)
            except BaseException as error:  # surfaced in the test thread below
                errors.append(error)

        def enter(tile: int, entered: threading.Event):
            try:
                with sanborn_batch.tile_file_lock(tile):
                    entered.set()
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(sanborn_batch, "LOCK_DIR", self.locks):
            first = threading.Thread(target=hold_first, daemon=True)
            same = threading.Thread(target=enter, args=(154, same_tile_entered), daemon=True)
            other = threading.Thread(target=enter, args=(155, other_tile_entered), daemon=True)
            first.start()
            self.assertTrue(first_entered.wait(1), "the first worker never acquired its tile")
            same.start()
            other.start()
            self.assertTrue(other_tile_entered.wait(1), "a different tile was unnecessarily blocked")
            self.assertFalse(same_tile_entered.wait(0.15), "two workers owned one tile at once")
            release_first.set()
            self.assertTrue(same_tile_entered.wait(1), "the waiting worker never resumed")
            first.join(1)
            same.join(1)
            other.join(1)

        self.assertEqual(errors, [])

    def test_interrupted_downloading_and_proposing_are_recovered_explicitly(self):
        partial = self.folder / "tile-154.jp2.part"
        partial.write_bytes(b"resumable partial download")
        source = self.folder / "tile-155.jp2"
        spatial = self.folder / "tile-155.spatial.json"
        source.write_bytes(b"completed source")
        spatial.write_text("{}", encoding="utf-8")
        with mock.patch.object(sanborn_batch, "LOCK_DIR", self.locks):
            with sanborn_batch.connect(self.database) as db:
                queue(db, 154, "downloading")
                queue(
                    db,
                    155,
                    "proposing",
                    source_path=str(source),
                    source_sha256=sanborn_batch.sha256(source),
                    spatial_ocr_path=str(spatial),
                    printed_number_seen=1,
                )
                queue(db, 156, "proposing")
                self.assertEqual(sanborn_batch.recover_interrupted_tile(db, 154), "queued")
                self.assertEqual(
                    sanborn_batch.recover_interrupted_tile(db, 155), "review-ready"
                )
                self.assertEqual(sanborn_batch.recover_interrupted_tile(db, 156), "queued")
                statuses = {
                    row["tile"]: row["status"]
                    for row in db.execute("SELECT tile, status FROM tiles ORDER BY tile")
                }
                recovered_events = db.execute(
                    "SELECT COUNT(*) FROM events WHERE event='interrupted-work-recovered'"
                ).fetchone()[0]

        self.assertEqual(statuses, {154: "queued", 155: "review-ready", 156: "queued"})
        self.assertEqual(recovered_events, 3)
        self.assertEqual(partial.read_bytes(), b"resumable partial download")

    def test_only_schema_two_apple_vision_spatial_ocr_is_reusable(self):
        source = self.folder / "tile-154.jp2"
        preview = self.folder / "tile-154.png"
        source.write_bytes(b"source")
        Image.new("RGB", (40, 30), "white").save(preview)
        old = self.folder / "schema-1.json"
        current = self.folder / "schema-2.json"
        self._make_spatial_record(old, source, preview, schema_version=1)
        expected = self._make_spatial_record(current, source, preview, schema_version=2)

        self.assertIsNone(sanborn_batch._valid_spatial_ocr(old, 154, source))
        self.assertEqual(
            sanborn_batch._valid_spatial_ocr(current, 154, source),
            expected,
        )

    def test_same_size_same_dimensions_preview_substitution_invalidates_spatial_ocr(self):
        source = self.folder / "tile-154.jp2"
        preview = self.folder / "tile-154.png"
        record_path = self.folder / "tile-154.spatial.json"
        source.write_bytes(b"source")
        Image.new("RGB", (40, 30), "red").save(preview, format="BMP")
        self._make_spatial_record(record_path, source, preview, schema_version=2)
        original_size = preview.stat().st_size

        Image.new("RGB", (40, 30), "blue").save(preview, format="BMP")

        self.assertEqual(preview.stat().st_size, original_size)
        self.assertIsNone(
            sanborn_batch._valid_spatial_ocr(record_path, 154, source)
        )

    def test_recorded_preview_dimensions_must_match_live_image(self):
        source = self.folder / "tile-154.jp2"
        preview = self.folder / "tile-154.png"
        record_path = self.folder / "tile-154.spatial.json"
        source.write_bytes(b"source")
        Image.new("RGB", (40, 30), "white").save(preview)
        record = self._make_spatial_record(record_path, source, preview, schema_version=2)
        record["preview"]["width"] = 41
        record_path.write_text(json.dumps(record), encoding="utf-8")

        self.assertIsNone(
            sanborn_batch._valid_spatial_ocr(record_path, 154, source)
        )

    def test_source_correction_outside_scan_is_rejected_before_export(self):
        proposal = self.folder / "proposal.json"
        points = self.folder / "bad.points"
        comparison = self.folder / "bad-comparison.json"
        proposal.write_text(
            json.dumps(
                {
                    "tile": 154,
                    "source": {"width": 100, "height": 100},
                    "control_candidates": [
                        {
                            "candidate_id": index,
                            "source_x": source_x,
                            "source_y": source_y,
                            "target_x": target_x,
                            "target_y": target_y,
                        }
                        for index, (source_x, source_y, target_x, target_y) in enumerate(
                            (
                                (10, 10, 0, 0),
                                (90, 10, 100, 0),
                                (10, 90, 0, 100),
                            )
                        )
                    ],
                    "ranked_triplets": [{"candidate_ids": [0, 1, 2]}],
                }
            ),
            encoding="utf-8",
        )
        args = argparse.Namespace(
            proposal=proposal,
            triplet=0,
            source_correction=["1,100,10"],
            correction_note="Synthetic correction",
            points=points,
            comparison=comparison,
        )

        with self.assertRaisesRegex(RuntimeError, "outside the 100 x 100 source scan"):
            sanborn_controls.cmd_export(args)
        self.assertFalse(points.exists())
        self.assertFalse(comparison.exists())

    def test_export_allows_only_documented_scale_or_angle_distortion(self):
        def proposal_for(targets, name):
            proposal = self.folder / f"{name}.json"
            proposal.write_text(
                json.dumps(
                    {
                        "tile": 474,
                        "source": {"width": 1000, "height": 1000},
                        "control_candidates": [
                            {
                                "candidate_id": index,
                                "source_x": source_x,
                                "source_y": source_y,
                                "target_x": target_x,
                                "target_y": target_y,
                            }
                            for index, ((source_x, source_y), (target_x, target_y)) in enumerate(
                                zip(((100, 100), (900, 100), (100, 900)), targets)
                            )
                        ],
                        "ranked_triplets": [{"candidate_ids": [0, 1, 2]}],
                    }
                ),
                encoding="utf-8",
            )
            return proposal

        note = "The paper sheet is visibly stretched and both local overlays agree."
        points = self.folder / "distorted.points"
        comparison = self.folder / "distorted-comparison.json"
        accepted = argparse.Namespace(
            proposal=proposal_for(((0, 0), (800, 0), (0, -400)), "distorted"),
            triplet=0,
            source_correction=[],
            correction_note="",
            allow_distortion=True,
            distortion_note=note,
            points=points,
            comparison=comparison,
        )
        self.assertEqual(sanborn_controls.cmd_export(accepted), 0)
        exception = json.loads(comparison.read_text(encoding="utf-8"))["distortion_exception"]
        self.assertTrue(exception["allowed"])
        self.assertEqual(exception["note"], note)
        self.assertTrue(exception["warnings"])

        mirrored_points = self.folder / "mirrored.points"
        mirrored_comparison = self.folder / "mirrored-comparison.json"
        mirrored = argparse.Namespace(
            proposal=proposal_for(((0, 0), (800, 0), (0, 800)), "mirrored"),
            triplet=0,
            source_correction=[],
            correction_note="",
            allow_distortion=True,
            distortion_note="A note must never bypass mirroring.",
            points=mirrored_points,
            comparison=mirrored_comparison,
        )
        with self.assertRaisesRegex(RuntimeError, "non-overridable.*mirrored"):
            sanborn_controls.cmd_export(mirrored)
        self.assertFalse(mirrored_points.exists())
        self.assertFalse(mirrored_comparison.exists())

    def test_final_pair_accepts_valid_ledger_and_rejects_changed_evidence(self):
        source, points, output, ledger, protected, fields, record = self._final_fixture()
        with sanborn_batch.connect(self.database) as db:
            queue(db, 154, "approved", **fields)
            row = db.execute("SELECT * FROM tiles WHERE tile=154").fetchone()
            with mock.patch.object(
                sanborn_batch, "PROTECTED_PROJECT", protected
            ), mock.patch.object(
                sanborn_batch, "_inspect_source_raster", return_value={"size": [100, 100]}
            ), mock.patch.object(
                sanborn_batch,
                "_inspect_final_geotiff",
                return_value=self._live_geotiff_info(record),
            ) as inspect_final:
                verified = sanborn_batch._verify_final_pair(row, output, ledger)
                self.assertEqual(verified["output"]["sha256"], sanborn_batch.sha256(output))

                wrong_hash = deepcopy(record)
                wrong_hash["output"]["sha256"] = "0" * 64
                ledger.write_text(json.dumps(wrong_hash), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                    sanborn_batch._verify_final_pair(row, output, ledger)

                wrong_bbox = deepcopy(record)
                wrong_bbox["transformation"]["safety_limits"]["expected_target_bbox"][0] += 1
                ledger.write_text(json.dumps(wrong_bbox), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "safety limits"):
                    sanborn_batch._verify_final_pair(row, output, ledger)

                changed_project = deepcopy(record)
                changed_project["protected_project"]["unchanged_during_run"] = False
                ledger.write_text(json.dumps(changed_project), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "does not prove"):
                    sanborn_batch._verify_final_pair(row, output, ledger)

                ledger.write_text(json.dumps(record), encoding="utf-8")
                wrong_metadata = self._live_geotiff_info(record)
                wrong_metadata["metadata"][""][sanborn_batch.SOURCE_METADATA_KEY] = "0" * 64
                inspect_final.return_value = wrong_metadata
                with self.assertRaisesRegex(RuntimeError, "internally bound"):
                    sanborn_batch._verify_final_pair(row, output, ledger)

                wrong_extent = self._live_geotiff_info(record)
                wrong_extent["geoTransform"][0] += 1000
                inspect_final.return_value = wrong_extent
                with self.assertRaisesRegex(RuntimeError, "extent does not match"):
                    sanborn_batch._verify_final_pair(row, output, ledger)

    def test_queue_seed_columns_cannot_disagree_with_the_hash_locked_seed_record(self):
        _, _, _, _, _, fields, _ = self._final_fixture()
        fields["target_seed_x"] = float(fields["target_seed_x"]) + 1.0
        with sanborn_batch.connect(self.database) as db:
            queue(db, 154, "approved", **fields)
            row = db.execute("SELECT * FROM tiles WHERE tile=154").fetchone()
            with self.assertRaisesRegex(RuntimeError, "queue columns disagree"):
                sanborn_batch._require_fresh_stored_seed(row)

    def test_interrupted_one_file_publish_is_preserved_for_inspection(self):
        for present in ("output", "ledger"):
            with self.subTest(present=present):
                folder = self.folder / present
                folder.mkdir()
                output = folder / "tile.tif"
                ledger = folder / "tile.georef.json"
                path = output if present == "output" else ledger
                path.write_bytes(b"interrupted evidence")

                sanborn_batch._preserve_incomplete_pair(output, ledger)

                self.assertFalse(output.exists())
                self.assertFalse(ledger.exists())
                preserved = list(folder.glob("*.incomplete-*.*"))
                self.assertEqual(len(preserved), 1)
                self.assertEqual(preserved[0].read_bytes(), b"interrupted evidence")

    def test_complete_partial_download_is_promoted_without_an_eof_request(self):
        destination = self.folder / "tile.jp2"
        partial = destination.with_suffix(".jp2.part")
        content = b"complete LOC response already on disk"
        partial.write_bytes(content)
        with mock.patch.object(sanborn_batch, "request") as request:
            sanborn_batch.download(
                "https://example.invalid/tile.jp2",
                destination,
                expected_bytes=len(content),
            )
        request.assert_not_called()
        self.assertEqual(destination.read_bytes(), content)
        self.assertFalse(partial.exists())

    def test_verified_tile_can_be_explicitly_reopened_to_approved_for_revalidation(self):
        review_dir = self.folder / "review"
        review_dir.mkdir()
        (review_dir / "review.json").write_text("{}\n", encoding="utf-8")
        (review_dir / "approval.json").write_text("{}\n", encoding="utf-8")
        points = self.folder / "approved.points"
        points.write_text("approved controls", encoding="utf-8")
        with sanborn_batch.connect(self.database) as db:
            queue(
                db,
                154,
                "verified",
                review_dir=str(review_dir),
                points_path=str(points),
                output_path=str(self.folder / "old.tif"),
            )
            with mock.patch.object(
                sanborn_batch,
                "require_approval",
                return_value=({"approval_token": "review-token"}, {"approved": True}),
            ) as approval_check:
                sanborn_batch.reopen_tile(
                    db,
                    154,
                    "approved",
                    "Re-run final evidence checks after an engine update",
                )
            row = db.execute("SELECT * FROM tiles WHERE tile=154").fetchone()

        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["review_dir"], str(review_dir))
        self.assertEqual(row["points_path"], str(points))
        approval_check.assert_called_once_with(review_dir)

    def test_reopen_to_approved_explains_when_a_fresh_packet_is_required(self):
        review_dir = self.folder / "review"
        with sanborn_batch.connect(self.database) as db:
            queue(db, 154, "verified", review_dir=str(review_dir), points_path="old.points")
            with mock.patch.object(sanborn_batch, "require_approval", side_effect=RuntimeError("renderer changed")):
                with self.assertRaisesRegex(RuntimeError, "Reopen to review-ready.*fresh approval"):
                    sanborn_batch.reopen_tile(db, 154, "approved", "Check old result")
            self.assertEqual(db.execute("SELECT status FROM tiles WHERE tile=154").fetchone()["status"], "verified")

    def test_reopen_clears_locked_links_but_keeps_historical_packet_files(self):
        source, spatial, points = [self.folder / name for name in ("source.jp2", "ocr.json", "controls.points")]
        review_dir = self.folder / "review"
        review_dir.mkdir()
        review_file, approval_file = review_dir / "review.json", review_dir / "approval.json"
        for path in (source, spatial, points, review_file, approval_file):
            path.write_bytes(b"immutable historical evidence")
        originals = {path: path.read_bytes() for path in (points, review_file, approval_file)}
        for status in ("awaiting-approval", "approved", "failed", "proposal-rejected", "proposal-stale"):
            with self.subTest(status=status), sanborn_batch.connect(self.database) as db:
                db.execute("DELETE FROM tiles WHERE tile=154")
                queue(db, 154, status, source_path=str(source), source_sha256=sanborn_batch.sha256(source),
                      spatial_ocr_path=str(spatial), printed_number_seen=1, points_path=str(points), review_dir=str(review_dir))
                sanborn_batch.reopen_tile(db, 154, "review-ready", "Choose new historic street centers")
                row = db.execute("SELECT * FROM tiles WHERE tile=154").fetchone()
                self.assertIsNone(row["points_path"])
                self.assertIsNone(row["review_dir"])
                self.assertEqual(row["status"], "review-ready")
                for path, content in originals.items():
                    self.assertEqual(path.read_bytes(), content)

    def test_verified_rebuild_archives_prior_fixed_name_result(self):
        batch_dir = self.folder / "batch"
        output = self.folder / "Sanborn 1911 -- Tile 196_georeferenced.tif"
        ledger = output.with_suffix(".georef.json")
        manifest = batch_dir / "qgis-import" / "tile-0196.json"
        source = self.folder / "tile-196.jp2"
        spatial = self.folder / "tile-196.spatial.json"
        points = self.folder / "old.points"
        for path, content in (
            (output, b"old warped pixels"),
            (source, b"source"),
            (spatial, b"spatial"),
            (points, b"old controls"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        ledger.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "output": {"path": str(output)},
                }
            ),
            encoding="utf-8",
        )
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "tile": 196,
                    "path": str(output),
                    "ledger_path": str(ledger),
                }
            ),
            encoding="utf-8",
        )
        with sanborn_batch.connect(self.database) as db:
            queue(
                db,
                196,
                "verified",
                source_path=str(source),
                source_sha256=sanborn_batch.sha256(source),
                spatial_ocr_path=str(spatial),
                printed_number_seen=1,
                points_path=str(points),
                review_dir=str(self.folder / "old-review"),
                output_path=str(output),
            )
            with mock.patch.object(sanborn_batch, "BATCH_DIR", batch_dir), mock.patch.object(
                sanborn_batch, "validate_qgis_manifest"
            ):
                sanborn_batch.reopen_tile(
                    db,
                    196,
                    "review-ready",
                    "Old controls were rubber banded; rebuild from corrected points",
                )
            row = db.execute("SELECT * FROM tiles WHERE tile=196").fetchone()
            archived_event = db.execute(
                "SELECT detail FROM events WHERE tile=196 AND event='prior-result-archived'"
            ).fetchone()

        archived = list((batch_dir / "archive" / "tile-0196").rglob("*"))
        archived_files = [path for path in archived if path.is_file()]
        self.assertTrue(any(path.name == "archive-index.json" for path in archived_files))
        self.assertTrue(any(path.parent.name == "verified-bundle" and path.name == manifest.name for path in archived_files))
        self.assertFalse(output.exists())
        self.assertFalse(ledger.exists())
        self.assertFalse(manifest.exists())
        self.assertEqual(row["status"], "review-ready")
        self.assertIsNone(row["points_path"])
        self.assertIsNone(row["review_dir"])
        self.assertIsNone(row["output_path"])
        self.assertIsNotNone(archived_event)
        output.write_bytes(b"new corrected warp can use the fixed name")
        self.assertTrue(output.is_file())

    def test_archive_preflight_failure_leaves_verified_originals_and_status_intact(self):
        batch_dir, output, ledger, manifest = self._minimal_archive_fixture()
        expected = {path: path.read_bytes() for path in (output, ledger, manifest)}
        with sanborn_batch.connect(self.database) as db:
            queue(db, 196, "verified", output_path=str(output))
            with mock.patch.object(sanborn_batch, "BATCH_DIR", batch_dir), mock.patch.object(
                sanborn_batch,
                "validate_qgis_manifest",
                side_effect=RuntimeError("synthetic archive preflight rejection"),
            ):
                with self.assertRaisesRegex(RuntimeError, "preflight rejection"):
                    sanborn_batch.reopen_tile(db, 196, "queued", "Correct the controls")
            status = db.execute("SELECT status FROM tiles WHERE tile=196").fetchone()["status"]

        self.assertEqual(status, "verified")
        for path, content in expected.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(list((batch_dir / "archive").glob("tile-*/*")), [])

    def test_archive_unlink_failure_restores_every_original_and_keeps_verified_status(self):
        batch_dir, output, ledger, manifest = self._minimal_archive_fixture()
        expected = {path: path.read_bytes() for path in (output, ledger, manifest)}
        candidate_paths = {str(path.resolve()) for path in expected}
        real_unlink = Path.unlink
        unlink_count = [0]

        def fail_on_second_candidate(path, *args, **kwargs):
            if str(path.resolve()) in candidate_paths:
                unlink_count[0] += 1
                if unlink_count[0] == 2:
                    raise OSError("synthetic second-unlink crash")
            return real_unlink(path, *args, **kwargs)

        with sanborn_batch.connect(self.database) as db:
            queue(db, 196, "verified", output_path=str(output))
            with mock.patch.object(sanborn_batch, "BATCH_DIR", batch_dir), mock.patch.object(
                sanborn_batch, "validate_qgis_manifest"
            ), mock.patch.object(Path, "unlink", new=fail_on_second_candidate):
                with self.assertRaisesRegex(OSError, "second-unlink crash"):
                    sanborn_batch.reopen_tile(db, 196, "queued", "Correct the controls")
            status = db.execute("SELECT status FROM tiles WHERE tile=196").fetchone()["status"]

        self.assertEqual(status, "verified")
        self.assertEqual(unlink_count[0], 2)
        for path, content in expected.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(list((batch_dir / "archive").glob("tile-*/*")), [])

    def test_retired_old_archive_is_never_restored_as_a_new_verified_result(self):
        batch_dir, output, ledger, manifest = self._minimal_archive_fixture(pixels=b"PIXELS-V1")
        row = {"tile": 196, "output_path": str(output)}
        with mock.patch.object(sanborn_batch, "BATCH_DIR", batch_dir), mock.patch.object(
            sanborn_batch, "validate_qgis_manifest"
        ):
            sanborn_batch._archive_verified_result(row)
            output.write_bytes(b"PIXELS-V2")
            ledger.write_text(
                json.dumps({"schema_version": 1, "output": {"path": str(output)}}),
                encoding="utf-8",
            )
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "tile": 196,
                        "path": str(output),
                        "ledger_path": str(ledger),
                    }
                ),
                encoding="utf-8",
            )
            for path in (output, ledger, manifest):
                path.unlink()
            with self.assertRaisesRegex(RuntimeError, "all missing"):
                sanborn_batch._archive_verified_result(row)

        archived_pixels = [
            path.read_bytes()
            for path in (batch_dir / "archive" / "tile-0196").glob("*/originals/*.tif")
        ]
        self.assertEqual(archived_pixels, [b"PIXELS-V1"])
        self.assertFalse(output.exists())

    def test_packet_forwards_documented_distortion_exception(self):
        source = self.folder / "tile-474.jp2"
        points = self.folder / "tile-474.points"
        source.write_bytes(b"scan")
        points.write_text("controls", encoding="utf-8")
        with sanborn_batch.connect(self.database) as db:
            queue(
                db,
                474,
                "review-ready",
                source_path=str(source),
                source_sha256=sanborn_batch.sha256(source),
                printed_number_seen=1,
            )
        note = "The historic sheet is internally skewed; both independent overlays agree."
        args = argparse.Namespace(
            database=self.database,
            tile=474,
            points=points,
            osm_db=None,
            kauffman_map=None,
            control_label=[
                "Boulevard x Decatur Street",
                "Boulevard x Tennelle Street",
                "Carroll Street x Tennelle Street",
            ],
            index_json=self._make_seed_index(474),
            allow_distortion=True,
            distortion_note=note,
        )
        with mock.patch.object(sanborn_batch, "BATCH_DIR", self.folder / "batch"), mock.patch.object(
            sanborn_batch, "LOCK_DIR", self.locks
        ), mock.patch.object(sanborn_batch.subprocess, "run") as run:
            self.assertEqual(sanborn_batch.cmd_packet(args), 0)

        command = run.call_args.args[0]
        self.assertIn("--allow-distortion", command)
        self.assertEqual(command[command.index("--distortion-note") + 1], note)
        with sanborn_batch.connect(self.database) as db:
            row = db.execute("SELECT * FROM tiles WHERE tile=474").fetchone()
        self.assertEqual(row["status"], "awaiting-approval")

    def test_replaced_source_cannot_reuse_a_prior_tile_identity_check(self):
        source = self.folder / "tile-154.jp2"
        points = self.folder / "tile-154.points"
        source.write_bytes(b"the scan whose printed title was checked")
        points.write_text("controls", encoding="utf-8")
        prepared_digest = sanborn_batch.sha256(source)
        with sanborn_batch.connect(self.database) as db:
            queue(
                db,
                154,
                "review-ready",
                source_path=str(source),
                source_sha256=prepared_digest,
                printed_number_seen=1,
            )
        source.write_bytes(b"a different scan at the same path")
        args = argparse.Namespace(
            database=self.database,
            tile=154,
            points=points,
            osm_db=None,
            kauffman_map=None,
            control_label=[
                "Auburn Avenue x Butler Street",
                "Auburn Avenue x Fort Street",
                "Houston Street x Butler Street",
            ],
            index_json=self._make_seed_index(154),
            allow_distortion=False,
            distortion_note="",
        )
        with mock.patch.object(sanborn_batch, "LOCK_DIR", self.locks), mock.patch.object(
            sanborn_batch.subprocess, "run"
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "identity check"):
                sanborn_batch.cmd_packet(args)
        run.assert_not_called()

        # Even a replacement and ledger whose hashes agree with each other may
        # not inherit the printed title check stored for the original source.
        output = self.folder / "replacement.tif"
        ledger = self.folder / "replacement.georef.json"
        protected = self.folder / "protected.qgz"
        output.write_bytes(b"replacement output")
        protected.write_bytes(b"protected")
        record = {
            "schema_version": 1,
            "source": {"path": str(source), "sha256": sanborn_batch.sha256(source)},
            "points": {"path": str(points), "sha256": sanborn_batch.sha256(points)},
            "transformation": {
                "target_crs": "EPSG:3857",
                "safety_limits": {},
            },
            "output": {
                "path": str(output),
                "bytes": output.stat().st_size,
                "sha256": sanborn_batch.sha256(output),
                "bands": ["Red", "Green", "Blue", "Alpha"],
                "compression": "DEFLATE",
                "predictor": 2,
            },
            "protected_project": {
                "path": str(protected),
                "unchanged_during_run": True,
            },
        }
        ledger.write_text(json.dumps(record), encoding="utf-8")
        with sanborn_batch.connect(self.database) as db:
            sanborn_batch.update_tile(db, 154, points_path=str(points))
            db.commit()
            row = db.execute("SELECT * FROM tiles WHERE tile=154").fetchone()
            with mock.patch.object(
                sanborn_batch, "PROTECTED_PROJECT", protected
            ), mock.patch.object(
                sanborn_batch,
                "_inspect_final_geotiff",
                return_value=self._live_geotiff_info(),
            ):
                with self.assertRaisesRegex(RuntimeError, "identity check"):
                    sanborn_batch._verify_final_pair(row, output, ledger)

    def test_packet_requires_a_unique_fresh_index_seed(self):
        source = self.folder / "tile-154.jp2"
        points = self.folder / "tile-154.points"
        source.write_bytes(b"source")
        points.write_text("controls", encoding="utf-8")
        empty_index = self._make_seed_index(154)
        empty_record = json.loads(empty_index.read_text(encoding="utf-8"))
        empty_record["seeds"] = []
        empty_index.write_text(json.dumps(empty_record), encoding="utf-8")
        with sanborn_batch.connect(self.database) as db:
            queue(
                db,
                154,
                "review-ready",
                source_path=str(source),
                source_sha256=sanborn_batch.sha256(source),
                printed_number_seen=1,
            )
        labels = [
            "Auburn Avenue x Butler Street",
            "Auburn Avenue x Fort Street",
            "Houston Street x Butler Street",
        ]
        args = argparse.Namespace(
            database=self.database,
            tile=154,
            points=points,
            osm_db=None,
            kauffman_map=None,
            control_label=labels,
            index_json=empty_index,
            allow_distortion=False,
            distortion_note="",
        )
        with mock.patch.object(sanborn_batch, "LOCK_DIR", self.locks), mock.patch.object(
            sanborn_batch.subprocess, "run"
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "one unique index-map seed"):
                sanborn_batch.cmd_packet(args)
        run.assert_not_called()

        selected_index = self._make_seed_index(154)
        with sanborn_batch.connect(self.database) as db:
            sanborn_batch.store_tile_seed(db, 154, selected_index, missing_ok=False)
            row = db.execute("SELECT * FROM tiles WHERE tile=154").fetchone()
            self.assertEqual(sanborn_batch._require_fresh_stored_seed(row)["status"], "selected")
            record = json.loads(selected_index.read_text(encoding="utf-8"))
            record["seeds"][0]["map_x"] += 10
            selected_index.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "seed file changed"):
                sanborn_batch._require_fresh_stored_seed(row)

    def test_finish_emits_hash_bound_schema_three_qgis_manifest(self):
        source, points, output, ledger, protected, fields, fixture_record = self._final_fixture()
        tile = 154
        review_dir = self.folder / "review"
        review_dir.mkdir()
        (review_dir / "review.json").write_text("{}\n", encoding="utf-8")
        (review_dir / "approval.json").write_text("{}\n", encoding="utf-8")
        fields.update(review_dir=str(review_dir))
        with sanborn_batch.connect(self.database) as db:
            queue(db, tile, "approved", **fields)
        review = {
            "approval_token": "hash-locked-review-token",
            "source": {"path": str(source), "sha256": sanborn_batch.sha256(source)},
            "points": {
                "path": str(points),
                "sha256": sanborn_batch.sha256(points),
                "controls": fixture_record["points"]["controls"],
            },
            "safety_limits": {
                "allow_distortion": False,
                "distortion_note": "",
                "distortion_warnings": [],
                "target_seed_context": json.loads(fields["target_seed_json"]),
            },
        }
        approval = {
            "geographic_verification": {
                "reference_method": "local-osm-and-kauffman-packet",
                "note": "Both independent overlays were reviewed.",
            }
        }
        downloads = self.folder / "downloads"
        downloads.mkdir()
        final_output = downloads / sanborn_batch.output_name(tile)
        final_ledger = final_output.with_suffix(".georef.json")
        output.replace(final_output)
        final_record = json.loads(ledger.read_text(encoding="utf-8"))
        final_record["output"]["path"] = str(final_output)
        final_record["output"]["sha256"] = sanborn_batch.sha256(final_output)
        final_record["output"]["bytes"] = final_output.stat().st_size
        final_ledger.write_text(json.dumps(final_record), encoding="utf-8")
        ledger.unlink()
        batch_dir = self.folder / "batch"
        args = argparse.Namespace(database=self.database, tile=tile, quality_note="")

        with mock.patch.object(sanborn_batch, "DOWNLOAD_DIR", downloads), mock.patch.object(
            sanborn_batch, "BATCH_DIR", batch_dir
        ), mock.patch.object(sanborn_batch, "LOCK_DIR", self.locks), mock.patch.object(
            sanborn_batch, "PROTECTED_PROJECT", protected
        ), mock.patch.object(
            sanborn_batch, "require_approval", return_value=(review, approval)
        ), mock.patch.object(
            sanborn_batch, "_inspect_source_raster", return_value={"size": [100, 100]}
        ), mock.patch.object(
            sanborn_batch,
            "_inspect_final_geotiff",
            return_value=self._live_geotiff_info(final_record),
        ), mock.patch.object(
            sanborn_batch, "validate_qgis_manifest"
        ), mock.patch.object(sanborn_batch.subprocess, "run") as run:
            self.assertEqual(sanborn_batch.cmd_finish(args), 0)

        run.assert_not_called()
        manifest_path = batch_dir / "qgis-import" / "tile-0154.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 3)
        self.assertEqual(manifest["tile"], tile)
        self.assertEqual(manifest["path"], str(final_output))
        self.assertEqual(manifest["raster_sha256"], sanborn_batch.sha256(final_output))
        self.assertEqual(manifest["ledger_path"], str(final_ledger))
        self.assertEqual(manifest["ledger_sha256"], sanborn_batch.sha256(final_ledger))
        self.assertEqual(manifest["source_sha256"], sanborn_batch.sha256(source))
        self.assertEqual(manifest["points_sha256"], sanborn_batch.sha256(points))
        self.assertEqual(
            manifest["geographic_verification"]["reference_method"],
            "local-osm-and-kauffman",
        )
        self.assertFalse(manifest["save_project"])
        self.assertFalse(manifest["expanded"])
        with sanborn_batch.connect(self.database) as db:
            row = db.execute("SELECT * FROM tiles WHERE tile=?", (tile,)).fetchone()
        self.assertEqual(row["status"], "verified")
        self.assertEqual(row["output_path"], str(final_output))


if __name__ == "__main__":
    unittest.main()
