#!/usr/bin/env python3

import sys
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from sanborn_georeference import (
    affine_diagnostics,
    affine_safety_warnings,
    require_expected_crs,
    require_target_location,
    target_location_diagnostics,
    target_location_warnings,
)
from sanborn_review import approval_token, require_approval


def controls(source_points, target_points):
    return [
        {
            "source_x": source_x,
            "source_line_gdal": source_y,
            "map_x": map_x,
            "map_y": map_y,
        }
        for (source_x, source_y), (map_x, map_y) in zip(source_points, target_points)
    ]


TILE_196_TARGETS = [
    (-9390422.77151954, 3997201.2984945206),
    (-9390325.255645605, 3997299.585330836),
    (-9390211.609577455, 3997188.8988798736),
]

TILE_236_SOURCES = [(835, 2219), (6128, 2692), (3028, 7418)]
TILE_236_TARGETS = [
    (-9392972.099178197, 3999051.4818662284),
    (-9392997.480022099, 3998732.6066058716),
    (-9393289.905192463, 3998918.82814558),
]
TILE_474_SOURCES = [(6452, 3565), (6345, 5592), (2652, 5056)]
TILE_474_TARGETS = [
    (-9391764.83930054, 3995400.81652537),
    (-9391811.2595282, 3995198.25152542),
    (-9392093.75868612, 3995262.59459438),
]


def padded_bbox(points, padding=10.0):
    x_values = [point[0] for point in points]
    y_values = [point[1] for point in points]
    return [
        min(x_values) - padding,
        min(y_values) - padding,
        max(x_values) + padding,
        max(y_values) + padding,
    ]


def centroid(points):
    return [
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    ]


class AffineSafetyTests(unittest.TestCase):
    def test_rejected_tile_196_is_caught(self):
        result = affine_diagnostics(
            controls([(785, 3220), (3560, 3460), (4030, 5740)], TILE_196_TARGETS),
            6665,
            7796,
        )
        self.assertGreater(result["scale_ratio"], 1.15)
        self.assertFalse(85 <= result["axis_angle_degrees"] <= 95)

    def test_corrected_tile_196_passes(self):
        result = affine_diagnostics(
            controls([(1254, 4152), (3546, 3539), (4490, 6200)], TILE_196_TARGETS),
            6665,
            7796,
        )
        self.assertLessEqual(result["scale_ratio"], 1.15)
        self.assertTrue(85 <= result["axis_angle_degrees"] <= 95)
        self.assertFalse(result["unexpected_mirroring"])

    def test_accepted_tile_236_passes_shape_and_location_checks(self):
        tile_controls = controls(TILE_236_SOURCES, TILE_236_TARGETS)
        result = affine_diagnostics(tile_controls, 6645, 7796)
        location = target_location_diagnostics(
            tile_controls,
            result,
            6645,
            7796,
            expected_target_bbox=padded_bbox(TILE_236_TARGETS),
            expected_target_seed=centroid(TILE_236_TARGETS),
        )
        result["target_location_check"] = location
        self.assertEqual(affine_safety_warnings(result), [])
        self.assertTrue(location["passed"])
        self.assertEqual(target_location_warnings(location), [])

    def test_accepted_tile_474_location_passes_without_hiding_existing_shape_warning(self):
        tile_controls = controls(TILE_474_SOURCES, TILE_474_TARGETS)
        result = affine_diagnostics(tile_controls, 6605, 7795)
        location = target_location_diagnostics(
            tile_controls,
            result,
            6605,
            7795,
            expected_target_bbox=padded_bbox(TILE_474_TARGETS),
            expected_target_seed=centroid(TILE_474_TARGETS),
        )
        result["target_location_check"] = location
        self.assertTrue(location["passed"])
        self.assertEqual(target_location_warnings(location), [])
        warnings = affine_safety_warnings(result)
        self.assertTrue(any("scale ratio" in warning for warning in warnings))
        self.assertTrue(any("axis angle" in warning for warning in warnings))

    def test_uniformly_translated_triplet_is_rejected_by_location_gate(self):
        translated_targets = [
            (target_x + 1000.0, target_y - 1000.0)
            for target_x, target_y in TILE_236_TARGETS
        ]
        accepted_controls = controls(TILE_236_SOURCES, TILE_236_TARGETS)
        translated_controls = controls(TILE_236_SOURCES, translated_targets)
        accepted = affine_diagnostics(accepted_controls, 6645, 7796)
        translated = affine_diagnostics(translated_controls, 6645, 7796)
        self.assertAlmostEqual(accepted["scale_ratio"], translated["scale_ratio"])
        self.assertAlmostEqual(
            accepted["axis_angle_degrees"], translated["axis_angle_degrees"]
        )
        self.assertEqual(affine_safety_warnings(translated), [])

        location = target_location_diagnostics(
            translated_controls,
            translated,
            6645,
            7796,
            expected_target_bbox=padded_bbox(TILE_236_TARGETS),
            expected_target_seed=centroid(TILE_236_TARGETS),
        )
        translated["target_location_check"] = location
        self.assertFalse(location["passed"])
        self.assertFalse(location["all_target_controls_inside_expected_bbox"])
        self.assertFalse(location["target_seed_within_tolerance"])
        self.assertGreater(location["target_seed_distance_to_footprint"], 0)
        with self.assertRaisesRegex(RuntimeError, "Expected target location gate failed"):
            require_target_location(location)
        self.assertTrue(
            any("expected target bounding box" in warning for warning in affine_safety_warnings(translated))
        )

    def test_location_diagnostics_are_recorded_when_gate_is_not_configured(self):
        tile_controls = controls(TILE_236_SOURCES, TILE_236_TARGETS)
        result = affine_diagnostics(tile_controls, 6645, 7796)
        location = target_location_diagnostics(tile_controls, result, 6645, 7796)
        self.assertFalse(location["enabled"])
        self.assertIsNone(location["passed"])
        self.assertEqual(len(location["transformed_source_footprint"]), 4)
        self.assertIsNone(location["expected_target_bbox"])
        self.assertIsNone(location["expected_target_seed"])

    def test_tile_154_candidate_triangle_passes(self):
        target_points = [
            (-9393166.206974294, 3996274.096014802),
            (-9392912.153632404, 3996021.13214878),
            (-9393165.99546726, 3996023.287801004),
        ]
        result = affine_diagnostics(
            controls([(2202, 1052), (6393, 5168), (2182, 5190)], target_points),
            6646,
            7795,
        )
        self.assertAlmostEqual(result["scale_ratio"], 1.0203434, places=5)
        self.assertAlmostEqual(result["axis_angle_degrees"], 88.8888, places=3)
        self.assertFalse(result["unexpected_mirroring"])

    def test_collinear_source_points_are_rejected(self):
        with self.assertRaises(RuntimeError):
            affine_diagnostics(
                controls(
                    [(0, 0), (10, 10), (20, 20)],
                    [(0, 0), (10, 10), (20, 20)],
                )
            )

    def test_clustered_controls_are_rejected_even_when_shape_is_square(self):
        result = affine_diagnostics(
            controls(
                [(100, 100), (120, 100), (100, 120)],
                [(0, 0), (20, 0), (0, -20)],
            ),
            10_000,
            10_000,
        )
        warnings = affine_safety_warnings(result)
        self.assertTrue(any("control triangle" in warning for warning in warnings))
        self.assertTrue(any("scan width" in warning for warning in warnings))
        self.assertTrue(any("scan height" in warning for warning in warnings))

    def test_almost_collinear_controls_are_rejected(self):
        source = [(0, 100), (5000, 100.001), (10000, 100.003)]
        target = [(x, -y) for x, y in source]
        result = affine_diagnostics(controls(source, target), 10_000, 10_000)
        warnings = affine_safety_warnings(result)
        self.assertTrue(any("control triangle" in warning for warning in warnings))
        self.assertTrue(any("scan height" in warning for warning in warnings))

    def test_wrong_crs_is_rejected_by_default(self):
        with self.assertRaises(RuntimeError):
            require_expected_crs("EPSG:4326")

    def test_approval_token_changes_when_an_input_file_changes(self):
        limits = {
            "max_scale_ratio": 1.15,
            "axis_angle_degrees": [85.0, 95.0],
            "min_triangle_coverage": 0.02,
            "min_x_span_fraction": 0.20,
            "min_y_span_fraction": 0.20,
            "expected_crs": "EPSG:3857",
        }
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "scan.jp2"
            points = Path(temp) / "controls.points"
            source.write_bytes(b"first scan")
            points.write_text("first controls", encoding="utf-8")
            first = approval_token(
                source,
                points,
                {"scale_ratio": 1.0},
                labels=["A", "B", "C"],
                target_crs="EPSG:3857",
                safety_limits=limits,
            )
            points.write_text("changed controls", encoding="utf-8")
            changed_points = approval_token(
                source,
                points,
                {"scale_ratio": 1.0},
                labels=["A", "B", "C"],
                target_crs="EPSG:3857",
                safety_limits=limits,
            )
            source.write_bytes(b"changed scan")
            changed_source = approval_token(
                source,
                points,
                {"scale_ratio": 1.0},
                labels=["A", "B", "C"],
                target_crs="EPSG:3857",
                safety_limits=limits,
            )
        self.assertNotEqual(first, changed_points)
        self.assertNotEqual(changed_points, changed_source)

    def test_legacy_approval_without_local_artifacts_is_rejected(self):
        limits = {
            "max_scale_ratio": 1.15,
            "axis_angle_degrees": [85.0, 95.0],
            "min_triangle_coverage": 0.02,
            "min_x_span_fraction": 0.20,
            "min_y_span_fraction": 0.20,
            "expected_crs": "EPSG:3857",
        }
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "scan.png"
            points = folder / "controls.points"
            Image.new("RGB", (1000, 1000), "white").save(source)
            point_text = (
                "#CRS: EPSG:3857\n"
                "mapX,mapY,sourceX,sourceY,enable\n"
                "0,0,100,-100,1\n"
                "800,0,900,-100,1\n"
                "0,-800,100,-900,1\n"
            )
            points.write_text(point_text, encoding="utf-8")
            diagnostic = affine_diagnostics(
                controls(
                    [(100, 100), (900, 100), (100, 900)],
                    [(0, 0), (800, 0), (0, -800)],
                ),
                1000,
                1000,
            )
            labels = ["A", "B", "C"]
            token = approval_token(
                source,
                points,
                diagnostic,
                labels=labels,
                target_crs="EPSG:3857",
                safety_limits=limits,
            )
            review = {
                "schema_version": 2,
                "source": {
                    "path": str(source),
                    "width": 1000,
                    "height": 1000,
                },
                "points": {
                    "path": str(points),
                    "controls": [{"label": label} for label in labels],
                },
                "safety_limits": limits,
                "approval_token": token,
            }
            approval = {
                "schema_version": 2,
                "approval_token": token,
                "geographic_verification": {
                    "reference_method": "qgis-osm-and-kauffman",
                    "note": "Independent street and complete grid checked",
                },
            }
            (folder / "review.json").write_text(
                json.dumps(review), encoding="utf-8"
            )
            (folder / "approval.json").write_text(
                json.dumps(approval), encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "unsupported schema"):
                require_approval(folder)


if __name__ == "__main__":
    unittest.main()
