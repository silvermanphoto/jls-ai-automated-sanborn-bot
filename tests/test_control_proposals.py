#!/usr/bin/env python3

import json
from pathlib import Path
import sys
import tempfile
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from sanborn_controls import (
    build_control_candidates,
    build_street_axes,
    load_aliases,
    name_core,
    osm_name_catalog,
    rank_triplets,
    register_reviewed_scan,
    resolve_ocr_names,
    write_points,
)
from sanborn_georeference import read_points
from sanborn_osm import import_osm


GRID_OSM = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="sanborn-control-test">
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


def label(text, normalized, x, y, orientation, confidence=95):
    return {
        "text": text,
        "name": text,
        "normalized": normalized,
        "source_x": x,
        "source_y": y,
        "orientation": orientation,
        "confidence": confidence,
        "strength": "weak-token",
    }


class ControlProposalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.osm = self.folder / "grid.osm"
        self.database = self.folder / "grid.sqlite3"
        self.aliases = self.folder / "aliases.json"
        self.osm.write_text(GRID_OSM, encoding="utf-8")
        import_osm(self.osm, self.database)
        self.aliases.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "aliases": [
                        {
                            "historic": "Butler Street",
                            "modern": "Jesse Hill Junior Drive",
                            "evidence": "Synthetic reviewed alias",
                            "status": "approved",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.ocr = {
            "source": {"width": 1000, "height": 1000},
            "street_labels": [
                label("Auburn", "AUBURN", 500, 200, "horizontal"),
                label("Edgewood", "EDGEWOOD", 500, 800, "horizontal"),
                label("Butler", "BUTLER", 200, 500, "vertical"),
                label("Fort", "FORT", 800, 500, "vertical"),
            ],
            "text_lines": [],
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_name_core_ignores_suffix_and_address_quadrant(self):
        self.assertEqual(name_core("Auburn Avenue Northeast"), "auburn")
        self.assertEqual(name_core("Fifth Street NE"), "5th")

    def test_reviewed_alias_and_exact_names_build_four_shared_controls(self):
        resolved = resolve_ocr_names(
            self.ocr,
            osm_name_catalog(self.database),
            load_aliases(self.aliases),
        )
        axes = build_street_axes(resolved)
        candidates = build_control_candidates(self.database, axes)
        labels = {candidate["label"] for candidate in candidates}
        self.assertEqual(len(candidates), 4)
        self.assertTrue(any("Butler" in value and "Auburn" in value for value in labels))
        self.assertTrue(any("Fort" in value and "Edgewood" in value for value in labels))

    def test_wide_three_control_proposal_exports_valid_qgis_points(self):
        resolved = resolve_ocr_names(
            self.ocr,
            osm_name_catalog(self.database),
            load_aliases(self.aliases),
        )
        candidates = build_control_candidates(self.database, build_street_axes(resolved))
        triplets = rank_triplets(candidates, 1000, 1000)
        self.assertTrue(triplets)
        self.assertEqual(triplets[0]["strict_safety_warnings"], [])
        by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
        selected = [by_id[value] for value in triplets[0]["candidate_ids"]]
        points = self.folder / "proposed.points"
        write_points(points, selected)
        crs, controls = read_points(points)
        self.assertEqual(crs, "EPSG:3857")
        self.assertEqual(len(controls), 3)
        self.assertTrue(all(float(control["source_y_qgis"]) < 0 for control in controls))

    def test_unapproved_alias_is_not_used(self):
        payload = json.loads(self.aliases.read_text(encoding="utf-8"))
        payload["aliases"][0]["status"] = "suggested"
        self.aliases.write_text(json.dumps(payload), encoding="utf-8")
        resolved = resolve_ocr_names(
            self.ocr,
            osm_name_catalog(self.database),
            load_aliases(self.aliases),
        )
        self.assertFalse(any(row["historic_core"] == "butler" for row in resolved))

    def test_index_seed_rejects_a_coherent_proposal_in_the_wrong_neighborhood(self):
        resolved = resolve_ocr_names(
            self.ocr,
            osm_name_catalog(self.database),
            load_aliases(self.aliases),
        )
        candidates = build_control_candidates(self.database, build_street_axes(resolved))
        baseline = rank_triplets(candidates, 1000, 1000)
        self.assertTrue(baseline)
        center_x, center_y = baseline[0]["target_centroid"]
        nearby = rank_triplets(
            candidates,
            1000,
            1000,
            expected_target_seed=(center_x, center_y),
            max_seed_distance=500,
        )
        self.assertTrue(nearby)
        self.assertLessEqual(nearby[0]["target_seed_distance"], 500)
        self.assertEqual(
            rank_triplets(
                candidates,
                1000,
                1000,
                expected_target_seed=(center_x + 10_000, center_y + 10_000),
                max_seed_distance=500,
            ),
            [],
        )

    def test_reviewed_scan_registration_transfers_pixels_without_copying_them(self):
        reviewed = np.full((900, 1100), 245, dtype=np.uint8)
        random = np.random.default_rng(486)
        for _ in range(350):
            x = int(random.integers(20, 1080))
            y = int(random.integers(20, 880))
            radius = int(random.integers(2, 10))
            color = int(random.integers(15, 190))
            cv2.circle(reviewed, (x, y), radius, color, -1)
        for index in range(12):
            cv2.putText(
                reviewed,
                f"STREET {index}",
                (40 + (index % 3) * 340, 80 + (index // 3) * 190),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.1,
                20,
                2,
                cv2.LINE_AA,
            )
        center = (reviewed.shape[1] / 2, reviewed.shape[0] / 2)
        affine = cv2.getRotationMatrix2D(center, 0.35, 1.0)
        affine[:, 2] += (72, 54)
        current = cv2.warpAffine(
            reviewed,
            affine,
            (1240, 1040),
            borderValue=255,
        )
        reviewed_path = self.folder / "reviewed.png"
        current_path = self.folder / "current.png"
        self.assertTrue(cv2.imwrite(str(reviewed_path), reviewed))
        self.assertTrue(cv2.imwrite(str(current_path), current))

        homography, evidence = register_reviewed_scan(reviewed_path, current_path)
        tested = np.float32([[[100, 100], [900, 150], [850, 760]]])
        expected = cv2.transform(tested, affine)
        actual = cv2.perspectiveTransform(tested, homography)
        errors = np.linalg.norm(actual[0] - expected[0], axis=1)

        self.assertLess(float(errors.max()), 2.0)
        self.assertGreater(evidence["inlier_count"], 100)
        self.assertLess(evidence["median_error_pixels"], 2.0)


if __name__ == "__main__":
    unittest.main()
