#!/usr/bin/env python3

import argparse
import copy
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import sanborn_review
from sanborn_osm import EARTH_RADIUS_METERS, import_osm, web_mercator
from sanborn_review import (
    LOCAL_REFERENCE_METHOD,
    REQUIRED_ARTIFACTS,
    approve_packet,
    create_packet,
    current_review_state,
    require_approval,
)


def inverse_mercator(x, y):
    lon = math.degrees(x / EARTH_RADIUS_METERS)
    lat = math.degrees(2 * math.atan(math.exp(y / EARTH_RADIUS_METERS)) - math.pi / 2)
    return lon, lat


class LocalReviewPacketTests(unittest.TestCase):
    def setUp(self):
        if not all(shutil.which(program) for program in ("gdalinfo", "gdal_translate", "gdalwarp")):
            self.skipTest("GDAL command-line tools are required")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "scan.png"
        self.points = self.root / "controls.points"
        self.osm_db = self.root / "streets.sqlite3"
        self.kauffman = self.root / "kauffman.tif"
        self.seed_record = self.root / "index-seeds.json"
        self.seed_record.write_text('{"stable":"seed evidence"}\n', encoding="utf-8")
        self.review_dir = self.root / "review"
        self.base_x, self.base_y = web_mercator(-84.39, 33.75)
        self._make_source()
        self._make_points()
        self._make_osm()
        self._make_kauffman()
        create_packet(self._create_args())

    def tearDown(self):
        self.temporary.cleanup()

    def _make_source(self):
        image = Image.new("RGB", (1000, 1000), "ivory")
        draw = ImageDraw.Draw(image)
        for coordinate in range(100, 1000, 100):
            draw.line((0, coordinate, 999, coordinate), fill=(90, 80, 70), width=3)
            draw.line((coordinate, 0, coordinate, 999), fill=(90, 80, 70), width=3)
        draw.rectangle((80, 80, 920, 920), outline="black", width=6)
        image.save(self.source)

    def _make_points(self):
        self.points.write_text(
            "#CRS: EPSG:3857\n"
            "mapX,mapY,sourceX,sourceY,enable\n"
            f"{self.base_x},{self.base_y},100,-100,1\n"
            f"{self.base_x + 800},{self.base_y},900,-100,1\n"
            f"{self.base_x},{self.base_y - 800},100,-900,1\n",
            encoding="utf-8",
        )

    def _make_distorted_points(self):
        points = self.root / "distorted-controls.points"
        points.write_text(
            "#CRS: EPSG:3857\n"
            "mapX,mapY,sourceX,sourceY,enable\n"
            f"{self.base_x},{self.base_y},100,-100,1\n"
            f"{self.base_x + 800},{self.base_y},900,-100,1\n"
            f"{self.base_x},{self.base_y - 400},100,-900,1\n",
            encoding="utf-8",
        )
        return points

    def _make_hard_failure_points(self, kind):
        points = self.root / f"hard-{kind}.points"
        if kind == "mirrored":
            rows = (
                (self.base_x, self.base_y, 100, -100),
                (self.base_x + 800, self.base_y, 900, -100),
                (self.base_x, self.base_y + 800, 100, -900),
            )
        else:
            rows = (
                (self.base_x, self.base_y, 100, -100),
                (self.base_x + 50, self.base_y, 150, -100),
                (self.base_x, self.base_y - 50, 100, -150),
            )
        points.write_text(
            "#CRS: EPSG:3857\n"
            "mapX,mapY,sourceX,sourceY,enable\n"
            + "".join(
                f"{map_x},{map_y},{source_x},{source_y},1\n"
                for map_x, map_y, source_x, source_y in rows
            ),
            encoding="utf-8",
        )
        return points

    def _node(self, node_id, x, y):
        lon, lat = inverse_mercator(x, y)
        return f'<node id="{node_id}" lon="{lon:.12f}" lat="{lat:.12f}" />'

    def _make_osm(self):
        source = self.root / "streets.osm"
        nodes = [
            self._node(1, self.base_x - 100, self.base_y - 300),
            self._node(2, self.base_x + 300, self.base_y - 300),
            self._node(3, self.base_x + 900, self.base_y - 300),
            self._node(4, self.base_x + 300, self.base_y + 100),
            self._node(5, self.base_x + 300, self.base_y - 900),
        ]
        source.write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<osm version=\"0.6\">\n"
            + "\n".join(nodes)
            + "\n<way id=\"101\"><nd ref=\"1\"/><nd ref=\"2\"/><nd ref=\"3\"/>"
            '<tag k="highway" v="residential"/><tag k="name" v="Test Street"/></way>\n'
            '<way id="102"><nd ref="4"/><nd ref="2"/><nd ref="5"/>'
            '<tag k="highway" v="secondary"/><tag k="name" v="Example Avenue"/></way>\n'
            "</osm>\n",
            encoding="utf-8",
        )
        import_osm(source, self.osm_db, replace=True)

    def _make_kauffman(self):
        png = self.root / "kauffman.png"
        image = Image.new("RGB", (500, 500), (232, 221, 191))
        draw = ImageDraw.Draw(image)
        for coordinate in range(25, 500, 50):
            draw.line((0, coordinate, 499, coordinate), fill=(80, 100, 95), width=2)
            draw.line((coordinate, 0, coordinate, 499), fill=(80, 100, 95), width=2)
        image.save(png)
        subprocess.run(
            [
                shutil.which("gdal_translate"),
                "-q",
                "-of",
                "GTiff",
                "-a_srs",
                "EPSG:3857",
                "-a_ullr",
                str(self.base_x - 200),
                str(self.base_y + 200),
                str(self.base_x + 1000),
                str(self.base_y - 1000),
                str(png),
                str(self.kauffman),
            ],
            check=True,
        )

    def _create_args(self, **overrides):
        arguments = argparse.Namespace(
            source=self.source,
            points=self.points,
            review_dir=self.review_dir,
            osm_db=self.osm_db,
            kauffman_map=self.kauffman,
            control_label=[
                "Test Street x Example Avenue",
                "Test Street x Second Avenue",
                "Third Street x Example Avenue",
            ],
            target_seed_json=json.dumps(
                {
                    "status": "selected",
                    "tile": 154,
                    "map_x": self.base_x + 400,
                    "map_y": self.base_y - 400,
                    "suggested_max_distance": 250.0,
                    "provenance": {
                        "path": str(self.seed_record),
                        "sha256": __import__("hashlib").sha256(
                            self.seed_record.read_bytes()
                        ).hexdigest(),
                    },
                },
                sort_keys=True,
            ),
            source_preview_width=320,
            map_preview_width=300,
            max_scale_ratio=1.15,
            min_axis_angle=85.0,
            max_axis_angle=95.0,
            min_triangle_coverage=0.02,
            min_x_span_fraction=0.20,
            min_y_span_fraction=0.20,
            expected_crs="EPSG:3857",
            allow_distortion=False,
            distortion_note="",
            replace=True,
        )
        for name, value in overrides.items():
            setattr(arguments, name, value)
        return arguments

    def test_packet_contains_local_osm_kauffman_and_contact_sheet(self):
        import json

        review = json.loads((self.review_dir / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(review["schema_version"], 3)
        self.assertEqual(
            review["local_geographic_verification"]["method"],
            LOCAL_REFERENCE_METHOD,
        )
        self.assertEqual(set(review["artifacts"]), set(REQUIRED_ARTIFACTS))
        self.assertGreater(review["render_spec"]["osm"]["way_count"], 0)
        with Image.open(review["artifacts"]["osm_roads"]["path"]) as roads:
            self.assertIsNotNone(roads.getbbox())
        current_review_state(review)

    def test_packet_requires_three_named_intersections(self):
        with self.assertRaisesRegex(RuntimeError, "exactly three"):
            create_packet(
                self._create_args(
                    review_dir=self.root / "unlabeled-review",
                    control_label=[],
                )
            )

    def test_approval_needs_no_external_qgis_note_and_survives_recheck(self):
        approve_packet(
            argparse.Namespace(
                review_dir=self.review_dir,
                approved_by="Test reviewer",
                note="The two independent local overlays agree.",
            )
        )
        review, approval = require_approval(self.review_dir)
        self.assertTrue(review["approved"])
        self.assertEqual(
            approval["geographic_verification"]["reference_method"],
            LOCAL_REFERENCE_METHOD,
        )
        self.assertEqual(
            set(approval["geographic_verification"]["artifact_sha256"]),
            set(REQUIRED_ARTIFACTS),
        )

    def _approve_for_frozen_test(self):
        approve_packet(argparse.Namespace(review_dir=self.review_dir,
            approved_by="Test reviewer", note="Reviewed the completed evidence."))

    def test_frozen_approval_survives_renderer_and_software_updates_but_new_approval_does_not(self):
        # Approve using a private copy of the renderer so no production code is
        # modified merely to exercise a code-upgrade boundary.
        path = self.review_dir / "review.json"
        review = json.loads(path.read_text())
        renderer = self.root / "renderer-copy.py"
        shutil.copy2(Path(sanborn_review.__file__), renderer)
        review["provenance"]["renderer_code"]["sanborn_review"] = sanborn_review._file_record(renderer)
        review["approval_token"] = sanborn_review.approval_token(
            self.source, self.points, review["affine_diagnostics"],
            labels=[p["label"] for p in review["points"]["controls"]],
            target_crs=review["points"]["target_crs"], safety_limits=review["safety_limits"],
            provenance=review["provenance"], artifacts=review["artifacts"], render_spec=review["render_spec"])
        path.write_text(json.dumps(review))
        self._approve_for_frozen_test()
        renderer.write_text(renderer.read_text() + "\n# renderer upgrade\n")
        with mock.patch.object(sanborn_review, "_software_provenance", return_value={"gdal": "future"}):
            with mock.patch.object(sanborn_review, "require_program", side_effect=AssertionError("No live toolchain needed")):
                require_approval(self.review_dir, frozen=True)
            with self.assertRaisesRegex(RuntimeError, "renderer code"):
                require_approval(self.review_dir)
            with self.assertRaisesRegex(RuntimeError, "renderer code"):
                self._approve_for_frozen_test()

    def test_frozen_approval_rejects_changed_source_points_references_and_artifacts(self):
        self._approve_for_frozen_test()
        review = json.loads((self.review_dir / "review.json").read_text())
        paths = [self.source, self.points, self.osm_db, self.kauffman,
                 *[Path(record["path"]) for record in review["artifacts"].values()]]
        for path in paths:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b"changed")
                    with self.assertRaisesRegex(RuntimeError, "changed"):
                        require_approval(self.review_dir, frozen=True)
                finally:
                    path.write_bytes(original)
        require_approval(self.review_dir, frozen=True)

    def test_frozen_approval_recomputes_original_token(self):
        self._approve_for_frozen_test()
        path = self.review_dir / "review.json"
        review = json.loads(path.read_text())
        review["render_spec"]["changed"] = True
        path.write_text(json.dumps(review))
        with self.assertRaisesRegex(RuntimeError, "frozen approval token"):
            require_approval(self.review_dir, frozen=True)

    def test_approval_requires_named_reviewer_current_schema_and_zoned_timestamp(self):
        with self.assertRaisesRegex(RuntimeError, "nonblank reviewer"):
            approve_packet(
                argparse.Namespace(
                    review_dir=self.review_dir,
                    approved_by="   ",
                    note="Cannot approve anonymously.",
                )
            )
        approve_packet(
            argparse.Namespace(
                review_dir=self.review_dir,
                approved_by="Test reviewer",
                note="Both local overlays were checked.",
            )
        )
        approval_file = self.review_dir / "approval.json"
        original = json.loads(approval_file.read_text(encoding="utf-8"))
        cases = (
            ("blank reviewer", {"approved_by": "  "}, "no reviewer name"),
            ("wrong schema", {"schema_version": 2}, "unsupported schema"),
            ("bad timestamp", {"approved_utc": "not-a-time"}, "timestamp is invalid"),
            (
                "timezone-free timestamp",
                {"approved_utc": "2026-07-15T12:00:00"},
                "must include a timezone",
            ),
        )
        for label, changes, expected in cases:
            with self.subTest(label=label):
                changed = copy.deepcopy(original)
                changed.update(changes)
                approval_file.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, expected):
                    require_approval(self.review_dir)
        approval_file.write_text(json.dumps(original), encoding="utf-8")
        require_approval(self.review_dir)

    def test_every_required_artifact_is_checked_for_change_and_deletion(self):
        import json

        review = json.loads((self.review_dir / "review.json").read_text(encoding="utf-8"))
        for key in REQUIRED_ARTIFACTS:
            path = Path(review["artifacts"][key]["path"])
            original = path.read_bytes()
            path.write_bytes(original + b"changed")
            with self.assertRaisesRegex(RuntimeError, "changed after"):
                current_review_state(review)
            path.write_bytes(original)
        deleted = Path(review["artifacts"]["osm_overlay"]["path"])
        original = deleted.read_bytes()
        deleted.unlink()
        with self.assertRaisesRegex(RuntimeError, "missing"):
            current_review_state(review)
        deleted.write_bytes(original)

    def test_reference_or_points_change_invalidates_an_existing_approval(self):
        approve_packet(
            argparse.Namespace(review_dir=self.review_dir, approved_by="Test", note="")
        )
        original_db = self.osm_db.read_bytes()
        self.osm_db.write_bytes(original_db + b"changed")
        with self.assertRaisesRegex(RuntimeError, "OSM database.*changed"):
            require_approval(self.review_dir)
        self.osm_db.write_bytes(original_db)

        original_points = self.points.read_text(encoding="utf-8")
        self.points.write_text(original_points.replace("900,-100", "850,-100"), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "control-points file.*changed"):
            require_approval(self.review_dir)

    def test_distortion_warning_rejects_without_override_and_nonblank_note(self):
        points = self._make_distorted_points()
        review_dir = self.root / "distortion-rejected"
        with self.assertRaisesRegex(RuntimeError, "rejected by affine safety gate"):
            create_packet(self._create_args(points=points, review_dir=review_dir))

        with self.assertRaisesRegex(
            RuntimeError, "requires a nonblank --distortion-note"
        ):
            create_packet(
                self._create_args(
                    points=points,
                    review_dir=review_dir,
                    allow_distortion=True,
                    distortion_note="   ",
                )
            )

    def test_documented_distortion_override_is_accepted_and_hash_locked(self):
        points = self._make_distorted_points()
        review_dir = self.root / "distortion-accepted"
        note = "A documented historical paper stretch explains the unequal axes."
        create_packet(
            self._create_args(
                points=points,
                review_dir=review_dir,
                allow_distortion=True,
                distortion_note=note,
            )
        )

        review_file = review_dir / "review.json"
        review = json.loads(review_file.read_text(encoding="utf-8"))
        limits = review["safety_limits"]
        self.assertIs(limits["allow_distortion"], True)
        self.assertEqual(limits["distortion_note"], note)
        self.assertTrue(limits["distortion_warnings"])
        token, _ = current_review_state(review)
        self.assertEqual(token, review["approval_token"])

        approve_packet(
            argparse.Namespace(
                review_dir=review_dir,
                approved_by="Test reviewer",
                note="The documented distortion and both overlays were reviewed.",
            )
        )
        require_approval(review_dir)
        approved = json.loads(review_file.read_text(encoding="utf-8"))

        tampered_note = copy.deepcopy(approved)
        tampered_note["safety_limits"]["distortion_note"] = "A different explanation."
        review_file.write_text(json.dumps(tampered_note, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "changed after the packet was built"):
            require_approval(review_dir)

        review_file.write_text(json.dumps(approved, indent=2) + "\n", encoding="utf-8")
        tampered_warnings = copy.deepcopy(approved)
        tampered_warnings["safety_limits"]["distortion_warnings"].append(
            "invented warning"
        )
        review_file.write_text(
            json.dumps(tampered_warnings, indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(RuntimeError, "distortion warnings differ"):
            require_approval(review_dir)

    def test_distortion_note_cannot_override_mirroring_or_clustered_controls(self):
        for kind, expected in (
            ("mirrored", "mirrored orientation"),
            ("clustered", "control triangle covers only"),
        ):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(RuntimeError, expected):
                    create_packet(
                        self._create_args(
                            points=self._make_hard_failure_points(kind),
                            review_dir=self.root / f"hard-{kind}-review",
                            allow_distortion=True,
                            distortion_note="This note must never bypass a hard gate.",
                        )
                    )


if __name__ == "__main__":
    unittest.main()
