#!/usr/bin/env python3
"""Build and approve a fully local, hash-locked review packet for one tile.

The packet contains the source controls, a georeferenced preview, a local OSM
road overlay, and a co-registered crop of the 1921 Kauffman map.  Approval is
bound to the exact source, points, reference data, renderer inputs, and every
required artifact.  No live QGIS session or network request is needed.
"""

from __future__ import annotations

from sanborn_historical import validate_evidence as validate_historical_evidence

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, NoReturn, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps, __version__ as PILLOW_VERSION

from sanborn_georeference import (
    affine_diagnostics,
    affine_safety_warnings,
    capture_json,
    read_points,
    require_expected_crs,
    require_program,
    sha256,
    split_affine_safety_warnings,
)
from sanborn_osm import iter_way_geometries, read_metadata


SCHEMA_VERSION = 3
LOCAL_REFERENCE_METHOD = "local-osm-and-kauffman-packet"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KAUFFMAN_MAP = Path(
    "/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/"
    "Stage 1 -Orthorectified Atlanta Maps to print/"
    "1921 Atlanta Kauffman Map_modified.tif"
)
REQUIRED_ARTIFACTS = (
    "source_preview",
    "source_crosshairs",
    "georeferenced_preview",
    "osm_roads",
    "osm_overlay",
    "kauffman_reprojected",
    "kauffman_overlay",
    "contact_sheet",
)


def validate_control_labels(values: Sequence[str]) -> list[str]:
    """Require three distinct intersection names for a certifiable contact sheet."""
    labels = [str(value).strip() for value in values]
    if len(labels) != 3 or any(not label for label in labels):
        fail("Use --control-label exactly three times with nonblank intersection names.")
    if len({label.casefold() for label in labels}) != 3:
        fail("The three control labels must be distinct intersections.")
    separator = re.compile(r"(?:\s(?:x|×|at|and|&)\s|/)", re.IGNORECASE)
    for label in labels:
        if re.fullmatch(r"control\s*\d+", label, re.IGNORECASE) or not separator.search(label):
            fail(
                "Each control label must name both streets, for example "
                "'Auburn Avenue x Butler Street'."
            )
    return labels


def parse_target_seed_context(value: str) -> dict[str, Any]:
    try:
        context = json.loads(value)
    except json.JSONDecodeError as error:
        fail(f"--target-seed-json is not valid JSON: {error}")
    if not isinstance(context, dict) or context.get("status") != "selected":
        fail("The review packet requires one selected independent index-map seed.")
    try:
        tile = int(context["tile"])
        map_x = float(context["map_x"])
        map_y = float(context["map_y"])
        distance = float(context["suggested_max_distance"])
    except (KeyError, TypeError, ValueError):
        fail("The selected index-map seed is missing numeric tile or location values.")
    if tile <= 0 or not math.isfinite(map_x) or not math.isfinite(map_y):
        fail("The selected index-map seed has invalid tile or location values.")
    if not 0 < distance <= 250.0:
        fail("The selected index-map seed tolerance must be greater than zero and at most 250.")
    provenance = context.get("provenance")
    if (
        not isinstance(provenance, dict)
        or not isinstance(provenance.get("path"), str)
        or SHA256_PATTERN.fullmatch(str(provenance.get("sha256", ""))) is None
    ):
        fail("The selected index-map seed is missing its source-file provenance.")
    return context
OSM_COLOR = (0, 84, 255, 225)
OSM_CASING = (255, 255, 255, 225)


def fail(message: str) -> NoReturn:
    raise RuntimeError(message)


def default_osm_database() -> Path:
    override = os.environ.get("SANBORN_OSM_DB")
    if override:
        return Path(override).expanduser()
    candidates = (
        PROJECT_ROOT / "batch" / "osm" / "atlanta-sanborn-osm.sqlite3",
        PROJECT_ROOT / "batch" / "osm" / "atlanta-streets.sqlite3",
        PROJECT_ROOT / "batch" / "atlanta-sanborn-osm.sqlite3",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _font_resource() -> Path | None:
    candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def font(size: int, resource: Path | None = None) -> ImageFont.ImageFont:
    resource = resource if resource is not None else _font_resource()
    if resource:
        return ImageFont.truetype(str(resource), size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow before load_default gained a size argument.
        return ImageFont.load_default()


def _canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _file_record(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        fail(f"Required review input is missing: {resolved}")
    return {
        "path": str(resolved),
        "bytes": resolved.stat().st_size,
        "sha256": sha256(resolved),
    }


def _render_input_snapshot(
    source: Path,
    points: Path,
    osm_db: Path,
    kauffman_map: Path,
    font_resource: Path | None,
    target_seed_context: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Capture every file whose bytes can influence the review decision."""
    files = {
        "source scan": source,
        "control file": points,
        "local OSM database": osm_db,
        "Kauffman reference map": kauffman_map,
        "review renderer": Path(__file__),
        "OSM renderer": Path(__file__).with_name("sanborn_osm.py"),
    }
    if font_resource is not None:
        files["review font"] = font_resource
    provenance = target_seed_context.get("provenance")
    if not isinstance(provenance, dict):
        fail("The selected target seed has no provenance record.")
    seed_path_value = provenance.get("path")
    expected_seed_digest = provenance.get("sha256")
    if not isinstance(seed_path_value, str) or not isinstance(expected_seed_digest, str):
        fail("The selected target seed provenance is incomplete.")
    seed_path = Path(seed_path_value).expanduser().resolve()
    seed_record = _file_record(seed_path)
    if seed_record["sha256"] != expected_seed_digest:
        fail("The selected target seed file no longer matches its stored provenance.")
    files["target seed record"] = seed_path
    return {label: _file_record(path) for label, path in files.items()}


def _require_render_inputs_unchanged(snapshot: dict[str, dict[str, Any]]) -> None:
    for label, expected in snapshot.items():
        current = _file_record(Path(str(expected["path"])))
        if current != expected:
            fail(
                f"The {label} changed while the local review packet was rendering. "
                "No approvable review record was published."
            )


def _raster_summary(path: Path, gdalinfo: str | None = None) -> dict[str, Any]:
    info = capture_json([gdalinfo or require_program("gdalinfo"), "-json", str(path)])
    coordinate_system = info.get("coordinateSystem") or {}
    return {
        "size": [int(value) for value in info["size"]],
        "bands": [
            str(band.get("colorInterpretation", "Undefined"))
            for band in info.get("bands", [])
        ],
        "geo_transform": info.get("geoTransform"),
        "crs_wkt": coordinate_system.get("wkt"),
    }


def _artifact_record(path: Path, gdalinfo: str) -> dict[str, Any]:
    record = _file_record(path)
    if path.suffix.casefold() == ".png":
        with Image.open(path) as image:
            record["raster"] = {"size": list(image.size), "mode": image.mode}
    else:
        record["raster"] = _raster_summary(path, gdalinfo)
    return record


def _check_record(record: dict[str, Any], description: str) -> dict[str, Any]:
    if not isinstance(record, dict) or not record.get("path"):
        fail(f"The review packet has no provenance record for {description}.")
    current = _file_record(Path(record["path"]))
    if current["sha256"] != record.get("sha256") or current["bytes"] != record.get("bytes"):
        fail(
            f"{description} changed after the review packet was built. "
            "Rebuild and approve the packet again."
        )
    return current


def _software_provenance() -> dict[str, str]:
    gdalinfo = require_program("gdalinfo")
    result = subprocess.run(
        [gdalinfo, "--version"], check=True, capture_output=True, text=True
    )
    return {
        "gdal": result.stdout.strip(),
        "pillow": PILLOW_VERSION,
        "python": ".".join(str(value) for value in sys.version_info[:3]),
    }


def approval_token(
    source: Path,
    points: Path,
    diagnostics: dict,
    *,
    labels: list[str],
    target_crs: str,
    safety_limits: dict,
    provenance: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    render_spec: dict[str, Any] | None = None,
) -> str:
    """Lock approval to every file and decision that controls or proves the warp.

    The final three keyword arguments are optional only to preserve the small
    public helper used by older callers.  Packets created by this version always
    provide all three and are rejected if any are absent.
    """

    payload: dict[str, Any] = {
        "source": _file_record(source),
        "points": _file_record(points),
        "diagnostics": diagnostics,
        "labels": labels,
        "target_crs": target_crs,
        "safety_limits": safety_limits,
    }
    if provenance is not None:
        payload["provenance"] = provenance
    if artifacts is not None:
        payload["artifacts"] = artifacts
    if render_spec is not None:
        payload["render_spec"] = render_spec
    return _canonical_hash(payload)


def _extent_from_info(info: dict[str, Any]) -> tuple[float, float, float, float]:
    transform = info.get("geoTransform")
    if not transform or len(transform) != 6:
        fail("The georeferenced preview has no usable map transform.")
    width, height = (int(value) for value in info["size"])
    corners = []
    for pixel, line in ((0, 0), (width, 0), (0, height), (width, height)):
        x = transform[0] + pixel * transform[1] + line * transform[2]
        y = transform[3] + pixel * transform[4] + line * transform[5]
        corners.append((x, y))
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    extent = min(xs), min(ys), max(xs), max(ys)
    if not extent[0] < extent[2] or not extent[1] < extent[3]:
        fail("The georeferenced preview has an invalid target extent.")
    return extent


def _target_pixel(
    x: float,
    y: float,
    extent: Sequence[float],
    size: Sequence[int],
) -> tuple[float, float]:
    min_x, min_y, max_x, max_y = (float(value) for value in extent)
    width, height = (int(value) for value in size)
    px = (float(x) - min_x) / (max_x - min_x) * width
    py = (max_y - float(y)) / (max_y - min_y) * height
    return px, py


def _draw_target_controls(
    image: Image.Image,
    controls: list[dict[str, Any]],
    labels: list[str],
    extent: Sequence[float],
    label_font: ImageFont.ImageFont,
) -> None:
    draw = ImageDraw.Draw(image)
    radius = max(7, image.width // 140)
    for control, label in zip(controls, labels):
        x, y = _target_pixel(control["map_x"], control["map_y"], extent, image.size)
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            outline=(220, 0, 0, 255),
            width=max(2, radius // 3),
        )
        draw.line((x - radius * 2, y, x + radius * 2, y), fill=(220, 0, 0, 255), width=2)
        draw.line((x, y - radius * 2, x, y + radius * 2), fill=(220, 0, 0, 255), width=2)
        box = draw.textbbox((0, 0), label, font=label_font, stroke_width=2)
        text_width = box[2] - box[0]
        text_height = box[3] - box[1]
        tx = x + radius * 2
        if tx + text_width > image.width:
            tx = x - radius * 2 - text_width
        tx = max(0, min(tx, image.width - text_width))
        ty = max(0, min(y - radius * 2, image.height - text_height))
        draw.text(
            (tx, ty),
            label,
            font=label_font,
            fill=(220, 0, 0, 255),
            stroke_width=2,
            stroke_fill=(255, 255, 255, 255),
        )


def _road_width(highway: str | None, image_width: int) -> int:
    scale = max(1, image_width // 700)
    rank = {
        "motorway": 6,
        "trunk": 6,
        "primary": 5,
        "secondary": 4,
        "tertiary": 3,
        "residential": 2,
        "unclassified": 2,
        "service": 1,
    }.get(str(highway), 2)
    return max(1, rank * scale)


def _render_osm_roads(
    osm_db: Path,
    extent: Sequence[float],
    size: Sequence[int],
) -> tuple[Image.Image, list[int], int]:
    width, height = (int(value) for value in size)
    min_x, min_y, max_x, max_y = (float(value) for value in extent)
    padding_x = (max_x - min_x) * 0.15
    padding_y = (max_y - min_y) * 0.15
    query_extent = (
        min_x - padding_x,
        min_y - padding_y,
        max_x + padding_x,
        max_y + padding_y,
    )
    roads = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(roads)
    geometries = list(iter_way_geometries(osm_db, bbox_3857=query_extent))
    way_ids: list[int] = []
    vertex_count = 0
    for geometry in geometries:
        vertices = geometry.get("vertices", [])
        if len(vertices) < 2:
            continue
        pixels = [
            _target_pixel(vertex["x_3857"], vertex["y_3857"], extent, (width, height))
            for vertex in vertices
        ]
        line_width = _road_width(geometry.get("highway"), width)
        draw.line(
            pixels,
            fill=OSM_CASING,
            width=line_width + max(2, line_width // 2),
            joint="curve",
        )
        draw.line(pixels, fill=OSM_COLOR, width=line_width, joint="curve")
        way_ids.append(int(geometry["osm_way_id"]))
        vertex_count += len(vertices)
    if not way_ids:
        fail(
            "The local OSM database contains no named road geometry in the proposed "
            "target extent. Check the controls or refresh the bounded Atlanta OSM cache."
        )
    return roads, sorted(set(way_ids)), vertex_count


def _white_background(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, "white")
    background.alpha_composite(rgba)
    return background


def _make_contact_sheet(
    panels: Sequence[tuple[str, Image.Image]],
    destination: Path,
    title_font: ImageFont.ImageFont,
) -> None:
    cell_width = 880
    cell_height = 660
    header = 44
    sheet = Image.new("RGB", (cell_width * 2, (cell_height + header) * math.ceil(len(panels) / 2)), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (title, panel) in enumerate(panels):
        row, column = divmod(index, 2)
        x = column * cell_width
        y = row * (cell_height + header)
        draw.rectangle((x, y, x + cell_width, y + header), fill=(238, 238, 238))
        draw.text((x + 14, y + 8), title, font=title_font, fill="black")
        fitted = ImageOps.contain(panel.convert("RGB"), (cell_width, cell_height))
        px = x + (cell_width - fitted.width) // 2
        py = y + header + (cell_height - fitted.height) // 2
        sheet.paste(fitted, (px, py))
    sheet.save(destination)


def _build_provenance(
    osm_db: Path,
    kauffman_map: Path,
    font_resource: Path | None,
    gdalinfo: str,
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "osm_database": _file_record(osm_db),
        "osm_metadata": read_metadata(osm_db),
        "kauffman_map": _file_record(kauffman_map),
        "kauffman_raster": _raster_summary(kauffman_map, gdalinfo),
        "renderer_code": {
            "sanborn_review": _file_record(Path(__file__)),
            "sanborn_osm": _file_record(Path(__file__).with_name("sanborn_osm.py")),
        },
        "software": _software_provenance(),
    }
    if font_resource:
        provenance["font"] = _file_record(font_resource)
    else:
        provenance["font"] = {"pillow_builtin": True, "pillow": PILLOW_VERSION}
    return provenance


def _current_provenance(review: dict[str, Any], gdalinfo: str) -> dict[str, Any]:
    stored = review.get("provenance")
    if not isinstance(stored, dict):
        fail("The review packet predates local OSM/Kauffman provenance. Rebuild it.")
    osm = _check_record(stored.get("osm_database", {}), "the local OSM database")
    kauffman = _check_record(stored.get("kauffman_map", {}), "the Kauffman reference map")
    code = stored.get("renderer_code")
    if not isinstance(code, dict):
        fail("The review packet has no renderer-code provenance.")
    current_code = {
        key: _check_record(record, f"renderer code {key}")
        for key, record in sorted(code.items())
    }
    current: dict[str, Any] = {
        "osm_database": osm,
        "osm_metadata": read_metadata(Path(osm["path"])),
        "kauffman_map": kauffman,
        "kauffman_raster": _raster_summary(Path(kauffman["path"]), gdalinfo),
        "renderer_code": current_code,
        "software": _software_provenance(),
    }
    historical = stored.get("historical_evidence")
    if historical is not None:
        current["historical_evidence"] = _check_record(historical, "historical street measurements")
        evidence = json.loads(Path(historical["path"]).read_text())
        validate_historical_evidence(evidence, review["source"]["path"], review["points"]["path"])
        current["historical_reference"] = _check_record(stored.get("historical_reference", {}), "the original historical reference")
        if current["historical_reference"] != evidence["reference"]:
            fail("Historical measurements and reference provenance disagree.")
        current["historical_profile"] = evidence["reference_profile"]
        current["historical_measurement_preview"] = _check_record(stored.get("historical_measurement_preview", {}), "the measurement preview")
        if current["historical_measurement_preview"] != evidence["reference_preview"]:
            fail("The historical preview provenance differs from its measurement record.")
    font_record = stored.get("font")
    if isinstance(font_record, dict) and font_record.get("path"):
        current["font"] = _check_record(font_record, "the review-packet font")
    else:
        current["font"] = {"pillow_builtin": True, "pillow": PILLOW_VERSION}
    if current != stored:
        fail(
            "A local reference, renderer, or software provenance input changed after "
            "the packet was built. Rebuild and approve it again."
        )
    return current


def _current_artifacts(review: dict[str, Any], gdalinfo: str) -> dict[str, Any]:
    stored = review.get("artifacts")
    if not isinstance(stored, dict):
        fail("The review packet has no hash-locked local artifacts. Rebuild it.")
    missing_keys = [key for key in REQUIRED_ARTIFACTS if key not in stored]
    if missing_keys:
        fail("The review packet is missing required artifacts: " + ", ".join(missing_keys))
    current: dict[str, Any] = {}
    for key, record in sorted(stored.items()):
        checked = _check_record(record, f"review artifact {key}")
        current[key] = _artifact_record(Path(checked["path"]), gdalinfo)
    if current != stored:
        fail(
            "A required review artifact's raster properties changed after the packet "
            "was built. Rebuild and approve it again."
        )
    return current


def current_review_state(review: dict[str, Any]) -> tuple[str, dict]:
    """Recompute the approval lock from every input and artifact used now."""

    if int(review.get("schema_version", 0)) < SCHEMA_VERSION:
        fail(
            "This packet predates fully local OSM and Kauffman verification. "
            "Rebuild it before approval or finishing."
        )
    source_record = review.get("source", {})
    points_record = review.get("points", {})
    source = Path(str(source_record.get("path", "")))
    points = Path(str(points_record.get("path", "")))
    _check_record(source_record, "the source scan")
    _check_record(points_record, "the control-points file")

    gdalinfo = require_program("gdalinfo")
    source_info = capture_json([gdalinfo, "-json", str(source)])
    source_width, source_height = source_info["size"]
    if [source_width, source_height] != [source_record.get("width"), source_record.get("height")]:
        fail("The source scan dimensions changed after the review packet was built.")

    target_crs, controls = read_points(points)
    for index, control in enumerate(controls, start=1):
        x = float(control["source_x"])
        y = float(control["source_line_gdal"])
        if not (0 <= x < source_width and 0 <= y < source_height):
            fail(
                f"Control {index} source coordinate ({x}, {y}) is outside the "
                f"{source_width} x {source_height} source scan."
            )
    limits = review["safety_limits"]
    require_expected_crs(target_crs, limits.get("expected_crs", "EPSG:3857"))
    diagnostics = affine_diagnostics(controls, source_width, source_height)
    warnings = affine_safety_warnings(
        diagnostics,
        max_scale_ratio=float(limits["max_scale_ratio"]),
        min_axis_angle=float(limits["axis_angle_degrees"][0]),
        max_axis_angle=float(limits["axis_angle_degrees"][1]),
        min_triangle_coverage=float(limits["min_triangle_coverage"]),
        min_x_span_fraction=float(limits["min_x_span_fraction"]),
        min_y_span_fraction=float(limits["min_y_span_fraction"]),
    )
    distortion_warnings, hard_warnings = split_affine_safety_warnings(warnings)
    allow_distortion = limits.get("allow_distortion", False)
    distortion_note = limits.get("distortion_note", "")
    recorded_warnings = limits.get("distortion_warnings", [])
    if not isinstance(allow_distortion, bool):
        fail("The review packet has an invalid distortion-override flag.")
    if not isinstance(distortion_note, str):
        fail("The review packet has an invalid distortion-override note.")
    if not isinstance(recorded_warnings, list) or not all(
        isinstance(warning, str) for warning in recorded_warnings
    ):
        fail("The review packet has an invalid distortion-warning record.")
    if allow_distortion and not distortion_note.strip():
        fail("The review packet allows distortion without a nonblank explanation.")
    if recorded_warnings != distortion_warnings:
        fail(
            "The current affine distortion warnings differ from the hash-locked "
            "review packet. Rebuild and approve it again."
        )
    if hard_warnings:
        fail("The current controls fail a hard safety gate: " + "; ".join(hard_warnings))
    if distortion_warnings and not allow_distortion:
        fail(
            "The current controls fail the distortion safety gate: "
            + "; ".join(distortion_warnings)
        )
    if allow_distortion and not distortion_warnings:
        fail("The review packet claims a distortion exception that is no longer needed.")

    labels = [str(control["label"]) for control in points_record["controls"]]
    provenance = _current_provenance(review, gdalinfo)
    artifacts = _current_artifacts(review, gdalinfo)
    render_spec = review.get("render_spec")
    if not isinstance(render_spec, dict):
        fail("The review packet has no local overlay render specification. Rebuild it.")
    token = approval_token(
        source,
        points,
        diagnostics,
        labels=labels,
        target_crs=target_crs,
        safety_limits=limits,
        provenance=provenance,
        artifacts=artifacts,
        render_spec=render_spec,
    )
    if token != review.get("approval_token"):
        fail(
            "The source, controls, local references, render settings, or review artifacts "
            "changed after the packet was built. Rebuild and approve it again."
        )
    return token, diagnostics


def create_packet(args: argparse.Namespace) -> int:
    source = args.source.expanduser().resolve()
    points = args.points.expanduser().resolve()
    review_dir = args.review_dir.expanduser().resolve()
    osm_db = args.osm_db.expanduser().resolve()
    kauffman_map = args.kauffman_map.expanduser().resolve()
    if not source.is_file() or not points.is_file():
        fail("The source scan and points file must both exist.")
    if not osm_db.is_file():
        fail(f"The local OSM database is missing: {osm_db}")
    if not kauffman_map.is_file():
        fail(f"The Kauffman reference map is missing: {kauffman_map}")
    labels = validate_control_labels(args.control_label)
    historical_evidence_path = getattr(args, "historical_evidence", None)
    historical_evidence = None
    if historical_evidence_path:
        historical_evidence_path = historical_evidence_path.expanduser().resolve()
        historical_evidence = validate_historical_evidence(json.loads(historical_evidence_path.read_text()), source, points)
        if [p["label"] for p in historical_evidence["controls"]] != labels:
            fail("Historical corner names differ from the review controls.")
    target_seed_context = parse_target_seed_context(args.target_seed_json)
    if review_dir.exists() and any(review_dir.iterdir()) and not args.replace:
        fail(f"Review folder is not empty: {review_dir}. Use --replace to rebuild it.")
    review_dir.mkdir(parents=True, exist_ok=True)

    gdalinfo = require_program("gdalinfo")
    gdal_translate = require_program("gdal_translate")
    gdalwarp = require_program("gdalwarp")
    font_resource = _font_resource()
    input_snapshot = _render_input_snapshot(
        source,
        points,
        osm_db,
        kauffman_map,
        font_resource,
        target_seed_context,
    )
    if historical_evidence:
        input_snapshot["historical measurements"] = _file_record(historical_evidence_path)
        input_snapshot["historical reference"] = _file_record(Path(historical_evidence["reference"]["path"]))
        input_snapshot["historical measurement preview"] = _file_record(Path(historical_evidence["reference_preview"]["path"]))
        input_snapshot["historical measurement code"] = _file_record(Path(__file__).with_name("sanborn_historical.py"))
    source_info = capture_json([gdalinfo, "-json", str(source)])
    source_width, source_height = source_info["size"]
    target_crs, controls = read_points(points)
    for index, control in enumerate(controls, start=1):
        x = float(control["source_x"])
        y = float(control["source_line_gdal"])
        if not (0 <= x < source_width and 0 <= y < source_height):
            fail(
                f"Control {index} source coordinate ({x}, {y}) is outside the "
                f"{source_width} x {source_height} source scan."
            )
    require_expected_crs(target_crs, args.expected_crs)
    diagnostics = affine_diagnostics(controls, source_width, source_height)
    safety_limits = {
        "max_scale_ratio": args.max_scale_ratio,
        "axis_angle_degrees": [args.min_axis_angle, args.max_axis_angle],
        "min_triangle_coverage": args.min_triangle_coverage,
        "min_x_span_fraction": args.min_x_span_fraction,
        "min_y_span_fraction": args.min_y_span_fraction,
        "expected_crs": args.expected_crs,
        "target_seed_context": target_seed_context,
    }
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
    allow_distortion = bool(getattr(args, "allow_distortion", False))
    distortion_note = str(getattr(args, "distortion_note", "") or "").strip()
    if allow_distortion and not distortion_note:
        fail("--allow-distortion requires a nonblank --distortion-note.")
    if distortion_note and not allow_distortion:
        fail("--distortion-note requires --allow-distortion.")
    if hard_warnings:
        fail(
            "Review packet rejected by hard affine safety gate: "
            + "; ".join(hard_warnings)
        )
    if distortion_warnings and not allow_distortion:
        fail(
            "Review packet rejected by affine safety gate: "
            + "; ".join(distortion_warnings)
            + f". Ratio={diagnostics['scale_ratio']:.4f}, "
            f"angle={diagnostics['axis_angle_degrees']:.3f} degrees."
        )
    if allow_distortion and not distortion_warnings:
        fail(
            "--allow-distortion only applies to scale-ratio or axis-angle warnings; "
            "these controls have no such warning."
        )
    safety_limits.update(
        {
            "allow_distortion": allow_distortion,
            "distortion_note": distortion_note,
            "distortion_warnings": list(distortion_warnings),
        }
    )

    artifact_paths = {
        "source_preview": review_dir / "source-preview.png",
        "source_crosshairs": review_dir / "source-crosshairs.png",
        "georeferenced_preview": review_dir / "georeferenced-preview.tif",
        "osm_roads": review_dir / "osm-roads.png",
        "osm_overlay": review_dir / "osm-overlay.png",
        "kauffman_reprojected": review_dir / "kauffman-reprojected.tif",
        "kauffman_overlay": review_dir / "kauffman-overlay.png",
        "contact_sheet": review_dir / "review-contact-sheet.png",
    }
    stale_paths = (
        *artifact_paths.values(),
        review_dir / "review.json",
        review_dir / "approval.json",
    )
    for path in stale_paths:
        if path.is_file():
            path.unlink()

    subprocess.run(
        [
            gdal_translate,
            "-q",
            "-of",
            "PNG",
            "-outsize",
            str(args.source_preview_width),
            "0",
            str(source),
            str(artifact_paths["source_preview"]),
        ],
        check=True,
    )
    source_image = Image.open(artifact_paths["source_preview"]).convert("RGB")
    source_crosshairs = source_image.copy()
    draw = ImageDraw.Draw(source_crosshairs)
    label_font = font(max(13, source_image.width // 105), font_resource)
    scale_x = source_image.width / source_width
    scale_y = source_image.height / source_height
    for control, label in zip(controls, labels):
        x = float(control["source_x"]) * scale_x
        y = float(control["source_line_gdal"]) * scale_y
        radius = max(10, source_image.width // 120)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline="red", width=4)
        draw.line((x - radius * 2, y, x + radius * 2, y), fill="red", width=4)
        draw.line((x, y - radius * 2, x, y + radius * 2), fill="red", width=4)
        box = draw.textbbox((0, 0), label, font=label_font, stroke_width=2)
        text_width = box[2] - box[0]
        tx = x + radius * 2
        if tx + text_width > source_image.width:
            tx = x - radius * 2 - text_width
        draw.text(
            (tx, max(0, y - radius * 2)),
            label,
            font=label_font,
            fill="red",
            stroke_width=3,
            stroke_fill="white",
        )
    source_crosshairs.save(artifact_paths["source_crosshairs"])

    with tempfile.TemporaryDirectory(prefix="sanborn-review-") as temp:
        vrt = Path(temp) / "controls.vrt"
        command = [gdal_translate, "-q", "-of", "VRT", "-a_srs", target_crs]
        for control in controls:
            command.extend(
                [
                    "-gcp",
                    str(control["source_x"]),
                    str(control["source_line_gdal"]),
                    str(control["map_x"]),
                    str(control["map_y"]),
                ]
            )
        command.extend([str(source), str(vrt)])
        subprocess.run(command, check=True)
        subprocess.run(
            [
                gdalwarp,
                "-q",
                "-order",
                "1",
                "-t_srs",
                target_crs,
                "-r",
                "cubic",
                "-dstalpha",
                "-ts",
                str(args.map_preview_width),
                "0",
                "-overwrite",
                str(vrt),
                str(artifact_paths["georeferenced_preview"]),
            ],
            check=True,
        )

    georef_info = capture_json(
        [gdalinfo, "-json", str(artifact_paths["georeferenced_preview"])]
    )
    extent = _extent_from_info(georef_info)
    map_size = tuple(int(value) for value in georef_info["size"])
    georef_image = _white_background(Image.open(artifact_paths["georeferenced_preview"]))
    target_font = font(max(12, map_size[0] // 110), font_resource)

    roads, osm_way_ids, osm_vertex_count = _render_osm_roads(osm_db, extent, map_size)
    roads.save(artifact_paths["osm_roads"])
    osm_overlay = Image.alpha_composite(georef_image, roads)
    _draw_target_controls(osm_overlay, controls, labels, extent, target_font)
    osm_overlay.save(artifact_paths["osm_overlay"])

    min_x, min_y, max_x, max_y = extent
    subprocess.run(
        [
            gdalwarp,
            "-q",
            "-t_srs",
            target_crs,
            "-te",
            f"{min_x:.12f}",
            f"{min_y:.12f}",
            f"{max_x:.12f}",
            f"{max_y:.12f}",
            "-ts",
            str(map_size[0]),
            str(map_size[1]),
            "-r",
            "cubic",
            "-dstalpha",
            "-overwrite",
            str(kauffman_map),
            str(artifact_paths["kauffman_reprojected"]),
        ],
        check=True,
    )
    kauffman_raw = Image.open(artifact_paths["kauffman_reprojected"]).convert("RGBA")
    alpha_histogram = kauffman_raw.getchannel("A").histogram()
    kauffman_coverage = sum(alpha_histogram[1:]) / float(map_size[0] * map_size[1])
    if kauffman_coverage < 0.05:
        fail(
            "The proposed target extent has no meaningful coverage in the Kauffman "
            "reference map. Check the target controls before review."
        )
    kauffman_image = _white_background(kauffman_raw)
    if kauffman_image.size != georef_image.size:
        fail("The Kauffman crop did not match the proposed target preview dimensions.")
    kauffman_overlay = Image.blend(kauffman_image.convert("RGBA"), georef_image, 0.45)
    _draw_target_controls(kauffman_overlay, controls, labels, extent, target_font)
    kauffman_overlay.save(artifact_paths["kauffman_overlay"])

    historical_views = []
    historical_coverage = None
    if historical_evidence:
        historical_raster = review_dir / "historical-reference.tif"
        historical_overlay_path = review_dir / "historical-overlay.png"
        subprocess.run([gdalwarp, "-q", "-t_srs", target_crs, "-te", *[str(v) for v in extent],
                        "-ts", str(map_size[0]), str(map_size[1]), "-r", "cubic", "-dstalpha", "-overwrite",
                        historical_evidence["reference"]["path"], str(historical_raster)], check=True)
        historical_raw = Image.open(historical_raster).convert("RGBA")
        historical_coverage = sum(historical_raw.getchannel("A").histogram()[1:]) / float(map_size[0]*map_size[1])
        if historical_coverage < .05:
            fail("The original topo has no meaningful coverage at this sheet. Use a reference that covers its actual streets.")
        historical_image = _white_background(historical_raw)
        historical_overlay = Image.blend(historical_image, georef_image, .45)
        _draw_target_controls(historical_overlay, controls, labels, extent, target_font)
        historical_overlay.save(historical_overlay_path)
        artifact_paths["historical_reference"] = historical_raster
        artifact_paths["historical_overlay"] = historical_overlay_path
        historical_views = [("1958 original topo: primary historic reference", historical_image),
                            ("1958 topo overlay: check streets beyond the fit corners", historical_overlay)]

    georef_marked = georef_image.copy()
    _draw_target_controls(georef_marked, controls, labels, extent, target_font)
    _make_contact_sheet(
        (
            ("Source scan: proposed controls", source_crosshairs),
            ("Affine preview: target controls", georef_marked),
            ("Local OSM named roads", osm_overlay),
            ("1921 Kauffman map overlay", kauffman_overlay),
            *historical_views,
        ),
        artifact_paths["contact_sheet"],
        font(24, font_resource),
    )

    for index, label in enumerate(labels):
        controls[index]["label"] = label
    _require_render_inputs_unchanged(input_snapshot)
    provenance = _build_provenance(osm_db, kauffman_map, font_resource, gdalinfo)
    if historical_evidence:
        provenance["historical_evidence"] = _file_record(historical_evidence_path)
        provenance["historical_reference"] = historical_evidence["reference"]
        provenance["historical_profile"] = historical_evidence["reference_profile"]
        provenance["historical_measurement_preview"] = historical_evidence["reference_preview"]
        provenance["renderer_code"]["sanborn_historical"] = _file_record(Path(__file__).with_name("sanborn_historical.py"))
    render_spec = {
        "target_crs": target_crs,
        "target_extent": [float(value) for value in extent],
        "map_preview_size": list(map_size),
        "source_preview_width": args.source_preview_width,
        "osm": {
            "query_padding_fraction": 0.15,
            "way_ids": osm_way_ids,
            "way_count": len(osm_way_ids),
            "vertex_count": osm_vertex_count,
            "color_rgba": list(OSM_COLOR),
            "casing_rgba": list(OSM_CASING),
        },
        "kauffman": {
            "resampling": "cubic",
            "sanborn_blend_fraction": 0.45,
            "coverage_fraction": kauffman_coverage,
        },
        "affine_preview": {"order": 1, "resampling": "cubic"},
        **({"historical_reference": {"raster_coverage_fraction": historical_coverage,
            "coverage_note": "Raster coverage includes blank paper; each measured street also passed a drawn-ink check.",
            "independent_checks": historical_evidence["independent_checks"],
            "uncertainty": historical_evidence["reference_profile"]["uncertainty"]}} if historical_evidence else {}),
    }
    artifacts = {
        key: _artifact_record(path, gdalinfo) for key, path in sorted(artifact_paths.items())
    }
    token = approval_token(
        source,
        points,
        diagnostics,
        labels=labels,
        target_crs=target_crs,
        safety_limits=safety_limits,
        provenance=provenance,
        artifacts=artifacts,
        render_spec=render_spec,
    )
    record = {
        "schema_version": SCHEMA_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": {
            **_file_record(source),
            "width": source_width,
            "height": source_height,
        },
        "points": {
            **_file_record(points),
            "target_crs": target_crs,
            "controls": controls,
        },
        "affine_diagnostics": diagnostics,
        "safety_limits": safety_limits,
        "provenance": provenance,
        "render_spec": render_spec,
        "artifacts": artifacts,
        "required_artifacts": list(artifact_paths),
        "local_geographic_verification": {
            "method": LOCAL_REFERENCE_METHOD,
            "network_required": False,
            "qgis_required": False,
            "instructions": (
                "Inspect review-contact-sheet.png at full size. Confirm the three source "
                "crosshairs name the intended intersections and that the transformed street "
                "grid agrees with genuinely surviving OSM street centerlines and historical evidence. "
                "Where supplied, the 1958 original topo is primary for vanished streets; Kauffman is supporting context. "
                "Do not use planned road lines or changed curbs as original street centers. "
                "Zero residual at three fit corners is not independent validation."
            ),
        },
        "approval_token": token,
        "approved": False,
    }
    review_path = review_dir / "review.json"
    partial_review = review_dir / f".review.json.partial-{os.getpid()}"
    published = False
    try:
        partial_review.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _require_render_inputs_unchanged(input_snapshot)
        os.replace(partial_review, review_path)
        published = True
        _require_render_inputs_unchanged(input_snapshot)
    except Exception:
        partial_review.unlink(missing_ok=True)
        if published:
            review_path.unlink(missing_ok=True)
        raise
    print(f"Review packet: {review_dir}")
    if warnings:
        print(
            "Affine safety override recorded: "
            + "; ".join(warnings)
            + f". Note: {distortion_note}"
        )
    else:
        print(
            f"Affine safety passed: ratio {diagnostics['scale_ratio']:.4f}; "
            f"angle {diagnostics['axis_angle_degrees']:.3f} degrees"
        )
    print(f"Local references: {len(osm_way_ids)} OSM ways plus the Kauffman crop")
    print(f"Approval token: {token}")
    return 0


def approve_packet(args: argparse.Namespace) -> int:
    review_dir = args.review_dir.expanduser().resolve()
    review_file = review_dir / "review.json"
    if not review_file.is_file():
        fail(f"Review packet does not exist: {review_file}")
    review = json.loads(review_file.read_text(encoding="utf-8"))
    approved_by = str(args.approved_by).strip()
    if not approved_by:
        fail("Approval requires a nonblank reviewer name.")
    token, _ = current_review_state(review)
    artifact_hashes = {
        key: review["artifacts"][key]["sha256"] for key in review["artifacts"]
    }
    record = {
        "schema_version": SCHEMA_VERSION,
        "approved_utc": datetime.now(timezone.utc).isoformat(),
        "approval_token": token,
        "approved_by": approved_by,
        "note": args.note,
        "geographic_verification": {
            "reference_method": LOCAL_REFERENCE_METHOD,
            "note": "Hash-locked street references and comparison views reviewed; historical measurements remain historical evidence.",
            "artifact_sha256": artifact_hashes,
        },
    }
    (review_dir / "approval.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    review["approved"] = True
    review_file.write_text(
        json.dumps(review, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Approved review packet: {review_dir}")
    print(f"Approval token: {token}")
    return 0


def require_approval(review_dir: Path) -> tuple[dict, dict]:
    review_file = review_dir / "review.json"
    approval_file = review_dir / "approval.json"
    if not review_file.is_file() or not approval_file.is_file():
        fail("The hash-locked review packet has not been approved.")
    review = json.loads(review_file.read_text(encoding="utf-8"))
    approval = json.loads(approval_file.read_text(encoding="utf-8"))
    if approval.get("schema_version") != SCHEMA_VERSION:
        fail("The approval record uses an unsupported schema.")
    if review.get("approved") is not True:
        fail("The review packet is not marked approved.")
    approved_by = approval.get("approved_by")
    if not isinstance(approved_by, str) or not approved_by.strip():
        fail("The approval record has no reviewer name.")
    approved_utc = approval.get("approved_utc")
    if not isinstance(approved_utc, str):
        fail("The approval record has no timestamp.")
    try:
        approved_time = datetime.fromisoformat(approved_utc)
    except ValueError:
        fail("The approval record timestamp is invalid.")
    if approved_time.tzinfo is None or approved_time.utcoffset() is None:
        fail("The approval record timestamp must include a timezone.")
    current_token, _ = current_review_state(review)
    if current_token != approval.get("approval_token"):
        fail("The local review packet changed after approval. Rebuild and approve it again.")
    verification = approval.get("geographic_verification", {})
    if verification.get("reference_method") != LOCAL_REFERENCE_METHOD:
        fail("Approval does not certify the required local OSM and Kauffman overlays.")
    expected_hashes = {
        key: review["artifacts"][key]["sha256"] for key in review["artifacts"]
    }
    if verification.get("artifact_sha256") != expected_hashes:
        fail("Approval does not match every required local review artifact.")
    return review, approval


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="Generate the fully local approval packet")
    create.add_argument("--historical-evidence", type=Path)
    create.add_argument("--source", required=True, type=Path)
    create.add_argument("--points", required=True, type=Path)
    create.add_argument("--review-dir", required=True, type=Path)
    create.add_argument("--osm-db", type=Path, default=default_osm_database())
    create.add_argument("--kauffman-map", type=Path, default=DEFAULT_KAUFFMAN_MAP)
    create.add_argument("--control-label", action="append", default=[])
    create.add_argument(
        "--target-seed-json",
        required=True,
        help="Selected independent index-map seed context supplied by the batch queue",
    )
    create.add_argument("--source-preview-width", type=int, default=1600)
    create.add_argument("--map-preview-width", type=int, default=1400)
    create.add_argument("--max-scale-ratio", type=float, default=1.15)
    create.add_argument("--min-axis-angle", type=float, default=85.0)
    create.add_argument("--max-axis-angle", type=float, default=95.0)
    create.add_argument("--min-triangle-coverage", type=float, default=0.02)
    create.add_argument("--min-x-span-fraction", type=float, default=0.20)
    create.add_argument("--min-y-span-fraction", type=float, default=0.20)
    create.add_argument("--expected-crs", default="EPSG:3857")
    create.add_argument(
        "--allow-distortion",
        action="store_true",
        help="Build the packet despite affine safety warnings; requires a written reason",
    )
    create.add_argument(
        "--distortion-note",
        default="",
        help="Documented historical evidence for an allowed distortion",
    )
    create.add_argument("--replace", action="store_true")
    create.set_defaults(function=create_packet)

    approve = sub.add_parser("approve", help="Lock approval to the exact local packet hash")
    approve.add_argument("--review-dir", required=True, type=Path)
    approve.add_argument("--approved-by", default="Joel or ChatGPT")
    approve.add_argument("--note", default="")
    # Retain the former flags as ignored compatibility shims for sanborn_batch.py.
    approve.add_argument("--reference-method", help=argparse.SUPPRESS)
    approve.add_argument("--verification-note", help=argparse.SUPPRESS)
    approve.set_defaults(function=approve_packet)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.function(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        RuntimeError,
        subprocess.CalledProcessError,
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
