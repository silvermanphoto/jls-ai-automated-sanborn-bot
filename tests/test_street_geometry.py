#!/usr/bin/env python3

from pathlib import Path
import sys
import tempfile
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from sanborn_geometry import StreetGeometry, constant_axis, intersect_axes


class StreetGeometryTests(unittest.TestCase):
    def test_axis_intersection(self):
        diagonal = {"a": 0.2, "b": 1.0, "c": -500.0}
        vertical = constant_axis("vertical", 1000)
        x, y = intersect_axes(diagonal, vertical)
        self.assertAlmostEqual(x, 1000)
        self.assertAlmostEqual(y, 300)

    def test_paired_boundaries_recover_diagonal_street_center(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "diagonal.png"
            image = np.full((1000, 1200), 255, dtype=np.uint8)
            # Centerline y = -0.2x + 600, with two dark road boundaries.
            cv2.line(image, (0, 560), (1199, 320), 0, 4)
            cv2.line(image, (0, 640), (1199, 400), 0, 4)
            cv2.imwrite(str(path), image)
            geometry = StreetGeometry(path, 1200, 1000)
            axis = geometry.axis_for_labels(
                [
                    {
                        "source_x": 600,
                        "source_y": 480,
                        "ocr_confidence": 90,
                    }
                ],
                "horizontal",
            )
            self.assertEqual(axis["method"], "paired-road-boundaries")
            vertical = constant_axis("vertical", 900)
            x, y = intersect_axes(axis, vertical)
            self.assertAlmostEqual(x, 900, delta=3)
            self.assertAlmostEqual(y, 420, delta=5)


if __name__ == "__main__":
    unittest.main()
