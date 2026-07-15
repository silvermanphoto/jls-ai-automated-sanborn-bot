#!/usr/bin/env python3
"""Create and verify one georeferenced Sanborn GeoTIFF from a QGIS points file.

This intentionally automates only the deterministic part of the workflow. Street
identity, control-point choice, and the OSM/Kauffman visual judgment remain human
or agent decisions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


SOURCE_METADATA_KEY = "SANBORN_SOURCE_SHA256"
POINTS_METADATA_KEY = "SANBORN_POINTS_SHA256"
AFFINE_METADATA_KEY = "SANBORN_AFFINE_SIGNATURE"
PIPELINE_METADATA_KEY = "SANBORN_PIPELINE"
PIPELINE_METADATA_VALUE = "local-first-affine-v2"
_SHA256_CACHE: dict[tuple[str, int, int, int, int, int], str] = {}


def fail(message: str) -> "NoReturn":
    raise RuntimeError(message)


def sha256(path: Path) -> str:
    resolved = path.expanduser().resolve()
    before = resolved.stat()
    key = (
        str(resolved),
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    cached = _SHA256_CACHE.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    after = resolved.stat()
    after_key = (
        str(resolved),
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if after_key != key:
        fail(f"File changed while its SHA-256 was being read: {resolved}")
    value = digest.hexdigest()
    _SHA256_CACHE[key] = value
    return value


def affine_provenance_signature(
    source_sha256: str,
    points_sha256: str,
    target_crs: str,
    diagnostics: dict[str, object],
    safety_limits: dict[str, object],
) -> str:
    """Bind the raster itself to the exact inputs and verified affine decision."""
    payload = {
        "source_sha256": source_sha256,
        "points_sha256": points_sha256,
        "target_crs": target_crs.strip().upper(),
        "diagnostics": diagnostics,
        "safety_limits": safety_limits,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, check=True, env=env)


def capture_json(command: list[str], *, env: dict[str, str] | None = None) -> dict:
    result = subprocess.run(
        command,
        check=True,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return json.loads(result.stdout)


def require_program(name: str) -> str:
    found = shutil.which(name)
    if not found:
        fail(f"Required map-processing program is unavailable: {name}")
    return found


def read_points(path: Path) -> tuple[str, list[dict[str, float | str]]]:
    text = path.read_text(encoding="utf-8-sig")
    crs_match = re.search(r"^#CRS:\s*(\S+)\s*$", text, re.MULTILINE)
    if not crs_match:
        fail("The points file does not declare a target CRS on a #CRS line.")
    target_crs = crs_match.group(1)

    table = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    reader = csv.DictReader(io.StringIO(table))
    required = {"mapX", "mapY", "sourceX", "sourceY", "enable"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        fail("The points file is missing one or more required QGIS columns.")

    controls: list[dict[str, float | str]] = []
    for row in reader:
        if str(row["enable"]).strip() not in {"1", "true", "True"}:
            continue
        source_y = float(row["sourceY"])
        if source_y > 0:
            fail(
                "An enabled sourceY is positive. QGIS points normally store image "
                "line coordinates as negative values; check the points before warping."
            )
        controls.append(
            {
                "map_x": float(row["mapX"]),
                "map_y": float(row["mapY"]),
                "source_x": float(row["sourceX"]),
                "source_y_qgis": source_y,
                "source_line_gdal": -source_y,
            }
        )

    if len(controls) != 3:
        fail(f"Exactly three enabled controls are required; found {len(controls)}.")
    return target_crs, controls


def _solve_three(rows: list[list[float]], values: list[float]) -> list[float]:
    """Solve a 3 by 3 system without adding a heavy numerical dependency."""
    matrix = [list(row) + [value] for row, value in zip(rows, values)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(matrix[row][column]))
        if abs(matrix[pivot][column]) < 1e-12:
            fail("The three source controls are collinear or duplicated.")
        matrix[column], matrix[pivot] = matrix[pivot], matrix[column]
        divisor = matrix[column][column]
        matrix[column] = [value / divisor for value in matrix[column]]
        for row in range(3):
            if row == column:
                continue
            factor = matrix[row][column]
            matrix[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(matrix[row], matrix[column])
            ]
    return [matrix[row][3] for row in range(3)]


def triangle_area(points: list[tuple[float, float]]) -> float:
    (x1, y1), (x2, y2), (x3, y3) = points
    return abs(
        x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2)
    ) / 2.0


def affine_diagnostics(
    controls: list[dict[str, float | str]],
    source_width: int | None = None,
    source_height: int | None = None,
) -> dict[str, float | bool | list[list[float]]]:
    """Measure the affine shape before GDAL can hide bad controls with zero residual."""
    source_points = [
        (float(control["source_x"]), float(control["source_line_gdal"]))
        for control in controls
    ]
    target_points = [
        (float(control["map_x"]), float(control["map_y"]))
        for control in controls
    ]
    rows = [[x, y, 1.0] for x, y in source_points]
    x_coefficients = _solve_three(rows, [point[0] for point in target_points])
    y_coefficients = _solve_three(rows, [point[1] for point in target_points])
    a, b = x_coefficients[0], x_coefficients[1]
    d, e = y_coefficients[0], y_coefficients[1]

    # Singular values of the 2 by 2 linear component from eigenvalues of A^T A.
    ata_00 = a * a + d * d
    ata_01 = a * b + d * e
    ata_11 = b * b + e * e
    trace = ata_00 + ata_11
    discriminant = max(0.0, trace * trace - 4.0 * (ata_00 * ata_11 - ata_01 * ata_01))
    eigen_high = (trace + math.sqrt(discriminant)) / 2.0
    eigen_low = (trace - math.sqrt(discriminant)) / 2.0
    scale_high = math.sqrt(max(0.0, eigen_high))
    scale_low = math.sqrt(max(0.0, eigen_low))
    if scale_low < 1e-15:
        fail("The affine transform collapses one dimension.")

    column_x = (a, d)
    column_y = (b, e)
    cosine = (
        column_x[0] * column_y[0] + column_x[1] * column_y[1]
    ) / (math.hypot(*column_x) * math.hypot(*column_y))
    axis_angle = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
    determinant = a * e - b * d
    source_area = triangle_area(source_points)
    target_area = triangle_area(target_points)
    coverage = None
    x_span_fraction = None
    y_span_fraction = None
    if source_width and source_height:
        coverage = source_area / float(source_width * source_height)
        source_x_values = [point[0] for point in source_points]
        source_y_values = [point[1] for point in source_points]
        x_span_fraction = (max(source_x_values) - min(source_x_values)) / source_width
        y_span_fraction = (max(source_y_values) - min(source_y_values)) / source_height

    return {
        "matrix": [[a, b], [d, e]],
        "offset": [x_coefficients[2], y_coefficients[2]],
        "determinant": determinant,
        "unexpected_mirroring": determinant >= 0,
        "singular_scales": [scale_high, scale_low],
        "scale_ratio": scale_high / scale_low,
        "axis_angle_degrees": axis_angle,
        "source_triangle_area_pixels2": source_area,
        "target_triangle_area_crs2": target_area,
        "source_triangle_coverage": coverage,
        "source_x_span_fraction": x_span_fraction,
        "source_y_span_fraction": y_span_fraction,
    }


def _transform_point(
    diagnostics: dict[str, object], source_x: float, source_y: float
) -> tuple[float, float]:
    matrix = diagnostics["matrix"]
    offset = diagnostics["offset"]
    if not isinstance(matrix, list) or not isinstance(offset, list):
        fail("Affine diagnostics are missing their matrix or offset.")
    return (
        float(matrix[0][0]) * source_x
        + float(matrix[0][1]) * source_y
        + float(offset[0]),
        float(matrix[1][0]) * source_x
        + float(matrix[1][1]) * source_y
        + float(offset[1]),
    )


def _point_to_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    position = (
        (point[0] - start[0]) * dx + (point[1] - start[1]) * dy
    ) / length_squared
    position = max(0.0, min(1.0, position))
    nearest = (start[0] + position * dx, start[1] + position * dy)
    return math.hypot(point[0] - nearest[0], point[1] - nearest[1])


def _point_in_convex_polygon(
    point: tuple[float, float], polygon: list[tuple[float, float]]
) -> bool:
    signs: list[bool] = []
    for start, end in zip(polygon, polygon[1:] + polygon[:1]):
        cross = (end[0] - start[0]) * (point[1] - start[1]) - (
            end[1] - start[1]
        ) * (point[0] - start[0])
        if abs(cross) > 1e-9:
            signs.append(cross > 0)
    return not signs or all(sign == signs[0] for sign in signs)


def _distance_to_polygon(
    point: tuple[float, float], polygon: list[tuple[float, float]]
) -> float:
    if _point_in_convex_polygon(point, polygon):
        return 0.0
    return min(
        _point_to_segment_distance(point, start, end)
        for start, end in zip(polygon, polygon[1:] + polygon[:1])
    )


def target_location_diagnostics(
    controls: list[dict[str, float | str]],
    affine: dict[str, object],
    source_width: int,
    source_height: int,
    *,
    expected_target_bbox: list[float] | tuple[float, float, float, float] | None = None,
    expected_target_seed: list[float] | tuple[float, float] | None = None,
    max_target_seed_distance: float = 0.0,
) -> dict[str, object]:
    """Check that a plausible affine shape is also in the expected neighborhood.

    The optional bounding box is a permitted envelope for all three target
    controls. The optional seed is an independently known point that should lie
    inside the transformed scan footprint (or no farther away than the explicit
    allowance). These checks catch a perfectly coherent triplet copied to the
    wrong Atlanta neighborhood, which scale and angle checks cannot detect.
    """
    if source_width <= 0 or source_height <= 0:
        fail("Source dimensions must be positive for the target-location check.")
    if max_target_seed_distance < 0:
        fail("Maximum target-seed distance cannot be negative.")

    target_points = [
        (float(control["map_x"]), float(control["map_y"])) for control in controls
    ]
    target_x = [point[0] for point in target_points]
    target_y = [point[1] for point in target_points]
    control_bbox = [min(target_x), min(target_y), max(target_x), max(target_y)]
    control_centroid = [
        sum(target_x) / len(target_x),
        sum(target_y) / len(target_y),
    ]

    source_corners = [
        (0.0, 0.0),
        (float(source_width), 0.0),
        (float(source_width), float(source_height)),
        (0.0, float(source_height)),
    ]
    footprint = [_transform_point(affine, x, y) for x, y in source_corners]
    footprint_x = [point[0] for point in footprint]
    footprint_y = [point[1] for point in footprint]

    bbox_value = None
    controls_inside_bbox = None
    if expected_target_bbox is not None:
        if len(expected_target_bbox) != 4:
            fail("Expected target bounding box must have four coordinates.")
        min_x, min_y, max_x, max_y = (float(value) for value in expected_target_bbox)
        if not (min_x < max_x and min_y < max_y):
            fail("Expected target bounding box must be MIN_X MIN_Y MAX_X MAX_Y.")
        bbox_value = [min_x, min_y, max_x, max_y]
        controls_inside_bbox = all(
            min_x <= x <= max_x and min_y <= y <= max_y for x, y in target_points
        )

    seed_value = None
    seed_distance = None
    seed_within_tolerance = None
    if expected_target_seed is not None:
        if len(expected_target_seed) != 2:
            fail("Expected target seed must have X and Y coordinates.")
        seed = (float(expected_target_seed[0]), float(expected_target_seed[1]))
        seed_value = [seed[0], seed[1]]
        seed_distance = _distance_to_polygon(seed, footprint)
        seed_within_tolerance = seed_distance <= max_target_seed_distance + 1e-9

    enabled = expected_target_bbox is not None or expected_target_seed is not None
    passed_checks = [
        result
        for result in (controls_inside_bbox, seed_within_tolerance)
        if result is not None
    ]
    return {
        "enabled": enabled,
        "passed": all(passed_checks) if enabled else None,
        "target_control_bbox": control_bbox,
        "target_control_centroid": control_centroid,
        "transformed_source_footprint": [[x, y] for x, y in footprint],
        "transformed_source_footprint_bbox": [
            min(footprint_x),
            min(footprint_y),
            max(footprint_x),
            max(footprint_y),
        ],
        "expected_target_bbox": bbox_value,
        "all_target_controls_inside_expected_bbox": controls_inside_bbox,
        "expected_target_seed": seed_value,
        "target_seed_distance_to_footprint": seed_distance,
        "max_target_seed_distance": (
            max_target_seed_distance if expected_target_seed is not None else None
        ),
        "target_seed_within_tolerance": seed_within_tolerance,
    }


def target_location_warnings(location: dict[str, object]) -> list[str]:
    """Return hard location failures separately from overridable shape warnings."""
    warnings: list[str] = []
    if location.get("all_target_controls_inside_expected_bbox") is False:
        warnings.append(
            "one or more target controls lie outside the expected target bounding box"
        )
    if location.get("target_seed_within_tolerance") is False:
        warnings.append(
            "expected target seed is "
            f"{float(location['target_seed_distance_to_footprint']):.3f} CRS units "
            "from the transformed scan footprint; maximum is "
            f"{float(location['max_target_seed_distance']):.3f}"
        )
    return warnings


def require_target_location(location: dict[str, object]) -> None:
    """Reject a wrong-neighborhood transform even if distortion is overridden."""
    warnings = target_location_warnings(location)
    if warnings:
        fail("Expected target location gate failed: " + "; ".join(warnings))


def affine_safety_warnings(
    diagnostics: dict[str, object],
    *,
    max_scale_ratio: float = 1.15,
    min_axis_angle: float = 85.0,
    max_axis_angle: float = 95.0,
    min_triangle_coverage: float = 0.02,
    min_x_span_fraction: float = 0.20,
    min_y_span_fraction: float = 0.20,
) -> list[str]:
    """Return every reason these three controls are unsafe for a final affine warp."""
    warnings: list[str] = []
    if float(diagnostics["scale_ratio"]) > max_scale_ratio:
        warnings.append(
            f"scale ratio {diagnostics['scale_ratio']:.4f} exceeds {max_scale_ratio:.4f}"
        )
    axis_angle = float(diagnostics["axis_angle_degrees"])
    if not min_axis_angle <= axis_angle <= max_axis_angle:
        warnings.append(
            f"axis angle {axis_angle:.3f} is outside "
            f"{min_axis_angle:.1f}-{max_axis_angle:.1f} degrees"
        )
    if bool(diagnostics["unexpected_mirroring"]):
        warnings.append("the transform has an unexpected mirrored orientation")

    coverage = diagnostics.get("source_triangle_coverage")
    if coverage is not None and float(coverage) < min_triangle_coverage:
        warnings.append(
            f"control triangle covers only {float(coverage) * 100:.2f}% of the scan; "
            f"minimum is {min_triangle_coverage * 100:.2f}%"
        )
    x_span = diagnostics.get("source_x_span_fraction")
    if x_span is not None and float(x_span) < min_x_span_fraction:
        warnings.append(
            f"controls span only {float(x_span) * 100:.2f}% of scan width; "
            f"minimum is {min_x_span_fraction * 100:.2f}%"
        )
    y_span = diagnostics.get("source_y_span_fraction")
    if y_span is not None and float(y_span) < min_y_span_fraction:
        warnings.append(
            f"controls span only {float(y_span) * 100:.2f}% of scan height; "
            f"minimum is {min_y_span_fraction * 100:.2f}%"
        )
    location = diagnostics.get("target_location_check")
    if isinstance(location, dict):
        warnings.extend(target_location_warnings(location))
    return warnings


def split_affine_safety_warnings(warnings: list[str]) -> tuple[list[str], list[str]]:
    """Separate historic shape distortion from never-overridable control failures."""
    overridable_prefixes = ("scale ratio ", "axis angle ")
    overridable = [
        warning for warning in warnings if warning.startswith(overridable_prefixes)
    ]
    hard = [warning for warning in warnings if warning not in overridable]
    return overridable, hard


def require_expected_crs(target_crs: str, expected_crs: str = "EPSG:3857") -> None:
    if target_crs.strip().upper() != expected_crs.strip().upper():
        fail(
            f"The points file uses {target_crs}, but this run requires {expected_crs}. "
            "Use a deliberately different --expected-crs only for a documented workflow."
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Turn one source scan plus one verified three-point QGIS control file "
            "into a lossless, transparent GeoTIFF and a verification ledger."
        )
    )
    parser.add_argument("--source", required=True, type=Path, help="Read-only source scan")
    parser.add_argument("--points", required=True, type=Path, help="Verified QGIS .points file")
    parser.add_argument("--output", required=True, type=Path, help="New GeoTIFF filename")
    parser.add_argument(
        "--protected-project",
        type=Path,
        help="Mission-critical QGIS project whose modification time must be monitored",
    )
    parser.add_argument(
        "--confidence",
        choices=("three-osm", "osm-kauffman-assisted"),
        default="three-osm",
        help="Evidence level for the three controls",
    )
    parser.add_argument(
        "--control-label",
        action="append",
        default=[],
        help="Repeat exactly three times to record the named intersections",
    )
    parser.add_argument("--quality-note", default="", help="Short historical or QA note")
    parser.add_argument(
        "--pixel-size",
        type=float,
        help="Optional target pixel size in CRS units; omit to preserve normal full resolution",
    )
    parser.add_argument("--max-scale-ratio", type=float, default=1.15)
    parser.add_argument("--min-axis-angle", type=float, default=85.0)
    parser.add_argument("--max-axis-angle", type=float, default=95.0)
    parser.add_argument("--min-triangle-coverage", type=float, default=0.02)
    parser.add_argument("--min-x-span-fraction", type=float, default=0.20)
    parser.add_argument("--min-y-span-fraction", type=float, default=0.20)
    parser.add_argument(
        "--expected-crs",
        default="EPSG:3857",
        help="Required target CRS; defaults to the protected Atlanta map project's EPSG:3857",
    )
    parser.add_argument(
        "--expected-target-bbox",
        nargs=4,
        type=float,
        metavar=("MIN_X", "MIN_Y", "MAX_X", "MAX_Y"),
        help=(
            "Optional permitted EPSG:3857 neighborhood containing all three target controls"
        ),
    )
    parser.add_argument(
        "--expected-target-seed",
        nargs=2,
        type=float,
        metavar=("X", "Y"),
        help=(
            "Optional independently known point expected inside the transformed scan footprint"
        ),
    )
    parser.add_argument(
        "--max-target-seed-distance",
        type=float,
        default=0.0,
        help=(
            "Allowed CRS-unit distance from the expected seed to the transformed footprint"
        ),
    )
    parser.add_argument(
        "--allow-distortion",
        action="store_true",
        help="Proceed despite affine shape warnings; requires documented historical evidence",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.expanduser().resolve()
    points = args.points.expanduser().resolve()
    output = args.output.expanduser().resolve()
    protected = args.protected_project.expanduser().resolve() if args.protected_project else None
    ledger = output.with_suffix(".georef.json")

    for label, path in (("source scan", source), ("control file", points)):
        if not path.is_file():
            fail(f"The {label} does not exist: {path}")
    source_sha256_before = sha256(source)
    points_sha256_before = sha256(points)
    if output == source:
        fail("The output filename is the source filename. The source must remain read-only.")
    if output.exists():
        fail("The output already exists. Choose a new versioned filename.")
    if ledger.exists():
        # A ledger without its raster can only be an interrupted two-file publish.
        # It is safe to regenerate because a complete pair is never overwritten.
        try:
            incomplete = json.loads(ledger.read_text(encoding="utf-8"))
            same_inputs = (
                incomplete.get("source", {}).get("sha256") == source_sha256_before
                and incomplete.get("points", {}).get("sha256") == points_sha256_before
                and Path(str(incomplete.get("output", {}).get("path", ""))).resolve()
                == output
            )
        except (OSError, ValueError, json.JSONDecodeError):
            same_inputs = False
        if not same_inputs:
            fail(
                "A ledger without its raster already exists and does not match these "
                "inputs. Preserve it for investigation and choose a versioned output."
            )
        ledger.unlink()
    if not output.parent.is_dir():
        fail(f"The output folder does not exist: {output.parent}")
    if protected and not protected.is_file():
        fail(f"The protected QGIS project does not exist: {protected}")
    if args.pixel_size is not None and args.pixel_size <= 0:
        fail("Pixel size must be greater than zero.")
    if args.control_label and len(args.control_label) != 3:
        fail("Use --control-label exactly three times, or omit it entirely.")
    if args.max_target_seed_distance < 0:
        fail("--max-target-seed-distance cannot be negative.")
    if args.max_target_seed_distance and not args.expected_target_seed:
        fail("--max-target-seed-distance requires --expected-target-seed.")

    gdal_translate = require_program("gdal_translate")
    gdalwarp = require_program("gdalwarp")
    gdalinfo = require_program("gdalinfo")
    gdal_edit = require_program("gdal_edit.py")
    target_crs, controls = read_points(points)
    require_expected_crs(target_crs, args.expected_crs)

    source_info = capture_json([gdalinfo, "-json", str(source)])
    source_width, source_height = source_info["size"]
    for control in controls:
        x = float(control["source_x"])
        line = float(control["source_line_gdal"])
        if not (0 <= x < source_width and 0 <= line < source_height):
            fail(f"A control lies outside the {source_width} x {source_height} source image.")

    diagnostics = affine_diagnostics(controls, source_width, source_height)
    location_check = target_location_diagnostics(
        controls,
        diagnostics,
        source_width,
        source_height,
        expected_target_bbox=args.expected_target_bbox,
        expected_target_seed=args.expected_target_seed,
        max_target_seed_distance=args.max_target_seed_distance,
    )
    diagnostics["target_location_check"] = location_check
    require_target_location(location_check)
    warnings = affine_safety_warnings(
        diagnostics,
        max_scale_ratio=args.max_scale_ratio,
        min_axis_angle=args.min_axis_angle,
        max_axis_angle=args.max_axis_angle,
        min_triangle_coverage=args.min_triangle_coverage,
        min_x_span_fraction=args.min_x_span_fraction,
        min_y_span_fraction=args.min_y_span_fraction,
    )
    distortion_warnings, hard_warnings = split_affine_safety_warnings(warnings)
    if hard_warnings:
        fail("Affine hard safety gate failed: " + "; ".join(hard_warnings))
    if distortion_warnings and not args.allow_distortion:
        fail("Affine safety gate failed: " + "; ".join(distortion_warnings))
    if args.allow_distortion and not distortion_warnings:
        fail(
            "--allow-distortion only applies to scale-ratio or axis-angle warnings; "
            "these controls have no such warning."
        )
    if args.allow_distortion and not args.quality_note:
        fail("--allow-distortion also requires a --quality-note explaining the exception.")

    safety_limits = {
        "max_scale_ratio": args.max_scale_ratio,
        "axis_angle_degrees": [args.min_axis_angle, args.max_axis_angle],
        "min_triangle_coverage": args.min_triangle_coverage,
        "min_x_span_fraction": args.min_x_span_fraction,
        "min_y_span_fraction": args.min_y_span_fraction,
        "expected_crs": args.expected_crs,
        "expected_target_bbox": args.expected_target_bbox,
        "expected_target_seed": args.expected_target_seed,
        "max_target_seed_distance": args.max_target_seed_distance,
    }
    affine_signature = affine_provenance_signature(
        source_sha256_before,
        points_sha256_before,
        target_crs,
        diagnostics,
        safety_limits,
    )

    project_mtime_before = protected.stat().st_mtime_ns if protected else None
    partial = output.with_name(f".{output.stem}.partial-{os.getpid()}{output.suffix}")
    partial_ledger = ledger.with_name(f".{ledger.name}.partial-{os.getpid()}")

    try:
        with tempfile.TemporaryDirectory(prefix="sanborn-georef-") as temp_dir:
            vrt = Path(temp_dir) / "source-with-controls.vrt"
            translate_command = [gdal_translate, "-q", "-of", "VRT", "-a_srs", target_crs]
            for control in controls:
                translate_command.extend(
                    [
                        "-gcp",
                        str(control["source_x"]),
                        str(control["source_line_gdal"]),
                        str(control["map_x"]),
                        str(control["map_y"]),
                    ]
                )
            translate_command.extend([str(source), str(vrt)])
            run(translate_command)

            warp_command = [
                gdalwarp,
                "-q",
                "-order",
                "1",
                "-t_srs",
                target_crs,
                "-r",
                "cubic",
                "-dstalpha",
                "-multi",
                "-wo",
                "NUM_THREADS=ALL_CPUS",
                "-co",
                "COMPRESS=DEFLATE",
                "-co",
                "PREDICTOR=2",
                "-co",
                "ZLEVEL=9",
                "-co",
                "TILED=YES",
                "-co",
                "BIGTIFF=IF_SAFER",
                "-co",
                "NUM_THREADS=ALL_CPUS",
            ]
            if args.pixel_size is not None:
                warp_command.extend(["-tr", str(args.pixel_size), str(args.pixel_size)])
            warp_command.extend([str(vrt), str(partial)])
            run(warp_command)

        # Store the derivation identity in the GeoTIFF itself.  A resume check
        # therefore cannot be satisfied by rewriting only the adjacent ledger.
        run(
            [
                gdal_edit,
                "-mo",
                f"{SOURCE_METADATA_KEY}={source_sha256_before}",
                "-mo",
                f"{POINTS_METADATA_KEY}={points_sha256_before}",
                "-mo",
                f"{AFFINE_METADATA_KEY}={affine_signature}",
                "-mo",
                f"{PIPELINE_METADATA_KEY}={PIPELINE_METADATA_VALUE}",
                str(partial),
            ]
        )

        qa_env = dict(os.environ)
        qa_env["GDAL_PAM_ENABLED"] = "NO"
        output_info = capture_json([gdalinfo, "-json", "-checksum", "-stats", str(partial)], env=qa_env)
        if output_info.get("driverShortName") != "GTiff":
            fail("The output is not a GeoTIFF.")
        output_width, output_height = output_info["size"]
        if output_width < 2 or output_height < 2:
            fail("The warped output dimensions are invalid.")

        bands = output_info.get("bands", [])
        interpretations = [band.get("colorInterpretation") for band in bands]
        if interpretations != ["Red", "Green", "Blue", "Alpha"]:
            fail(f"Expected Red, Green, Blue, Alpha bands; found {interpretations}.")
        checksums = [int(band.get("checksum", 0)) for band in bands]
        if not any(checksums[:3]) or checksums[3] == 0:
            fail("Output pixel checks failed; the warp may be incomplete or empty.")

        image_structure = output_info.get("metadata", {}).get("IMAGE_STRUCTURE", {})
        if image_structure.get("COMPRESSION") != "DEFLATE":
            fail("The output is not using required lossless DEFLATE compression.")
        if image_structure.get("PREDICTOR") != "2":
            fail("The output is not using required predictor 2 compression.")

        embedded = output_info.get("metadata", {}).get("", {})
        expected_embedded = {
            SOURCE_METADATA_KEY: source_sha256_before,
            POINTS_METADATA_KEY: points_sha256_before,
            AFFINE_METADATA_KEY: affine_signature,
            PIPELINE_METADATA_KEY: PIPELINE_METADATA_VALUE,
        }
        for key, expected in expected_embedded.items():
            if embedded.get(key) != expected:
                fail(f"The output GeoTIFF is missing required provenance metadata {key}.")

        wkt = output_info.get("coordinateSystem", {}).get("wkt", "")
        epsg_match = re.fullmatch(r"EPSG:(\d+)", target_crs, re.IGNORECASE)
        if epsg_match and f'ID["EPSG",{epsg_match.group(1)}]' not in wkt:
            fail(f"The output CRS does not verify as {target_crs}.")

        alpha = bands[3]
        alpha_min = float(alpha.get("minimum", -1))
        alpha_max = float(alpha.get("maximum", -1))
        if alpha_max != 255:
            fail("The alpha band does not contain fully opaque map pixels.")

        if sha256(source) != source_sha256_before or sha256(points) != points_sha256_before:
            fail(
                "The source scan or control file changed during the warp. "
                "The GeoTIFF and ledger were not published."
            )

        for index, label in enumerate(args.control_label):
            controls[index]["label"] = label

        record = {
            "schema_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source": {
                "path": str(source),
                "width": source_width,
                "height": source_height,
                "bytes": source.stat().st_size,
                "sha256": source_sha256_before,
            },
            "points": {
                "path": str(points),
                "sha256": points_sha256_before,
                "controls": controls,
                "confidence": args.confidence,
            },
            "transformation": {
                "type": "Polynomial 1 / global affine",
                "target_crs": target_crs,
                "resampling": "cubic",
                "diagnostics": diagnostics,
                "warnings": warnings,
                "distortion_override": bool(args.allow_distortion),
                "safety_limits": safety_limits,
                "affine_provenance_signature": affine_signature,
            },
            "output": {
                "path": str(output),
                "width": output_width,
                "height": output_height,
                "bytes": partial.stat().st_size,
                "sha256": sha256(partial),
                "bands": interpretations,
                "band_checksums": checksums,
                "alpha_min": alpha_min,
                "alpha_max": alpha_max,
                "transparent_edge_pixels_present": alpha_min == 0,
                "compression": "DEFLATE",
                "predictor": 2,
            },
            "protected_project": {
                "path": str(protected) if protected else None,
                "mtime_ns_before": project_mtime_before,
                "mtime_ns_after": None,
                "unchanged_during_run": None,
            },
            "quality_note": args.quality_note,
        }
        # The expensive output hash above is part of the record. Sample the
        # protected project only after that work, then sample it once more after
        # assembling the ledger and immediately before the atomic publishes.
        project_mtime_after = protected.stat().st_mtime_ns if protected else None
        project_unchanged = project_mtime_before == project_mtime_after if protected else None
        if protected and not project_unchanged:
            fail(
                "The protected QGIS project changed while this tile was warping. "
                "The GeoTIFF and ledger were not published."
            )
        record["protected_project"]["mtime_ns_after"] = project_mtime_after
        record["protected_project"]["unchanged_during_run"] = project_unchanged
        partial_ledger.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        if protected and protected.stat().st_mtime_ns != project_mtime_before:
            fail(
                "The protected QGIS project changed during final ledger assembly. "
                "The GeoTIFF and ledger were not published."
            )
        # Publish the ledger first. If the process stops between these two atomic
        # renames, the next run recognizes and safely regenerates the incomplete
        # ledger-only pair. A raster is never left published without evidence.
        partial_ledger.replace(ledger)
        partial.replace(output)

        print(f"Created: {output}")
        print(f"Verification ledger: {ledger}")
        print(f"Size: {output_width} x {output_height}; bands: Red, Green, Blue, Alpha")
        print(
            "Affine diagnostics: "
            f"scale ratio {diagnostics['scale_ratio']:.4f}; "
            f"axis angle {diagnostics['axis_angle_degrees']:.3f} degrees"
        )
        print(f"SHA-256: {record['output']['sha256']}")
        if protected:
            print(f"Protected QGIS project unchanged during run: {project_unchanged}")
        return 0
    finally:
        if partial.exists():
            partial.unlink()
        if partial_ledger.exists():
            partial_ledger.unlink()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
