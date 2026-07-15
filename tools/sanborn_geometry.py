#!/usr/bin/env python3
"""Estimate Sanborn street centerlines from OCR labels and local image geometry."""

from __future__ import annotations

import math
from pathlib import Path
import statistics

import cv2


def constant_axis(orientation: str, coordinate: float) -> dict:
    if orientation == "horizontal":
        return {
            "a": 0.0,
            "b": 1.0,
            "c": -float(coordinate),
            "angle_degrees": 0.0,
            "method": "ocr-label-constant",
            "support": 1,
        }
    return {
        "a": 1.0,
        "b": 0.0,
        "c": -float(coordinate),
        "angle_degrees": 90.0,
        "method": "ocr-label-constant",
        "support": 1,
    }


def intersect_axes(first: dict, second: dict) -> tuple[float, float]:
    determinant = first["a"] * second["b"] - second["a"] * first["b"]
    if abs(determinant) < 1e-12:
        raise RuntimeError("Proposed source street axes are parallel.")
    x = (first["b"] * second["c"] - second["b"] * first["c"]) / determinant
    y = (first["c"] * second["a"] - second["c"] * first["a"]) / determinant
    return float(x), float(y)


class StreetGeometry:
    """Precompute long ink lines once, then estimate several street axes cheaply."""

    def __init__(
        self,
        preview: Path,
        source_width: int,
        source_height: int,
    ) -> None:
        image = cv2.imread(str(preview), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise RuntimeError(f"Could not read OCR preview for street geometry: {preview}")
        self.preview_width = int(image.shape[1])
        self.preview_height = int(image.shape[0])
        self.source_width = int(source_width)
        self.source_height = int(source_height)
        self.scale_x = self.source_width / self.preview_width
        self.scale_y = self.source_height / self.preview_height
        blurred = cv2.GaussianBlur(image, (3, 3), 0)
        edges = cv2.Canny(blurred, 55, 150, apertureSize=3)
        raw = cv2.HoughLinesP(
            edges,
            1,
            math.pi / 360,
            threshold=max(45, self.preview_width // 28),
            minLineLength=max(70, self.preview_width // 13),
            maxLineGap=max(12, self.preview_width // 100),
        )
        self.segments: list[tuple[float, float, float, float, float]] = []
        if raw is not None:
            for row in raw:
                x1, y1, x2, y2 = map(float, row[0])
                x1 *= self.scale_x
                x2 *= self.scale_x
                y1 *= self.scale_y
                y2 *= self.scale_y
                length = math.hypot(x2 - x1, y2 - y1)
                self.segments.append((x1, y1, x2, y2, length))

    @staticmethod
    def _fit_labels(labels: list[dict], orientation: str) -> dict | None:
        if len(labels) < 2:
            return None
        if orientation == "horizontal":
            independent = [float(label["source_x"]) for label in labels]
            dependent = [float(label["source_y"]) for label in labels]
            needed_span = 0.15 * max(independent)
        else:
            independent = [float(label["source_y"]) for label in labels]
            dependent = [float(label["source_x"]) for label in labels]
            needed_span = 0.15 * max(independent)
        if max(independent) - min(independent) < needed_span:
            return None
        mean_i = statistics.fmean(independent)
        mean_d = statistics.fmean(dependent)
        denominator = sum((value - mean_i) ** 2 for value in independent)
        if denominator <= 0:
            return None
        slope = sum(
            (x - mean_i) * (y - mean_d) for x, y in zip(independent, dependent)
        ) / denominator
        intercept = mean_d - slope * mean_i
        if orientation == "horizontal":
            return {
                "a": -slope,
                "b": 1.0,
                "c": -intercept,
                "angle_degrees": math.degrees(math.atan2(slope, 1.0)),
                "method": "multiple-ocr-labels",
                "support": len(labels),
            }
        return {
            "a": 1.0,
            "b": -slope,
            "c": -intercept,
            "angle_degrees": 90.0 - math.degrees(math.atan2(slope, 1.0)),
            "method": "multiple-ocr-labels",
            "support": len(labels),
        }

    def axis_for_labels(self, labels: list[dict], orientation: str) -> dict:
        if not labels:
            raise RuntimeError("Cannot estimate a street axis without an OCR label.")
        fitted = self._fit_labels(labels, orientation)
        if fitted is not None:
            return fitted
        best = max(labels, key=lambda label: float(label.get("ocr_confidence", 0)))
        label_x = float(best["source_x"])
        label_y = float(best["source_y"])
        candidates: list[dict] = []
        for x1, y1, x2, y2, length in self.segments:
            if orientation == "horizontal":
                if abs(x2 - x1) < 1e-6:
                    continue
                slope = (y2 - y1) / (x2 - x1)
                if abs(slope) > 0.65:
                    continue
                intercept = y1 - slope * x1
                value = slope * label_x + intercept
                delta = value - label_y
                angle = math.degrees(math.atan2(slope, 1.0))
            else:
                if abs(y2 - y1) < 1e-6:
                    continue
                slope = (x2 - x1) / (y2 - y1)
                if abs(slope) > 0.65:
                    continue
                intercept = x1 - slope * y1
                value = slope * label_y + intercept
                delta = value - label_x
                angle = 90.0 - math.degrees(math.atan2(slope, 1.0))
            if abs(delta) > 0.045 * max(self.source_width, self.source_height):
                continue
            candidates.append(
                {
                    "slope": slope,
                    "intercept": intercept,
                    "delta": delta,
                    "length": length,
                    "angle": angle,
                }
            )

        best_pair: tuple[float, dict, dict] | None = None
        for first_index, first in enumerate(candidates):
            for second in candidates[first_index + 1 :]:
                if first["delta"] * second["delta"] >= 0:
                    continue
                if abs(first["slope"] - second["slope"]) > 0.10:
                    continue
                separation = abs(first["delta"] - second["delta"])
                # Sanborn street corridors can be broad relative to a cropped
                # sheet.  This limit still excludes unrelated distant lines,
                # while allowing the two curb/building-front edges of a road.
                if not 10 <= separation <= 0.08 * max(self.source_width, self.source_height):
                    continue
                balance = abs(abs(first["delta"]) - abs(second["delta"]))
                score = (
                    balance
                    + abs(first["slope"] - second["slope"]) * 500
                    + 2000 / max(50, first["length"])
                    + 2000 / max(50, second["length"])
                )
                if best_pair is None or score < best_pair[0]:
                    best_pair = (score, first, second)

        if best_pair is None:
            coordinate = label_y if orientation == "horizontal" else label_x
            return constant_axis(orientation, coordinate)
        _, first, second = best_pair
        slope = (first["slope"] + second["slope"]) / 2.0
        intercept = (first["intercept"] + second["intercept"]) / 2.0
        if orientation == "horizontal":
            angle = math.degrees(math.atan2(slope, 1.0))
            # A centered horizontal OCR label is generally more precise than
            # distant building-front lines.  Invoke the image geometry only
            # when it establishes a meaningful diagonal.
            if abs(angle) < 3.0:
                return constant_axis(orientation, label_y)
            return {
                "a": -slope,
                "b": 1.0,
                "c": -intercept,
                "angle_degrees": angle,
                "method": "paired-road-boundaries",
                "support": 2,
                "boundary_deltas": [first["delta"], second["delta"]],
            }
        angle = 90.0 - math.degrees(math.atan2(slope, 1.0))
        if abs(angle - 90.0) < 3.0:
            return constant_axis(orientation, label_x)
        return {
            "a": 1.0,
            "b": -slope,
            "c": -intercept,
            "angle_degrees": angle,
            "method": "paired-road-boundaries",
            "support": 2,
            "boundary_deltas": [first["delta"], second["delta"]],
        }
