#!/usr/bin/env python3
"""Extract spatial street-name evidence from a Sanborn scan entirely on this Mac.

The scan is never rotated or rewritten.  Tesseract reads a disposable preview in
four orientations; every accepted text position is mapped back to the original
full-resolution source pixel coordinates.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable
import unicodedata

from PIL import Image

from sanborn_georeference import capture_json, require_program, sha256
from sanborn_index import NATIVE_SOURCE, compile_native_recognizer, recognize_crops


ROTATIONS = (0, 90, 180, 270)
SUFFIXES = {
    "ST": "STREET",
    "STR": "STREET",
    "STREET": "STREET",
    "AVE": "AVENUE",
    "AV": "AVENUE",
    "AVENUE": "AVENUE",
    "RD": "ROAD",
    "ROAD": "ROAD",
    "BLVD": "BOULEVARD",
    "BOUL": "BOULEVARD",
    "BOULEVARD": "BOULEVARD",
    "PL": "PLACE",
    "PLACE": "PLACE",
    "LN": "LANE",
    "LANE": "LANE",
    "DR": "DRIVE",
    "DRIVE": "DRIVE",
    "CT": "COURT",
    "COURT": "COURT",
    "TER": "TERRACE",
    "TERRACE": "TERRACE",
    "WAY": "WAY",
    "ALLEY": "ALLEY",
}
DIRECTIONALS = {
    "N": "NORTH",
    "NORTH": "NORTH",
    "S": "SOUTH",
    "SOUTH": "SOUTH",
    "E": "EAST",
    "EAST": "EAST",
    "W": "WEST",
    "WEST": "WEST",
    "NE": "NORTHEAST",
    "NW": "NORTHWEST",
    "SE": "SOUTHEAST",
    "SW": "SOUTHWEST",
}
NOISE_WORDS = {
    "ATLANTA",
    "GEORGIA",
    "SANBORN",
    "INSURANCE",
    "MAP",
    "COMPANY",
    "CO",
    "FEET",
    "SCALE",
    "DWG",
    "BLDG",
    "BUILDING",
    "FLOOR",
    "FRAME",
    "BRICK",
    "D",
}

VISION_TITLE_MINIMUM_CONFIDENCE = 0.8
VISION_TITLE_MINIMUM_TOP = 0.90
VISION_TITLE_MINIMUM_HEIGHT = 0.02
VISION_TITLE_EDGE_FRACTION = 0.15


def fail(message: str) -> "NoReturn":
    raise RuntimeError(message)


def clean_token(value: str) -> str:
    value = value.upper().replace("&", " AND ")
    value = re.sub(r"[^A-Z0-9']+", "", value)
    return value.strip("'")


def normalize_name(value: str) -> str:
    """Normalize OCR street text without pretending that two names are identical."""
    tokens = [clean_token(token) for token in value.split()]
    tokens = [token for token in tokens if token and not token.isdigit()]
    if not tokens:
        return ""
    normalized: list[str] = []
    for token in tokens:
        if token in SUFFIXES:
            normalized.append(SUFFIXES[token])
        elif token in DIRECTIONALS:
            normalized.append(DIRECTIONALS[token])
        else:
            normalized.append(token)
    return " ".join(normalized)


def normalize_printed_tile_text(value: str) -> str:
    """Keep only Unicode decimal digits after compatibility normalization.

    Apple Vision may return a title as ``No. 154`` rather than just ``154``.
    Removing non-digits permits that harmless variation, while a reading that
    contains another number (for example ``154 155``) still cannot pass an
    exact expected-tile comparison.
    """
    normalized = unicodedata.normalize("NFKC", str(value))
    return "".join(character for character in normalized if character.isdecimal())


def classify_printed_tile_observations(
    observations: Iterable[dict],
    expected_tile: int,
    *,
    minimum_confidence: float = VISION_TITLE_MINIMUM_CONFIDENCE,
    minimum_top: float = VISION_TITLE_MINIMUM_TOP,
    minimum_height: float = VISION_TITLE_MINIMUM_HEIGHT,
    edge_fraction: float = VISION_TITLE_EDGE_FRACTION,
) -> dict:
    """Accept only an exact Apple Vision reading in a Sanborn title corner.

    Vision uses bottom-left normalized coordinates.  A printed sheet number is
    therefore required to touch the top ten percent, have title-sized height,
    and sit at the left or right edge.  This prevents a building address or an
    adjacent-sheet index number elsewhere on the scan from proving identity.
    """
    expected_digits = str(int(expected_tile))
    reviewed: list[dict] = []
    accepted: list[dict] = []
    for raw in observations:
        text = str(raw.get("text", ""))
        normalized_digits = normalize_printed_tile_text(text)
        rejection_reasons: list[str] = []
        try:
            confidence = float(raw.get("confidence", 0.0))
            x = float(raw.get("x", -1.0))
            y = float(raw.get("y", -1.0))
            width = float(raw.get("width", 0.0))
            height = float(raw.get("height", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
            x = -1.0
            y = -1.0
            width = 0.0
            height = 0.0
            rejection_reasons.append("invalid-observation-values")

        if normalized_digits != expected_digits:
            rejection_reasons.append("not-exact-expected-digits")
        if confidence < minimum_confidence:
            rejection_reasons.append("confidence-below-minimum")
        if y + height < minimum_top:
            rejection_reasons.append("outside-top-title-strip")
        if height < minimum_height:
            rejection_reasons.append("text-too-small-for-title")
        if not (x < edge_fraction or x + width > 1.0 - edge_fraction):
            rejection_reasons.append("outside-left-or-right-title-corner")
        if not (
            math.isfinite(x)
            and math.isfinite(y)
            and math.isfinite(width)
            and math.isfinite(height)
            and math.isfinite(confidence)
            and 0.0 <= x <= 1.0
            and 0.0 <= y <= 1.0
            and 0.0 < width <= 1.0
            and 0.0 < height <= 1.0
            and x + width <= 1.001
            and y + height <= 1.001
        ):
            rejection_reasons.append("invalid-normalized-geometry")

        item = {
            "image": str(raw.get("image", "")),
            "text": text,
            "normalized_digits": normalized_digits,
            "confidence": confidence,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "accepted": not rejection_reasons,
            "rejection_reasons": rejection_reasons,
        }
        reviewed.append(item)
        if item["accepted"]:
            accepted.append(item)

    return {
        "seen": bool(accepted),
        "expected_digits": expected_digits,
        "criteria": {
            "normalized_digits_must_equal_expected": True,
            "minimum_confidence": minimum_confidence,
            "minimum_y_plus_height": minimum_top,
            "minimum_height": minimum_height,
            "left_edge_x_below": edge_fraction,
            "right_edge_x_plus_width_above": 1.0 - edge_fraction,
            "vision_coordinate_origin": "bottom-left",
        },
        "accepted_observations": accepted,
        "observations": reviewed,
    }


def verify_printed_tile_with_vision(
    preview: Path,
    expected_tile: int,
    cache: Path,
) -> dict:
    """Run the optional native title check without making OCR depend on it."""
    binary = cache / "sanborn-vision-ocr"
    module_cache = cache / "clang-module-cache"
    provenance = {
        "engine": "Apple Vision VNRecognizeTextRequest",
        "helper_source": str(NATIVE_SOURCE),
        "helper_source_sha256": sha256(NATIVE_SOURCE) if NATIVE_SOURCE.is_file() else None,
        "binary": str(binary),
        "preview": str(preview),
        "preview_sha256": sha256(preview),
    }
    try:
        compile_native_recognizer(binary, module_cache)
        observations = recognize_crops(binary, {str(preview): {}})
        result = classify_printed_tile_observations(observations, expected_tile)
        return {
            "method": "apple-vision-title-region",
            "status": "verified" if result["seen"] else "expected-title-not-found",
            "provenance": {
                **provenance,
                "binary_sha256": sha256(binary),
                "observation_count": len(observations),
            },
            **result,
        }
    except Exception as error:
        # Street OCR remains useful if Apple Vision or its local compiler is not
        # available.  Crucially, Tesseract text elsewhere on the sheet is never
        # promoted to proof of the printed sheet number.
        return {
            "method": "apple-vision-title-region",
            "status": "vision-unavailable",
            "seen": False,
            "expected_digits": str(int(expected_tile)),
            "criteria": {
                "normalized_digits_must_equal_expected": True,
                "minimum_confidence": VISION_TITLE_MINIMUM_CONFIDENCE,
                "minimum_y_plus_height": VISION_TITLE_MINIMUM_TOP,
                "minimum_height": VISION_TITLE_MINIMUM_HEIGHT,
                "left_edge_x_below": VISION_TITLE_EDGE_FRACTION,
                "right_edge_x_plus_width_above": 1.0 - VISION_TITLE_EDGE_FRACTION,
                "vision_coordinate_origin": "bottom-left",
            },
            "accepted_observations": [],
            "observations": [],
            "provenance": provenance,
            "error": f"{type(error).__name__}: {error}",
        }


def rotated_to_original(
    x: float,
    y: float,
    original_width: int,
    original_height: int,
    rotation: int,
) -> tuple[float, float]:
    """Map a point from a PIL counter-clockwise rotation back to the original."""
    rotation %= 360
    if rotation == 0:
        return x, y
    if rotation == 90:
        return original_width - y, x
    if rotation == 180:
        return original_width - x, original_height - y
    if rotation == 270:
        return y, original_height - x
    fail(f"Unsupported right-angle rotation: {rotation}")


def parse_tesseract_tsv(tsv: str, *, minimum_confidence: float = 20.0) -> list[dict]:
    """Turn Tesseract word rows into spatial text lines."""
    # OCR text can itself be a quote character. Disable CSV quote handling or one
    # stray map symbol can swallow the following TSV rows into a giant fake line.
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    groups: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    for row in reader:
        text = str(row.get("text", "")).strip()
        if not text:
            continue
        try:
            confidence = float(row.get("conf", "-1"))
            left = int(row["left"])
            top = int(row["top"])
            width = int(row["width"])
            height = int(row["height"])
        except (TypeError, ValueError, KeyError):
            continue
        if confidence < minimum_confidence:
            continue
        key = (
            str(row.get("block_num", "0")),
            str(row.get("par_num", "0")),
            str(row.get("line_num", "0")),
            str(row.get("page_num", "0")),
        )
        groups[key].append(
            {
                "text": text,
                "confidence": confidence,
                "left": left,
                "top": top,
                "right": left + width,
                "bottom": top + height,
                "word_num": int(row.get("word_num", "0") or 0),
            }
        )

    lines: list[dict] = []
    for words in groups.values():
        words.sort(key=lambda word: word["word_num"])
        lines.append(
            {
                "text": " ".join(word["text"] for word in words),
                "words": words,
                "confidence": sum(word["confidence"] for word in words) / len(words),
                "left": min(word["left"] for word in words),
                "top": min(word["top"] for word in words),
                "right": max(word["right"] for word in words),
                "bottom": max(word["bottom"] for word in words),
            }
        )
    return lines


def extract_street_phrases(text: str) -> list[dict]:
    """Extract conservative suffix-bearing names and weaker short label suggestions."""
    tokens = [clean_token(token) for token in text.split()]
    tokens = [token for token in tokens if token]
    results: list[dict] = []
    for index, token in enumerate(tokens):
        if token not in SUFFIXES:
            continue
        start = max(0, index - 4)
        prefix = tokens[start:index]
        while prefix and (prefix[0].isdigit() or prefix[0] in NOISE_WORDS):
            prefix.pop(0)
        prefix = [part for part in prefix if not part.isdigit() and part not in NOISE_WORDS]
        if not prefix:
            continue
        phrase = " ".join(prefix[-3:] + [token])
        normalized = normalize_name(phrase)
        if normalized:
            results.append({"name": phrase.title(), "normalized": normalized, "strength": "strong"})

    # Many Sanborn street labels omit the suffix. Preserve short, mostly alphabetic
    # lines as weak suggestions for the OSM resolver rather than silently accepting.
    alpha_tokens = [
        token
        for token in tokens
        if token not in NOISE_WORDS
        and not token.isdigit()
        and len(token) >= 2
        and re.fullmatch(r"[A-Z']+", token)
    ]
    if not results and 1 <= len(alpha_tokens) <= 3:
        phrase = " ".join(alpha_tokens)
        normalized = normalize_name(phrase)
        if len(normalized.replace(" ", "")) >= 4:
            results.append({"name": phrase.title(), "normalized": normalized, "strength": "weak"})
    return results


def _bbox_to_source(
    item: dict,
    *,
    rotation: int,
    preview_width: int,
    preview_height: int,
    source_width: int,
    source_height: int,
) -> tuple[list[float], float, float]:
    corners = [
        (item["left"], item["top"]),
        (item["right"], item["top"]),
        (item["right"], item["bottom"]),
        (item["left"], item["bottom"]),
    ]
    original = [
        rotated_to_original(x, y, preview_width, preview_height, rotation)
        for x, y in corners
    ]
    scale_x = source_width / preview_width
    scale_y = source_height / preview_height
    xs = [point[0] * scale_x for point in original]
    ys = [point[1] * scale_y for point in original]
    return [min(xs), min(ys), max(xs), max(ys)], sum(xs) / 4.0, sum(ys) / 4.0


def _line_to_source(
    line: dict,
    *,
    rotation: int,
    preview_width: int,
    preview_height: int,
    source_width: int,
    source_height: int,
    psm: int,
) -> dict:
    source_bbox, source_x, source_y = _bbox_to_source(
        line,
        rotation=rotation,
        preview_width=preview_width,
        preview_height=preview_height,
        source_width=source_width,
        source_height=source_height,
    )
    source_words = []
    for word in line["words"]:
        bbox, word_x, word_y = _bbox_to_source(
            word,
            rotation=rotation,
            preview_width=preview_width,
            preview_height=preview_height,
            source_width=source_width,
            source_height=source_height,
        )
        source_words.append(
            {
                "text": word["text"],
                "confidence": round(float(word["confidence"]), 3),
                "source_x": word_x,
                "source_y": word_y,
                "source_bbox": bbox,
            }
        )
    return {
        "text": line["text"],
        "normalized_text": normalize_name(line["text"]),
        "confidence": round(float(line["confidence"]), 3),
        "source_x": source_x,
        "source_y": source_y,
        "source_bbox": source_bbox,
        "source_words": source_words,
        "orientation": "horizontal" if rotation in {0, 180} else "vertical",
        "ocr_rotation_degrees": rotation,
        "psm": psm,
    }


def deduplicate_lines(lines: Iterable[dict], *, distance_pixels: float = 120.0) -> list[dict]:
    accepted: list[dict] = []
    for candidate in sorted(lines, key=lambda item: item["confidence"], reverse=True):
        normalized = candidate["normalized_text"]
        if not normalized or len(normalized) > 100:
            continue
        duplicate = False
        for existing in accepted:
            if normalized != existing["normalized_text"]:
                continue
            distance = math.hypot(
                candidate["source_x"] - existing["source_x"],
                candidate["source_y"] - existing["source_y"],
            )
            if distance <= distance_pixels:
                duplicate = True
                break
        if not duplicate:
            accepted.append(candidate)
    return sorted(accepted, key=lambda item: (item["source_y"], item["source_x"]))


def street_labels(lines: Iterable[dict]) -> list[dict]:
    labels: list[dict] = []
    for line in lines:
        for phrase in extract_street_phrases(line["text"]):
            if phrase["strength"] == "weak" and (
                float(line["confidence"]) < 60.0 or len(phrase["normalized"]) > 32
            ):
                continue
            label = dict(line)
            label.update(phrase)
            labels.append(label)
        # Also retain high-confidence individual words. The later local OSM index
        # is the authority on whether AUBURN, FORT, BUTLER, etc. are street names.
        for word in line.get("source_words", []):
            token = clean_token(word["text"])
            if (
                float(word["confidence"]) < 55.0
                or len(token) < 4
                or token in NOISE_WORDS
                or token in SUFFIXES
                or not re.fullmatch(r"[A-Z']+", token)
            ):
                continue
            label = {
                "text": word["text"],
                "normalized_text": normalize_name(word["text"]),
                "confidence": word["confidence"],
                "source_x": word["source_x"],
                "source_y": word["source_y"],
                "source_bbox": word["source_bbox"],
                "orientation": line["orientation"],
                "ocr_rotation_degrees": line["ocr_rotation_degrees"],
                "psm": line["psm"],
                "name": token.title(),
                "normalized": normalize_name(token),
                "strength": "weak-token",
            }
            labels.append(label)
    best: list[dict] = []
    for candidate in sorted(
        labels,
        key=lambda item: (item["strength"] == "strong", item["confidence"]),
        reverse=True,
    ):
        duplicate = False
        for existing in best:
            if candidate["normalized"] != existing["normalized"]:
                continue
            if candidate["orientation"] != existing["orientation"]:
                continue
            if math.hypot(
                candidate["source_x"] - existing["source_x"],
                candidate["source_y"] - existing["source_y"],
            ) <= 160:
                duplicate = True
                break
        if not duplicate:
            best.append(candidate)
    return sorted(
        best,
        key=lambda item: (
            item["strength"] != "strong",
            -float(item["confidence"]),
            item["normalized"],
        ),
    )


def _ocr_pass(arguments: tuple[Path, int, int, float]) -> tuple[int, int, str]:
    image_path, rotation, psm, minimum_confidence = arguments
    tesseract = require_program("tesseract")
    result = subprocess.run(
        [tesseract, str(image_path), "stdout", "-l", "eng", "--psm", str(psm), "tsv"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    # Parse once here so malformed TSV fails inside the worker, then return text to
    # keep coordinate conversion centralized in the caller.
    parse_tesseract_tsv(result.stdout, minimum_confidence=minimum_confidence)
    return rotation, psm, result.stdout


def scan(args: argparse.Namespace) -> int:
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not source.is_file():
        fail(f"Source scan does not exist: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    gdalinfo = require_program("gdalinfo")
    gdal_translate = require_program("gdal_translate")
    source_info = capture_json([gdalinfo, "-json", str(source)])
    source_width, source_height = map(int, source_info["size"])

    if args.preview:
        preview = args.preview.expanduser().resolve()
        if not preview.is_file():
            fail(f"Preview does not exist: {preview}")
    else:
        preview = output.with_suffix(".ocr-preview.png")
        subprocess.run(
            [
                gdal_translate,
                "-q",
                "-of",
                "PNG",
                "-outsize",
                str(args.preview_width),
                "0",
                str(source),
                str(preview),
            ],
            check=True,
        )

    # Street geometry and the printed-number gate both consume this disposable
    # image.  Bind the OCR record to its exact bytes so a later stale or
    # substituted preview cannot silently move proposed source intersections.
    preview_sha256_before = sha256(preview)
    preview_bytes_before = preview.stat().st_size

    with Image.open(preview) as base:
        base = base.convert("RGB")
        preview_width, preview_height = base.size
        with tempfile.TemporaryDirectory(prefix="sanborn-ocr-") as temporary:
            temporary_path = Path(temporary)
            rotated_paths: dict[int, Path] = {}
            for rotation in ROTATIONS:
                path = temporary_path / f"rotation-{rotation}.png"
                image = base if rotation == 0 else base.rotate(rotation, expand=True)
                image.save(path, optimize=False)
                rotated_paths[rotation] = path

            jobs = [
                (rotated_paths[rotation], rotation, psm, args.minimum_confidence)
                for rotation in ROTATIONS
                for psm in args.psm
            ]
            with ThreadPoolExecutor(max_workers=min(args.workers, len(jobs))) as executor:
                results = list(executor.map(_ocr_pass, jobs))

    mapped: list[dict] = []
    low_confidence_words: list[dict] = []
    pass_records: list[dict] = []
    for rotation, psm, tsv in results:
        parsed = parse_tesseract_tsv(tsv, minimum_confidence=args.minimum_confidence)
        pass_records.append({"rotation_degrees": rotation, "psm": psm, "line_count": len(parsed)})
        mapped.extend(
            _line_to_source(
                line,
                rotation=rotation,
                preview_width=preview_width,
                preview_height=preview_height,
                source_width=source_width,
                source_height=source_height,
                psm=psm,
            )
            for line in parsed
        )
        # Preserve low-confidence alphabetic words separately. They are not shown
        # as normal OCR suggestions, but a reviewed historical alias can rescue a
        # near miss such as POUSTON -> HOUSTON without rereading the scan.
        all_parsed = parse_tesseract_tsv(tsv, minimum_confidence=0.0)
        for line in all_parsed:
            mapped_line = _line_to_source(
                line,
                rotation=rotation,
                preview_width=preview_width,
                preview_height=preview_height,
                source_width=source_width,
                source_height=source_height,
                psm=psm,
            )
            for word in mapped_line["source_words"]:
                if float(word["confidence"]) >= args.minimum_confidence:
                    continue
                cleaned = clean_token(word["text"])
                if len(cleaned) < 4 or not re.fullmatch(r"[A-Z']+", cleaned):
                    continue
                low_confidence_words.append(
                    {
                        **word,
                        "cleaned": cleaned,
                        "orientation": mapped_line["orientation"],
                        "ocr_rotation_degrees": rotation,
                        "psm": psm,
                    }
                )

    lines = deduplicate_lines(mapped, distance_pixels=max(source_width, source_height) * 0.015)
    labels = street_labels(lines)
    low_word_best: dict[tuple[str, int, int, str], dict] = {}
    for word in low_confidence_words:
        key = (
            word["cleaned"],
            round(float(word["source_x"]) / 50),
            round(float(word["source_y"]) / 50),
            word["orientation"],
        )
        if key not in low_word_best or float(word["confidence"]) > float(
            low_word_best[key]["confidence"]
        ):
            low_word_best[key] = word
    expected_tile = str(args.tile) if args.tile is not None else None
    tesseract_tile_suggestions = []
    tile_verification = None
    if expected_tile:
        # Tesseract searches the whole sheet, so these hits are diagnostic hints
        # only.  A building number or adjacent-sheet label must never prove that
        # the downloaded scan is the expected tile.
        tile_pattern = re.compile(rf"(?<!\d){re.escape(expected_tile)}(?!\d)")
        tesseract_tile_suggestions = [
            line for line in lines if tile_pattern.search(line["text"])
        ]
        vision_cache = (
            args.vision_cache.expanduser().resolve()
            if args.vision_cache
            else output.parent / ".vision-cache"
        )
        tile_verification = verify_printed_tile_with_vision(
            preview,
            args.tile,
            vision_cache,
        )

    preview_sha256_after = sha256(preview)
    preview_bytes_after = preview.stat().st_size
    with Image.open(preview) as current_preview:
        current_preview_size = (int(current_preview.width), int(current_preview.height))
        current_preview.verify()
    if (
        preview_sha256_after != preview_sha256_before
        or preview_bytes_after != preview_bytes_before
        or current_preview_size != (preview_width, preview_height)
    ):
        fail(
            "The OCR preview changed while street and tile-number evidence was being "
            "built. No spatial OCR record was published."
        )
    if tile_verification is not None and (
        tile_verification.get("provenance", {}).get("preview_sha256")
        != preview_sha256_before
    ):
        fail("The printed-number proof is bound to a different OCR preview.")

    record = {
        "schema_version": 2,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": {
            "path": str(source),
            "sha256": sha256(source),
            "width": source_width,
            "height": source_height,
        },
        "preview": {
            "path": str(preview),
            "sha256": preview_sha256_before,
            "bytes": preview_bytes_before,
            "width": preview_width,
            "height": preview_height,
        },
        "orientation_policy": (
            "OCR previews were rotated; every coordinate below is mapped back to the "
            "untouched original source scan."
        ),
        "passes": pass_records,
        "expected_tile": args.tile,
        "printed_tile_number_method": (
            tile_verification["method"] if tile_verification else None
        ),
        "printed_tile_number_seen": (
            bool(tile_verification["seen"]) if tile_verification else None
        ),
        "printed_tile_number_hits": (
            tile_verification["accepted_observations"] if tile_verification else []
        ),
        "printed_tile_number_verification": tile_verification,
        "tesseract_tile_number_suggestions": tesseract_tile_suggestions,
        "street_labels": labels,
        "low_confidence_words": list(low_word_best.values()),
        "text_lines": lines,
    }
    output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"Spatial OCR record: {output}")
    print(f"Text lines: {len(lines)}; street-name suggestions: {len(labels)}")
    if expected_tile:
        assert tile_verification is not None
        if tile_verification["seen"]:
            tile_message = "verified in the printed title corner"
        elif tile_verification["status"] == "vision-unavailable":
            tile_message = "needs visual confirmation (Apple Vision unavailable)"
        else:
            tile_message = "needs visual confirmation (title-corner match not found)"
        print(f"Printed tile {expected_tile}: {tile_message}")
    for label in labels[:25]:
        print(
            f"  {label['name']} | {label['orientation']} | "
            f"source ({label['source_x']:.0f}, {label['source_y']:.0f}) | "
            f"confidence {label['confidence']:.1f} | {label['strength']}"
        )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--preview", type=Path, help="Existing disposable preview to reuse")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tile", type=int)
    parser.add_argument(
        "--vision-cache",
        type=Path,
        help="Reusable folder for the locally compiled Apple Vision title reader",
    )
    parser.add_argument("--preview-width", type=int, default=3200)
    parser.add_argument("--psm", type=int, action="append", default=[])
    parser.add_argument("--minimum-confidence", type=float, default=20.0)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if not args.psm:
        args.psm = [6, 11]
    if args.preview_width < 800:
        fail("Preview width must be at least 800 pixels for useful street OCR.")
    if args.workers < 1:
        fail("Worker count must be positive.")
    return args


def main() -> int:
    return scan(parse_args())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
