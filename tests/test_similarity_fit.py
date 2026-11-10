#!/usr/bin/env python3
"""Joel's hard rule (2026-09-28): every sheet is fitted by rotation + uniform scale +
shift, least squares over all measured points, and judged by leave-one-out error."""

import math
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from sanborn_georeference import (  # noqa: E402
    fit_diagnostics,
    fit_safety_warnings,
    ground_scale_factor,
    leave_one_out_errors,
    solve_similarity,
)

ATLANTA_X, ATLANTA_Y = -9393000.0, 3996000.0


def controls(rows):
    return [
        {"source_x": sx, "source_line_gdal": sy, "map_x": mx, "map_y": my}
        for sx, sy, mx, my in rows
    ]


def transformed(points, scale, degrees, shift_x, shift_y):
    a = scale * math.cos(math.radians(degrees))
    b = scale * math.sin(math.radians(degrees))
    return [(x, y, a * x + b * y + shift_x, b * x - a * y + shift_y) for x, y in points]


def reference_fit(points):
    """Independent least squares, as in the ladder's simfit.py reference."""
    rows, values = [], []
    for sx, sy, mx, my in points:
        rows.append([sx, sy, 1, 0]); values.append(mx)
        rows.append([-sy, sx, 0, 1]); values.append(my)
    return np.linalg.lstsq(np.array(rows, float), np.array(values, float), rcond=None)[0]


class SimilarityFitTests(unittest.TestCase):
    def test_known_rotation_scale_and_shift_are_recovered_exactly(self):
        sources = [(120, 340), (5210, 410), (4980, 6120), (300, 5900), (2600, 3100)]
        points = transformed(sources, 0.061, 17.5, ATLANTA_X, ATLANTA_Y)
        a, b, c, d = solve_similarity(points)
        self.assertAlmostEqual(math.hypot(a, b), 0.061, places=12)
        self.assertAlmostEqual(math.degrees(math.atan2(b, a)), 17.5, places=9)
        self.assertAlmostEqual(c, ATLANTA_X, places=6)
        self.assertAlmostEqual(d, ATLANTA_Y, places=6)
        diagnostics = fit_diagnostics(controls(points), 6000, 7000)
        self.assertLess(diagnostics["fit_rms_ground_metres"], 1e-6)
        self.assertLess(diagnostics["leave_one_out_max_ground_metres"], 1e-6)
        self.assertEqual(diagnostics["geotransform"], [c, a, b, d, b, -a])

    def test_result_has_zero_skew_even_from_distorted_measurements(self):
        # A three-point affine through these points would be sheared; the
        # similarity fit cannot be.
        points = [
            (100, 100, ATLANTA_X, ATLANTA_Y),
            (900, 100, ATLANTA_X + 48, ATLANTA_Y),
            (100, 900, ATLANTA_X + 9, ATLANTA_Y - 40),
        ]
        diagnostics = fit_diagnostics(controls(points), 1000, 1000)
        a, b = diagnostics["matrix"][0]
        c, d = diagnostics["matrix"][1]
        self.assertEqual((c, d), (b, -a))
        self.assertAlmostEqual(a * b + c * d, 0.0, places=15)
        self.assertAlmostEqual(diagnostics["axis_angle_degrees"], 90.0, places=9)
        self.assertEqual(diagnostics["skew_degrees"], 0.0)
        self.assertEqual(diagnostics["scale_ratio"], 1.0)

    def test_least_squares_matches_an_independent_solver(self):
        points = transformed([(0, 0), (800, 30), (60, 900), (820, 870)], 0.05, -32.0, ATLANTA_X, ATLANTA_Y)
        points = [(sx, sy, mx + noise_x, my + noise_y) for (sx, sy, mx, my), (noise_x, noise_y)
                  in zip(points, [(0.7, -0.2), (-0.4, 0.9), (0.1, 0.3), (-0.6, -0.5)])]
        for ours, theirs in zip(solve_similarity(points), reference_fit(points)):
            self.assertAlmostEqual(ours, theirs, places=6)

    def test_leave_one_out_values_on_a_small_fixture(self):
        # Four corners of a 1000-pixel square at 0.05 units per pixel; the fourth
        # is measured 3 units east of where the other three put it.
        points = [
            (0, 0, 0.0, 0.0),
            (1000, 0, 50.0, 0.0),
            (0, 1000, 0.0, -50.0),
            (1000, 1000, 53.0, -50.0),
        ]
        errors = leave_one_out_errors(points)
        # Fitted to the three exact corners, the fourth misses by exactly 3 units.
        self.assertAlmostEqual(errors[3], 3.0, places=9)
        # The others, checked against the independent solver.
        for index, point in enumerate(points):
            others = points[:index] + points[index + 1:]
            a, b, c, d = reference_fit(others)
            expected = math.hypot(a * point[0] + b * point[1] + c - point[2],
                                  b * point[0] - a * point[1] + d - point[3])
            self.assertAlmostEqual(errors[index], expected, places=9)
        diagnostics = fit_diagnostics(controls([(sx, sy, ATLANTA_X + mx, ATLANTA_Y + my)
                                                for sx, sy, mx, my in points]))
        ground = ground_scale_factor(ATLANTA_Y - 25.0)
        self.assertAlmostEqual(diagnostics["leave_one_out_ground_metres"][3], 3.0 * ground, places=6)
        self.assertAlmostEqual(
            diagnostics["leave_one_out_rms_ground_metres"],
            math.sqrt(sum((e * ground) ** 2 for e in errors) / 4), places=6)
        self.assertEqual(diagnostics["leave_one_out_worst_index"], errors.index(max(errors)))

    def test_two_fits_and_two_checks_make_a_fittable_sheet(self):
        # Roles do not matter to the fit: any four measured points are fitted together.
        points = transformed([(150, 200), (4800, 260), (2500, 3000), (900, 5600)], 0.055, 4.0, ATLANTA_X, ATLANTA_Y)
        diagnostics = fit_diagnostics(controls(points), 5000, 6000)
        self.assertEqual(diagnostics["point_count"], 4)
        self.assertEqual(fit_safety_warnings(diagnostics), [])

    def test_scale_sanity(self):
        sources = [(100, 100), (4000, 150), (200, 5000)]
        for units_per_pixel, plausible in ((0.03, False), (0.06, True), (0.09, True), (0.1, False)):
            with self.subTest(units_per_pixel=units_per_pixel):
                diagnostics = fit_diagnostics(
                    controls(transformed(sources, units_per_pixel, 0.0, ATLANTA_X, ATLANTA_Y)), 5000, 6000)
                warnings = fit_safety_warnings(diagnostics)
                self.assertEqual(not any("plausible" in w for w in warnings), plausible, warnings)
        # Ground metres, not Web Mercator units, set the range: 0.06 units is ~0.05 m.
        self.assertAlmostEqual(0.06 * ground_scale_factor(ATLANTA_Y), 0.0499, places=3)

    def test_leave_one_out_limit_is_fifteen_metres(self):
        base = transformed([(0, 0), (1000, 0), (0, 1000), (1000, 1000)], 0.06, 0.0, ATLANTA_X, ATLANTA_Y)
        for shift, passes in ((10.0, True), (60.0, False)):
            with self.subTest(shift=shift):
                points = list(base)
                sx, sy, mx, my = points[3]
                points[3] = (sx, sy, mx + shift, my)
                warnings = fit_safety_warnings(fit_diagnostics(controls(points), 1000, 1000))
                self.assertEqual(not any("leave-one-out" in w for w in warnings), passes, warnings)

    def test_fewer_than_three_points_are_refused(self):
        with self.assertRaisesRegex(RuntimeError, "At least 3"):
            fit_diagnostics(controls([(0, 0, 0, 0), (100, 0, 5, 0)]))


class PrecisionOverrideTests(unittest.TestCase):
    """Joel's override (2026-09-30) waives the 15 m limit for 77, 174 and 178 only."""

    def _forty_metre_fit(self):
        rows = [
            (0, 0, ATLANTA_X, ATLANTA_Y),
            (800, 0, ATLANTA_X + 48, ATLANTA_Y),
            (0, 800, ATLANTA_X, ATLANTA_Y - 48),
            (800, 800, ATLANTA_X + 48 + 68, ATLANTA_Y - 48),
        ]
        diagnostics = fit_diagnostics(controls(rows), 1000, 1000)
        self.assertGreater(diagnostics["leave_one_out_rms_ground_metres"], 38.0)
        return diagnostics

    def test_tile_77_passes_a_forty_metre_error_and_tile_78_does_not(self):
        from sanborn_georeference import fit_safety_limits
        from sanborn_placement_policy import precision_override

        diagnostics = self._forty_metre_fit()
        override = precision_override(77)
        self.assertEqual(fit_safety_warnings(diagnostics, precision_override=override), [])
        self.assertEqual(fit_safety_limits(precision_override=override)["precision_override"]["tile"], 77)
        for tile in (174, 178):
            self.assertIsNotNone(precision_override(tile))
        self.assertIsNone(precision_override(78))
        warnings = fit_safety_warnings(diagnostics, precision_override=precision_override(78))
        self.assertTrue(any("leave-one-out" in warning for warning in warnings))
        self.assertNotIn("precision_override", fit_safety_limits())

    def test_override_keeps_the_scale_gate(self):
        from sanborn_placement_policy import precision_override

        rows = [(0, 0, ATLANTA_X, ATLANTA_Y), (800, 0, ATLANTA_X + 800, ATLANTA_Y),
                (0, 800, ATLANTA_X, ATLANTA_Y - 800)]
        warnings = fit_safety_warnings(
            fit_diagnostics(controls(rows), 1000, 1000), precision_override=precision_override(77)
        )
        self.assertTrue(any("plausible" in warning for warning in warnings))


class StalePartialTests(unittest.TestCase):
    def test_only_partials_from_finished_processes_are_swept(self):
        import os
        import subprocess
        import tempfile

        from sanborn_georeference import sweep_stale_partials

        dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                              capture_output=True, text=True, check=True)
        dead_pid = int(dead.stdout)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "Sanborn 1911 -- Tile 154_georeferenced.tif"
            ledger = output.with_suffix(".georef.json")
            stale = output.with_name(f".{output.stem}.partial-{dead_pid}.tif")
            stale_ledger = ledger.with_name(f".{ledger.name}.partial-{dead_pid}")
            live = output.with_name(f".{output.stem}.partial-{os.getppid()}.tif")
            other = output.with_name(".Sanborn 1911 -- Tile 155_georeferenced.partial-1.tif")
            for path in (stale, stale_ledger, live, other):
                path.write_bytes(b"partial")
            removed = sweep_stale_partials(output, ledger)
            self.assertEqual(sorted(removed), sorted([stale, stale_ledger]))
            self.assertTrue(live.exists())
            self.assertTrue(other.exists())


if __name__ == "__main__":
    unittest.main()
