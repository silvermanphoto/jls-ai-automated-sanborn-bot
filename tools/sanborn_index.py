#!/usr/bin/env python3
"""Build local geographic seeds from the georeferenced 1911 Sanborn index.

Apple's built-in Vision recognizer reads overlapping disposable index crops.
Repeated readings of the same printed sheet number are clustered, then mapped
through the index GeoTIFF's geotransform into EPSG:3857.  These are approximate
location safeguards, never final control points.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import fcntl
from functools import wraps
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable

from PIL import Image


TOOLS_DIR = Path(__file__).resolve().parent
NATIVE_SOURCE = TOOLS_DIR / "sanborn_vision_ocr.m"
DEFAULT_INDEX = Path(
    "/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/"
    "Stage 1 -Orthorectified Atlanta Maps to print/"
    "1911 Sanborn Index Orthorectified_OSM_9point_finetuned_2026-07-14.tif"
)
MAX_MANUAL_SEED_DISTANCE = 250.0


def fail(message: str) -> "NoReturn":
    raise RuntimeError(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_sha256(path: Path) -> tuple[str, dict[str, int]]:
    """Hash one regular file, refusing an identity change during the read."""
    before = path.stat()
    fingerprint = {
        "device": int(before.st_dev),
        "inode": int(before.st_ino),
        "size": int(before.st_size),
        "mtime_ns": int(before.st_mtime_ns),
    }
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    current = {
        "device": int(after.st_dev),
        "inode": int(after.st_ino),
        "size": int(after.st_size),
        "mtime_ns": int(after.st_mtime_ns),
    }
    if current != fingerprint:
        fail(f"File changed while its identity was being recorded: {path}")
    return digest.hexdigest(), fingerprint


def atomic_write_json(path: Path, record: dict, expected_sha256: str | None) -> None:
    """Create or replace one JSON file atomically without a lost update."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if expected_sha256 is None:
        if path.exists():
            fail(f"Manual override appeared during confirmation; no changes were written: {path}")
        mode = 0o644
    else:
        if not path.is_file():
            fail(f"Manual override disappeared during confirmation; no changes were written: {path}")
        current_digest, _ = stable_sha256(path)
        if current_digest != expected_sha256:
            fail(f"Manual override changed during confirmation; no changes were written: {path}")
        mode = path.stat().st_mode & 0o777
    payload = (json.dumps(record, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if expected_sha256 is None:
            # A hard link is an atomic create-without-overwrite on this same
            # filesystem.  A concurrent creator wins instead of being erased.
            try:
                os.link(temporary, path)
            except FileExistsError:
                fail(
                    f"Manual override appeared during confirmation; no changes were written: {path}"
                )
            temporary.unlink()
        else:
            # Check again at the final replacement boundary.  The old file
            # remains untouched if either this check or os.replace fails.
            current_digest, _ = stable_sha256(path)
            if current_digest != expected_sha256:
                fail(
                    f"Manual override changed during confirmation; no changes were written: {path}"
                )
            os.replace(temporary, path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            # The file replacement itself is already atomic.  Some filesystems
            # do not permit explicitly syncing a directory.
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


def run(command: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        fail(f"Required local program is unavailable: {command[0]}")
    except subprocess.TimeoutExpired:
        fail(f"Local command timed out after {timeout} seconds: {command[0]}")
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        fail(f"Local command failed ({command[0]}): {detail}")


def raster_metadata(index_raster: Path) -> dict:
    payload = json.loads(run(["gdalinfo", "-json", str(index_raster)]).stdout)
    transform = payload.get("geoTransform")
    size = payload.get("size")
    wkt = payload.get("coordinateSystem", {}).get("wkt", "")
    if not isinstance(transform, list) or len(transform) != 6:
        fail("The index raster does not have a six-value geotransform.")
    if not isinstance(size, list) or len(size) != 2:
        fail("The index raster dimensions could not be read.")
    if "Pseudo-Mercator" not in wkt and "3857" not in wkt:
        fail("The index raster must be georeferenced in EPSG:3857.")
    return {
        "width": int(size[0]),
        "height": int(size[1]),
        "geotransform": [float(value) for value in transform],
        "crs": "EPSG:3857",
    }


def make_preview(index_raster: Path, output: Path, width: int) -> tuple[int, int]:
    output.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "gdal_translate",
            "-q",
            "-of",
            "PNG",
            "-outsize",
            str(width),
            "0",
            "-b",
            "1",
            "-b",
            "2",
            "-b",
            "3",
            str(index_raster),
            str(output),
        ]
    )
    with Image.open(output) as image:
        return int(image.width), int(image.height)


def compile_native_recognizer(binary: Path, module_cache: Path) -> None:
    clang = shutil.which("clang")
    if clang is None:
        fail("Apple clang is required to compile the local Vision helper.")
    if not NATIVE_SOURCE.is_file():
        fail(f"Native Vision source is missing: {NATIVE_SOURCE}")
    binary.parent.mkdir(parents=True, exist_ok=True)
    module_cache.mkdir(parents=True, exist_ok=True)
    lock_path = binary.with_name(f".{binary.name}.compile.lock")
    with lock_path.open("a+b") as lock:
        # All local workers may start together.  Only one compiles; followers
        # wait here, then see the completed binary on the second freshness test.
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if binary.is_file() and binary.stat().st_mtime_ns >= NATIVE_SOURCE.stat().st_mtime_ns:
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{binary.name}.", suffix=".compiling", dir=binary.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            temporary.unlink()
            run(
                [
                    clang,
                    "-fobjc-arc",
                    "-fmodules",
                    f"-fmodules-cache-path={module_cache}",
                    "-framework",
                    "Foundation",
                    "-framework",
                    "AppKit",
                    "-framework",
                    "Vision",
                    str(NATIVE_SOURCE),
                    "-o",
                    str(temporary),
                ]
            )
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                fail("Apple clang did not produce a usable local Vision helper.")
            os.chmod(temporary, 0o755)
            smoke_image = binary.parent / f".{binary.name}.{os.getpid()}.smoke.png"
            try:
                Image.new("RGB", (64, 64), "white").save(smoke_image)
                smoke = run([str(temporary), str(smoke_image)], timeout=60)
                rows = json.loads(smoke.stdout)
                if not isinstance(rows, list):
                    fail("The newly compiled local Vision helper failed its JSON smoke test.")
            except json.JSONDecodeError as error:
                fail(f"The newly compiled local Vision helper returned invalid JSON: {error}")
            finally:
                if smoke_image.exists():
                    smoke_image.unlink()
            os.replace(temporary, binary)
        finally:
            if temporary.exists():
                temporary.unlink()


def make_crops(
    preview: Path, folder: Path, crop_size: int, stride: int
) -> dict[str, dict]:
    folder.mkdir(parents=True, exist_ok=True)
    crops: dict[str, dict] = {}
    with Image.open(preview) as image:
        width, height = image.size
        x_starts = list(range(0, max(1, width - crop_size + 1), stride))
        y_starts = list(range(0, max(1, height - crop_size + 1), stride))
        if not x_starts or x_starts[-1] != max(0, width - crop_size):
            x_starts.append(max(0, width - crop_size))
        if not y_starts or y_starts[-1] != max(0, height - crop_size):
            y_starts.append(max(0, height - crop_size))
        for top in sorted(set(y_starts)):
            for left in sorted(set(x_starts)):
                right = min(width, left + crop_size)
                bottom = min(height, top + crop_size)
                path = folder / f"crop-y{top:05d}-x{left:05d}.png"
                image.crop((left, top, right, bottom)).save(path)
                crops[str(path)] = {
                    "left": left,
                    "top": top,
                    "width": right - left,
                    "height": bottom - top,
                }
    return crops


def recognize_crops(binary: Path, crops: dict[str, dict]) -> list[dict]:
    result = run([str(binary), *crops.keys()], timeout=1200)
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        fail(f"Apple Vision returned invalid JSON: {error}")
    if not isinstance(rows, list):
        fail("Apple Vision result was not a list.")
    return rows


def digit_groups(text: str, maximum: int = 549) -> list[tuple[int, float]]:
    """Return plausible tile numbers and their horizontal text fractions."""
    matches = list(re.finditer(r"(?<!\d)(\d{1,3})(?!\d)", text))
    result = []
    for match in matches:
        number = int(match.group(1))
        if not 1 <= number <= maximum:
            continue
        fraction = (match.start() + match.end()) / (2 * max(1, len(text)))
        result.append((number, fraction))
    return result


def observations_to_detections(
    observations: Iterable[dict], crops: dict[str, dict], maximum: int = 549
) -> list[dict]:
    detections: list[dict] = []
    for row in observations:
        crop = crops.get(str(row.get("image", "")))
        if crop is None:
            continue
        text = str(row.get("text", ""))
        groups = digit_groups(text, maximum)
        if not groups:
            continue
        box_height = float(row["height"]) * crop["height"]
        if not 5 <= box_height <= 90:
            continue
        left = crop["left"] + float(row["x"]) * crop["width"]
        box_width = float(row["width"]) * crop["width"]
        center_y = crop["top"] + (
            1.0 - (float(row["y"]) + float(row["height"]) / 2.0)
        ) * crop["height"]
        for number, fraction in groups:
            detections.append(
                {
                    "tile": number,
                    "preview_x": left + box_width * fraction,
                    "preview_y": center_y,
                    "confidence": float(row.get("confidence", 0)),
                    "crop": str(row["image"]),
                    "vision_text": text,
                    "exact_digits": text.strip() == str(number),
                }
            )
    return detections


def cluster_detections(detections: Iterable[dict], radius: float = 55.0) -> dict[int, list[dict]]:
    by_tile: dict[int, list[dict]] = defaultdict(list)
    for detection in detections:
        by_tile[int(detection["tile"])].append(detection)
    result: dict[int, list[dict]] = {}
    for tile, rows in by_tile.items():
        clusters: list[list[dict]] = []
        for row in sorted(rows, key=lambda item: -float(item["confidence"])):
            best_cluster = None
            best_distance = math.inf
            for cluster in clusters:
                center_x = sum(item["preview_x"] for item in cluster) / len(cluster)
                center_y = sum(item["preview_y"] for item in cluster) / len(cluster)
                distance = math.hypot(row["preview_x"] - center_x, row["preview_y"] - center_y)
                if distance <= radius and distance < best_distance:
                    best_cluster = cluster
                    best_distance = distance
            if best_cluster is None:
                clusters.append([row])
            else:
                best_cluster.append(row)
        summaries = []
        for cluster in clusters:
            unique_crops = len({row["crop"] for row in cluster})
            weights = [max(0.1, float(row["confidence"])) for row in cluster]
            weight_sum = sum(weights)
            exact_count = sum(bool(row["exact_digits"]) for row in cluster)
            summaries.append(
                {
                    "preview_x": sum(row["preview_x"] * weight for row, weight in zip(cluster, weights)) / weight_sum,
                    "preview_y": sum(row["preview_y"] * weight for row, weight in zip(cluster, weights)) / weight_sum,
                    "support": unique_crops,
                    "detection_count": len(cluster),
                    "exact_digit_reads": exact_count,
                    "mean_confidence": sum(float(row["confidence"]) for row in cluster) / len(cluster),
                    "score": unique_crops * 3.0 + exact_count * 1.5 + sum(float(row["confidence"]) for row in cluster),
                    "vision_texts": sorted({row["vision_text"] for row in cluster})[:8],
                }
            )
        result[tile] = sorted(summaries, key=lambda item: -float(item["score"]))
    return result


def pixel_to_map(x: float, y: float, transform: list[float]) -> tuple[float, float]:
    return (
        transform[0] + x * transform[1] + y * transform[2],
        transform[3] + x * transform[4] + y * transform[5],
    )


def build_seed_record(
    metadata: dict,
    preview_size: tuple[int, int],
    clusters: dict[int, list[dict]],
) -> list[dict]:
    preview_width, preview_height = preview_size
    seeds = []
    for tile in sorted(clusters):
        options = clusters[tile]
        best = options[0]
        full_x = float(best["preview_x"]) * metadata["width"] / preview_width
        full_y = float(best["preview_y"]) * metadata["height"] / preview_height
        map_x, map_y = pixel_to_map(full_x, full_y, metadata["geotransform"])
        runner_up = options[1]["score"] if len(options) > 1 else 0.0
        ambiguous = bool(runner_up >= 0.72 * best["score"])
        quality = "high" if best["support"] >= 3 and not ambiguous else "review"
        seeds.append(
            {
                "tile": tile,
                "map_x": map_x,
                "map_y": map_y,
                "crs": "EPSG:3857",
                "suggested_max_distance": 250.0,
                "quality": quality,
                "ambiguous": ambiguous,
                "support": best["support"],
                "mean_confidence": best["mean_confidence"],
                "preview_x": best["preview_x"],
                "preview_y": best["preview_y"],
                "alternatives": options[:4],
            }
        )
    return seeds


def map_to_pixel(x: float, y: float, transform: list[float]) -> tuple[float, float]:
    """Invert a six-value GDAL affine transform."""
    determinant = transform[1] * transform[5] - transform[2] * transform[4]
    if not math.isfinite(determinant) or abs(determinant) < 1e-15:
        fail("The index raster geotransform cannot be inverted.")
    delta_x = x - transform[0]
    delta_y = y - transform[3]
    return (
        (delta_x * transform[5] - delta_y * transform[2]) / determinant,
        (delta_y * transform[1] - delta_x * transform[4]) / determinant,
    )


def _same_geotransform(first: object, second: object) -> bool:
    try:
        left = [float(value) for value in first]  # type: ignore[arg-type]
        right = [float(value) for value in second]  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return len(left) == len(right) == 6 and all(
        math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=0, abs_tol=1e-9)
        for a, b in zip(left, right)
    )


def validate_index_relationship(record: dict, requested_raster: Path) -> tuple[Path, dict, str, dict]:
    """Prove that the JSON still describes the explicitly supplied live raster."""
    if record.get("schema_version") != 1:
        fail("Manual confirmation requires a schema-version 1 tile-location index.")
    stored = record.get("index")
    if not isinstance(stored, dict):
        fail("The index JSON has no raster evidence block.")
    stored_path_value = stored.get("path")
    if not isinstance(stored_path_value, str) or not Path(stored_path_value).is_absolute():
        fail("The index JSON must record an absolute index-raster path.")
    stored_path = Path(stored_path_value).expanduser().resolve()
    raster = requested_raster.expanduser().resolve()
    if not raster.is_file() or not stored_path.is_file():
        fail("The recorded and supplied index raster must both exist.")
    if raster != stored_path:
        fail("The supplied index raster is not the raster recorded by the index JSON.")

    digest, fingerprint = stable_sha256(raster)
    recorded_digest = stored.get("sha256")
    if recorded_digest is not None:
        if not isinstance(recorded_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded_digest):
            fail("The index JSON contains an invalid raster SHA256 value.")
        if not secrets_compare(recorded_digest, digest):
            fail("The index raster no longer matches the SHA256 recorded in the index JSON.")

    live = raster_metadata(raster)
    try:
        stored_width = int(stored["width"])
        stored_height = int(stored["height"])
    except (KeyError, TypeError, ValueError):
        fail("The index JSON is missing valid raster dimensions.")
    if (stored_width, stored_height) != (live["width"], live["height"]):
        fail("The live index raster dimensions do not match the index JSON.")
    if str(stored.get("crs", "")).upper() != "EPSG:3857" or live["crs"] != "EPSG:3857":
        fail("The index JSON and live raster must both use EPSG:3857.")
    if not _same_geotransform(stored.get("geotransform"), live["geotransform"]):
        fail("The live index raster geotransform does not match the index JSON.")
    return raster, live, digest, fingerprint


def secrets_compare(first: str, second: str) -> bool:
    """Compare fixed-format digests without an early-exit string comparison."""
    return hmac.compare_digest(first, second)


def validated_preview(record: dict) -> tuple[Path, int, int, dict]:
    preview = record.get("preview")
    if not isinstance(preview, dict):
        fail("Preview-pixel confirmation requires the preview evidence in the index JSON.")
    path_value = preview.get("path")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        fail("The index JSON must record an absolute preview path.")
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        fail(f"The recorded index preview does not exist: {path}")
    try:
        recorded_size = (int(preview["width"]), int(preview["height"]))
    except (KeyError, TypeError, ValueError):
        fail("The index JSON is missing valid preview dimensions.")
    recorded_digest = preview.get("sha256")
    recorded_bytes = preview.get("bytes")
    if (
        not isinstance(recorded_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", recorded_digest)
        or not isinstance(recorded_bytes, int)
        or recorded_bytes <= 0
    ):
        fail(
            "Preview-pixel confirmation requires SHA256 and byte-size evidence; "
            "rebuild this legacy tile-location index first."
        )
    digest, fingerprint = stable_sha256(path)
    if not secrets_compare(digest, recorded_digest) or fingerprint["size"] != recorded_bytes:
        fail("The live index preview no longer matches the preview recorded by the index JSON.")
    with Image.open(path) as image:
        actual_size = (int(image.width), int(image.height))
        image.verify()
    if actual_size != recorded_size or actual_size[0] <= 0 or actual_size[1] <= 0:
        fail("The recorded index preview dimensions do not match the live preview image.")
    return (
        path,
        *actual_size,
        {
            "path": str(path),
            "sha256": digest,
            "bytes": fingerprint["size"],
            "width": actual_size[0],
            "height": actual_size[1],
        },
    )


def _deep_json_copy(value: object) -> object:
    return json.loads(json.dumps(value))


def manual_override_path(args: argparse.Namespace) -> Path:
    index_json = args.index_json.expanduser().resolve()
    override_directory_value = getattr(args, "override_dir", None)
    override_directory = (
        override_directory_value.expanduser().resolve()
        if override_directory_value is not None
        else index_json.parent / f"{index_json.stem}-manual-overrides"
    )
    return override_directory / f"tile-{int(args.tile):04d}.json"


def lock_manual_override(function):
    """Serialize every decision for one tile across local worker processes."""
    @wraps(function)
    def locked(args: argparse.Namespace) -> int:
        override_path = manual_override_path(args)
        override_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = override_path.with_name(f".{override_path.name}.lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return function(args)

    return locked


@lock_manual_override
def cmd_confirm(args: argparse.Namespace) -> int:
    """Write one audited per-tile override without changing the shared OCR index."""
    index_json = args.index_json.expanduser().resolve()
    if not index_json.is_file():
        fail(f"Tile-location index JSON does not exist: {index_json}")
    if not 1 <= int(args.tile) <= 549:
        fail("The tile number must be between 1 and 549.")
    reviewer = str(args.reviewer).strip()
    note = str(args.note).strip()
    if not reviewer or not note:
        fail("Manual confirmation requires a nonblank reviewer and note.")
    tolerance = float(args.tolerance)
    if not math.isfinite(tolerance) or not 0 < tolerance <= MAX_MANUAL_SEED_DISTANCE:
        fail("Manual seed tolerance must be positive and no greater than 250 meters.")

    original_bytes = index_json.read_bytes()
    original_digest = hashlib.sha256(original_bytes).hexdigest()
    stable_index_digest, _ = stable_sha256(index_json)
    if not secrets_compare(original_digest, stable_index_digest):
        fail("The tile-location index changed while it was being read.")
    try:
        record = json.loads(original_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"The tile-location index JSON is invalid: {error}")
    if not isinstance(record, dict):
        fail("The tile-location index JSON must contain one object.")
    raster, metadata, raster_digest, raster_fingerprint = validate_index_relationship(
        record, args.index_raster
    )

    preview_coordinates = args.preview_pixel
    map_coordinates = args.map_coordinate
    if (preview_coordinates is None) == (map_coordinates is None):
        fail("Supply exactly one of --preview-pixel or --map-coordinate.")
    preview_x = preview_y = None
    preview_evidence = None
    if preview_coordinates is not None:
        _preview_path, preview_width, preview_height, preview_evidence = validated_preview(record)
        preview_x, preview_y = (float(value) for value in preview_coordinates)
        if not all(math.isfinite(value) for value in (preview_x, preview_y)):
            fail("Preview coordinates must be finite numbers.")
        if not (0 <= preview_x < preview_width and 0 <= preview_y < preview_height):
            fail("Preview coordinates lie outside the recorded index preview.")
        full_x = preview_x * metadata["width"] / preview_width
        full_y = preview_y * metadata["height"] / preview_height
        map_x, map_y = pixel_to_map(full_x, full_y, metadata["geotransform"])
        method = "preview-pixel"
        input_evidence = {
            "preview_x": preview_x,
            "preview_y": preview_y,
            "preview": preview_evidence,
        }
    else:
        map_x, map_y = (float(value) for value in map_coordinates)
        if not all(math.isfinite(value) for value in (map_x, map_y)):
            fail("Map coordinates must be finite numbers.")
        full_x, full_y = map_to_pixel(map_x, map_y, metadata["geotransform"])
        method = "map-coordinate"
        input_evidence = {"map_x": map_x, "map_y": map_y, "crs": "EPSG:3857"}
    if not (0 <= full_x < metadata["width"] and 0 <= full_y < metadata["height"]):
        fail("The manual location lies outside the georeferenced index raster.")

    seeds = record.get("seeds")
    if not isinstance(seeds, list) or any(not isinstance(seed, dict) for seed in seeds):
        fail("The index JSON seeds collection is malformed.")
    try:
        previous = [seed for seed in seeds if int(seed.get("tile", -1)) == int(args.tile)]
    except (TypeError, ValueError):
        fail("The index JSON contains a seed with an invalid tile number.")
    high_confidence_ocr = (
        len(previous) == 1
        and previous[0].get("quality") == "high"
        and not bool(previous[0].get("ambiguous"))
    )
    if high_confidence_ocr and not args.override_high_confidence:
        fail(
            f"Tile {args.tile} already has one unique high-quality OCR seed; "
            "use --override-high-confidence only after visibly confirming it is wrong."
        )

    override_path = manual_override_path(args)
    existing_override = None
    expected_override_digest = None
    if override_path.exists() and not override_path.is_file():
        fail(f"The manual override destination is not a regular file: {override_path}")
    if override_path.is_file():
        if not args.replace_existing:
            fail(
                f"Tile {args.tile} already has a manual override; rerun with "
                "--replace-existing to make a deliberate replacement."
            )
        override_bytes = override_path.read_bytes()
        expected_override_digest = hashlib.sha256(override_bytes).hexdigest()
        stable_override_digest, _ = stable_sha256(override_path)
        if not secrets_compare(expected_override_digest, stable_override_digest):
            fail("The existing manual override changed while it was being read.")
        try:
            existing_override = json.loads(override_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            fail(f"The existing manual override is invalid JSON: {error}")
        if not isinstance(existing_override, dict):
            fail("The existing manual override must contain one object.")
        if (
            existing_override.get("schema_version") != 1
            or existing_override.get("record_type")
            != "sanborn-manual-tile-seed-override"
            or existing_override.get("tile") != int(args.tile)
        ):
            fail("The existing manual override has the wrong schema or tile number.")
        base_evidence = existing_override.get("base_index")
        if not isinstance(base_evidence, dict):
            fail("The existing manual override has no base-index evidence.")
        if (
            base_evidence.get("path") != str(index_json)
            or base_evidence.get("sha256") != original_digest
        ):
            fail(
                "The shared OCR index changed since this manual override was reviewed; "
                "do not carry the old decision forward."
            )
        old_raster = existing_override.get("index_raster")
        if not isinstance(old_raster, dict) or (
            old_raster.get("path") != str(raster)
            or old_raster.get("sha256") != raster_digest
        ):
            fail("The index raster changed since this manual override was reviewed.")
        history = existing_override.get("history")
        if not isinstance(history, list):
            fail("The existing manual override history is malformed.")
        override_seeds = existing_override.get("seeds")
        if (
            not isinstance(override_seeds, list)
            or len(override_seeds) != 1
            or not isinstance(override_seeds[0], dict)
            or override_seeds[0].get("tile") != int(args.tile)
        ):
            fail("The existing manual override must contain exactly one selected tile seed.")
        prior_manual_seed = override_seeds[0]
        created_utc = existing_override.get("created_utc")
        if not isinstance(created_utc, str) or not created_utc:
            fail("The existing manual override has no creation timestamp.")
    else:
        history = []
        prior_manual_seed = None
        created_utc = None

    confirmed_utc = utc_now()
    raster_evidence = {
        "path": str(raster),
        "sha256": raster_digest,
        "size": raster_fingerprint["size"],
        "mtime_ns": raster_fingerprint["mtime_ns"],
        "width": metadata["width"],
        "height": metadata["height"],
        "geotransform": metadata["geotransform"],
        "crs": "EPSG:3857",
    }
    original_ocr = _deep_json_copy(previous)
    confirmation = {
        "reviewer": reviewer,
        "note": note,
        "confirmed_utc": confirmed_utc,
        "method": method,
        "input": input_evidence,
        "index_json_path": str(index_json),
        "base_index_sha256": original_digest,
        "index_raster": raster_evidence,
        "overrode_high_confidence_ocr": high_confidence_ocr,
    }
    manual_seed = {
        "tile": int(args.tile),
        "map_x": map_x,
        "map_y": map_y,
        "crs": "EPSG:3857",
        "suggested_max_distance": tolerance,
        "quality": "high",
        "ambiguous": False,
        "support": 1,
        "source": "manual-confirmation",
        "index_pixel_x": full_x,
        "index_pixel_y": full_y,
        "original_ocr_evidence": original_ocr,
        "manual_confirmation": confirmation,
    }
    if preview_x is not None and preview_y is not None:
        manual_seed["preview_x"] = preview_x
        manual_seed["preview_y"] = preview_y

    history.append(
        {
            **confirmation,
            "tile": int(args.tile),
            "tolerance": tolerance,
            "replaced_manual_seed": _deep_json_copy(prior_manual_seed),
        }
    )
    override_record = {
        "schema_version": 1,
        "record_type": "sanborn-manual-tile-seed-override",
        "created_utc": created_utc or confirmed_utc,
        "modified_utc": confirmed_utc,
        "tile": int(args.tile),
        "purpose": "reviewed per-tile location seed; shared OCR index remains immutable",
        "recognizer": "explicit human confirmation on the georeferenced index",
        "base_index": {
            "path": str(index_json),
            "sha256": original_digest,
            "schema_version": record.get("schema_version"),
            "created_utc": record.get("created_utc"),
        },
        "index": raster_evidence,
        "index_raster": raster_evidence,
        "base_seed_records": original_ocr,
        "seed_count": 1,
        "seeds": [manual_seed],
        "history": history,
    }
    if preview_evidence is not None:
        override_record["preview"] = preview_evidence

    final_raster_digest, _ = stable_sha256(raster)
    if not secrets_compare(final_raster_digest, raster_digest):
        fail("The index raster changed during manual confirmation; no changes were written.")
    final_index_digest, _ = stable_sha256(index_json)
    if not secrets_compare(final_index_digest, original_digest):
        fail("The shared OCR index changed during manual confirmation; no changes were written.")
    if preview_evidence is not None:
        final_preview_digest, final_preview_fingerprint = stable_sha256(
            Path(preview_evidence["path"])
        )
        if (
            not secrets_compare(final_preview_digest, preview_evidence["sha256"])
            or final_preview_fingerprint["size"] != preview_evidence["bytes"]
        ):
            fail("The index preview changed during manual confirmation; no changes were written.")
    atomic_write_json(override_path, override_record, expected_override_digest)
    print(f"Confirmed tile {args.tile}: {map_x:.3f}, {map_y:.3f} EPSG:3857")
    print(f"Manual tile override: {override_path}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    index_raster = args.index.expanduser().resolve()
    output = args.output.expanduser().resolve()
    cache = args.cache.expanduser().resolve()
    if not index_raster.is_file():
        fail(f"Georeferenced Sanborn index is missing: {index_raster}")
    index_digest, index_fingerprint = stable_sha256(index_raster)
    metadata = raster_metadata(index_raster)
    cache.mkdir(parents=True, exist_ok=True)
    preview = cache / "index-preview.png"
    preview_size = make_preview(index_raster, preview, args.preview_width)
    preview_digest, preview_fingerprint = stable_sha256(preview)
    binary = cache / "sanborn-vision-ocr"
    compile_native_recognizer(binary, cache / "clang-module-cache")
    crop_sizes = args.crop_size or [720, 480, 320]
    with tempfile.TemporaryDirectory(prefix="sanborn-index-crops-") as temporary:
        crops: dict[str, dict] = {}
        for crop_size in crop_sizes:
            stride = args.stride or max(120, round(crop_size * 0.68))
            crops.update(
                make_crops(
                    preview,
                    Path(temporary) / f"size-{crop_size}",
                    crop_size,
                    stride,
                )
            )
        observations = recognize_crops(binary, crops)
        detections = observations_to_detections(observations, crops, args.maximum_tile)
    clusters = cluster_detections(detections, args.cluster_radius)
    seeds = build_seed_record(metadata, preview_size, clusters)
    current_index_digest, current_index_fingerprint = stable_sha256(index_raster)
    if (
        not secrets_compare(index_digest, current_index_digest)
        or current_index_fingerprint != index_fingerprint
    ):
        fail("The index raster changed while its OCR location record was being built.")
    current_preview_digest, current_preview_fingerprint = stable_sha256(preview)
    if (
        not secrets_compare(preview_digest, current_preview_digest)
        or current_preview_fingerprint != preview_fingerprint
    ):
        fail("The index preview changed while its OCR location record was being built.")
    record = {
        "schema_version": 1,
        "created_utc": utc_now(),
        "purpose": "approximate wrong-neighborhood safeguard; never final controls",
        "index": {
            "path": str(index_raster),
            "sha256": index_digest,
            "size": index_fingerprint["size"],
            "mtime_ns": index_fingerprint["mtime_ns"],
            **metadata,
        },
        "preview": {
            "path": str(preview),
            "sha256": preview_digest,
            "bytes": preview_fingerprint["size"],
            "width": preview_size[0],
            "height": preview_size[1],
        },
        "recognizer": "Apple Vision accurate local OCR with overlapping crops",
        "crop_sizes": crop_sizes,
        "stride": args.stride or "68 percent of each crop size",
        "detection_count": len(detections),
        "seed_count": len(seeds),
        "seeds": seeds,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    high = sum(seed["quality"] == "high" for seed in seeds)
    print(f"Tile-location index: {output}")
    print(f"Seeds: {len(seeds)} total, {high} high-confidence; raw detections: {len(detections)}")
    return 0


def cmd_lookup(args: argparse.Namespace) -> int:
    record = json.loads(args.index_json.expanduser().resolve().read_text(encoding="utf-8"))
    matches = [seed for seed in record.get("seeds", []) if int(seed["tile"]) == args.tile]
    if len(matches) != 1:
        fail(f"Tile {args.tile} does not have exactly one location seed.")
    print(json.dumps(matches[0], indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Read tile locations from the georeferenced index")
    build.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--cache", type=Path, required=True)
    build.add_argument("--preview-width", type=int, default=3000)
    build.add_argument(
        "--crop-size",
        type=int,
        action="append",
        default=[],
        help="Repeat to override the default 720, 480, and 320 pixel passes",
    )
    build.add_argument(
        "--stride",
        type=int,
        default=0,
        help="Fixed crop stride; default is 68 percent of each crop size",
    )
    build.add_argument("--cluster-radius", type=float, default=55.0)
    build.add_argument("--maximum-tile", type=int, default=549)
    build.set_defaults(function=cmd_build)
    lookup = commands.add_parser("lookup", help="Show one approximate tile-location safeguard")
    lookup.add_argument("index_json", type=Path)
    lookup.add_argument("tile", type=int)
    lookup.set_defaults(function=cmd_lookup)
    confirm = commands.add_parser(
        "confirm",
        help="Replace a missing or ambiguous OCR location with a reviewed manual seed",
    )
    confirm.add_argument("index_json", type=Path)
    confirm.add_argument("tile", type=int)
    confirm.add_argument(
        "--index-raster",
        type=Path,
        required=True,
        help="The existing georeferenced index raster recorded by the JSON",
    )
    confirm.add_argument(
        "--override-dir",
        type=Path,
        help=(
            "Directory for immutable per-tile override records; default is a "
            "named sibling of the shared index JSON"
        ),
    )
    coordinates = confirm.add_mutually_exclusive_group(required=True)
    coordinates.add_argument(
        "--preview-pixel",
        nargs=2,
        type=float,
        metavar=("X", "Y"),
        help="Tile-center pixel in the recorded index preview",
    )
    coordinates.add_argument(
        "--map-coordinate",
        nargs=2,
        type=float,
        metavar=("X", "Y"),
        help="Tile-center EPSG:3857 map coordinate",
    )
    confirm.add_argument("--reviewer", required=True)
    confirm.add_argument("--note", required=True)
    confirm.add_argument("--tolerance", type=float, required=True)
    confirm.add_argument(
        "--replace-existing",
        action="store_true",
        help="Deliberately supersede an existing manual confirmation for this tile",
    )
    confirm.add_argument(
        "--override-high-confidence",
        action="store_true",
        help="Deliberately correct a unique high-confidence OCR seed that is visibly wrong",
    )
    confirm.set_defaults(function=cmd_confirm)
    return parser


def main() -> int:
    try:
        arguments = build_parser().parse_args()
        return arguments.function(arguments)
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
