#!/usr/bin/env python3

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from sanborn_ocr import (
    classify_printed_tile_observations,
    deduplicate_lines,
    extract_street_phrases,
    normalize_name,
    normalize_printed_tile_text,
    parse_tesseract_tsv,
    rotated_to_original,
)


TSV = """level	page_num	block_num	par_num	line_num	word_num	left	top	width	height	conf	text
5	1	1	1	1	1	10	20	80	20	91.2	AUBURN
5	1	1	1	1	2	100	20	45	20	88.4	AVE.
5	1	1	1	2	1	200	80	50	20	5.0	NOISE
"""


class SpatialOcrTests(unittest.TestCase):
    def test_tesseract_words_group_into_lines(self):
        lines = parse_tesseract_tsv(TSV, minimum_confidence=20)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "AUBURN AVE.")
        self.assertAlmostEqual(lines[0]["confidence"], 89.8)

    def test_street_suffix_is_canonicalized(self):
        phrases = extract_street_phrases("AUBURN AVE.")
        self.assertEqual(phrases[0]["normalized"], "AUBURN AVENUE")
        self.assertEqual(phrases[0]["strength"], "strong")
        self.assertEqual(normalize_name("N. Butler St."), "NORTH BUTLER STREET")

    def test_suffixless_short_label_is_preserved_as_weak(self):
        phrases = extract_street_phrases("EDGEWOOD")
        self.assertEqual(phrases[0]["normalized"], "EDGEWOOD")
        self.assertEqual(phrases[0]["strength"], "weak")

    def test_right_angle_coordinate_mapping(self):
        width, height = 1000, 800
        original = (250, 300)
        rotated_90 = (original[1], width - original[0])
        rotated_270 = (height - original[1], original[0])
        self.assertEqual(rotated_to_original(*rotated_90, width, height, 90), original)
        self.assertEqual(rotated_to_original(*rotated_270, width, height, 270), original)
        self.assertEqual(
            rotated_to_original(width - original[0], height - original[1], width, height, 180),
            original,
        )

    def test_duplicate_rotations_keep_best_confidence(self):
        first = {
            "text": "Auburn Ave",
            "normalized_text": "AUBURN AVENUE",
            "confidence": 80.0,
            "source_x": 100.0,
            "source_y": 200.0,
        }
        better = dict(first, confidence=95.0, source_x=102.0)
        distinct = dict(first, source_x=800.0)
        result = deduplicate_lines([first, better, distinct], distance_pixels=50)
        self.assertEqual(len(result), 2)
        self.assertIn(95.0, [line["confidence"] for line in result])

    def test_printed_tile_154_passes_in_top_left_title_corner(self):
        observations = [
            {
                "text": "No. 154",
                "confidence": 1.0,
                "x": 0.0341,
                "y": 0.9491,
                "width": 0.0648,
                "height": 0.03198,
            }
        ]
        result = classify_printed_tile_observations(observations, 154)
        self.assertTrue(result["seen"])
        self.assertEqual(len(result["accepted_observations"]), 1)
        self.assertEqual(normalize_printed_tile_text("No. 154"), "154")

    def test_printed_tile_236_passes_in_top_right_title_corner(self):
        observations = [
            {
                "text": "236",
                "confidence": 1.0,
                "x": 0.8995,
                "y": 0.9462,
                "width": 0.07155,
                "height": 0.03343,
            }
        ]
        result = classify_printed_tile_observations(observations, 236)
        self.assertTrue(result["seen"])

    def test_center_house_number_cannot_prove_tile_identity(self):
        observations = [
            {
                "text": "154",
                "confidence": 0.99,
                "x": 0.45,
                "y": 0.94,
                "width": 0.04,
                "height": 0.03,
            }
        ]
        result = classify_printed_tile_observations(observations, 154)
        self.assertFalse(result["seen"])
        self.assertIn(
            "outside-left-or-right-title-corner",
            result["observations"][0]["rejection_reasons"],
        )

    def test_lower_right_number_cannot_prove_tile_identity(self):
        observations = [
            {
                "text": "154",
                "confidence": 0.99,
                "x": 0.90,
                "y": 0.20,
                "width": 0.05,
                "height": 0.03,
            }
        ]
        result = classify_printed_tile_observations(observations, 154)
        self.assertFalse(result["seen"])
        self.assertIn(
            "outside-top-title-strip",
            result["observations"][0]["rejection_reasons"],
        )

    def test_adjacent_tile_number_cannot_prove_expected_tile(self):
        observations = [
            {
                "text": "155",
                "confidence": 1.0,
                "x": 0.03,
                "y": 0.95,
                "width": 0.06,
                "height": 0.03,
            }
        ]
        result = classify_printed_tile_observations(observations, 154)
        self.assertFalse(result["seen"])
        self.assertIn(
            "not-exact-expected-digits",
            result["observations"][0]["rejection_reasons"],
        )


if __name__ == "__main__":
    unittest.main()
