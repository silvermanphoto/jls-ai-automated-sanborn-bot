#!/usr/bin/env python3
"""Propose three Sanborn controls from local spatial OCR and local OSM data.

This tool does not approve its own work.  It converts OCR label positions into
street axes, resolves modern names through exact OSM names and reviewed aliases,
uses only genuine shared OSM nodes as targets, ranks wide three-control sets, and
writes a compact proposal for ChatGPT or Joel to approve or correct.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import difflib
from itertools import combinations
import json
import math
from pathlib import Path
import re
import sqlite3
import statistics
import sys
from typing import Iterable

from sanborn_georeference import (
    affine_diagnostics,
    affine_safety_warnings,
    split_affine_safety_warnings,
)
from sanborn_geometry import StreetGeometry, constant_axis, intersect_axes
from sanborn_osm import lookup_intersections, normalize_name as normalize_osm_name
from sanborn_ocr import clean_token


STREET_TYPES = {
    "street",
    "avenue",
    "road",
    "boulevard",
    "drive",
    "lane",
    "court",
    "circle",
    "parkway",
    "place",
    "terrace",
    "trail",
    "highway",
    "way",
    "alley",
}
QUADRANTS = {"northeast", "northwest", "southeast", "southwest"}
GENERIC_OCR_WORDS = {
    "alley",
    "building",
    "brick",
    "collection",
    "company",
    "dwelling",
    "floor",
    "frame",
    "green",
    "insurance",
    "map",
    "scale",
}


def fail(message: str) -> "NoReturn":
    raise RuntimeError(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def name_core(value: str) -> str:
    words = normalize_osm_name(value).split()
    if words and words[-1] in QUADRANTS:
        words.pop()
    if words and words[-1] in STREET_TYPES:
        words.pop()
    return " ".join(words)


def load_aliases(path: Path) -> list[dict]:
    if not path.is_file():
        fail(f"Reviewed street-alias file does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    aliases = []
    for row in payload.get("aliases", []):
        if row.get("status") != "approved":
            continue
        historic = str(row.get("historic", "")).strip()
        modern = str(row.get("modern", "")).strip()
        evidence = str(row.get("evidence", "")).strip()
        if not historic or not modern or not evidence:
            fail("Every approved alias needs historic, modern, and evidence text.")
        aliases.append(
            {
                **row,
                "historic_core": name_core(historic),
                "modern_normalized": normalize_osm_name(modern),
            }
        )
    return aliases


def osm_name_catalog(database: Path) -> list[dict]:
    if not database.is_file():
        fail(f"Local OSM database does not exist: {database}")
    uri = f"file:{database}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT normalized_name, MIN(raw_name) AS raw_name,
                   GROUP_CONCAT(DISTINCT source_tag) AS source_tags,
                   COUNT(DISTINCT osm_way_id) AS way_count
            FROM way_names
            GROUP BY normalized_name
            ORDER BY normalized_name
            """
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "normalized": row["normalized_name"],
            "raw_name": row["raw_name"],
            "source_tags": str(row["source_tags"]).split(","),
            "way_count": int(row["way_count"]),
            "core": name_core(row["normalized_name"]),
        }
        for row in rows
    ]


def _ocr_tokens(ocr: dict, aliases: list[dict] | None = None) -> list[dict]:
    """Collect conservative labels plus lower-confidence words for fuzzy aliases."""
    tokens: list[dict] = []
    seen: set[tuple[str, int, int, str]] = set()
    for label in ocr.get("street_labels", []):
        normalized = name_core(str(label.get("normalized", label.get("text", ""))))
        if not normalized:
            continue
        row = {
            "historic_text": str(label.get("text", label.get("name", ""))),
            "historic_core": normalized,
            "source_x": float(label["source_x"]),
            "source_y": float(label["source_y"]),
            "orientation": str(label["orientation"]),
            "ocr_confidence": float(label.get("confidence", 0)),
            "ocr_strength": str(label.get("strength", "suggestion")),
        }
        key = (
            normalized,
            round(row["source_x"] / 50),
            round(row["source_y"] / 50),
            row["orientation"],
        )
        if key not in seen:
            seen.add(key)
            tokens.append(row)

    for line in ocr.get("text_lines", []):
        for word in line.get("source_words", []):
            cleaned = clean_token(str(word.get("text", "")))
            if (
                len(cleaned) < 4
                or not re.fullmatch(r"[A-Z']+", cleaned)
                or cleaned.casefold() in GENERIC_OCR_WORDS
            ):
                continue
            confidence = float(word.get("confidence", 0))
            core = name_core(cleaned)
            resembles_reviewed_alias = any(
                difflib.SequenceMatcher(None, core, alias["historic_core"]).ratio() >= 0.82
                for alias in (aliases or [])
            )
            if confidence < 18 and not resembles_reviewed_alias:
                continue
            if not core:
                continue
            row = {
                "historic_text": cleaned.title(),
                "historic_core": core,
                "source_x": float(word["source_x"]),
                "source_y": float(word["source_y"]),
                "orientation": str(line["orientation"]),
                "ocr_confidence": confidence,
                "ocr_strength": "word" if confidence >= 18 else "low-confidence-alias",
            }
            key = (
                core,
                round(row["source_x"] / 50),
                round(row["source_y"] / 50),
                row["orientation"],
            )
            if key not in seen:
                seen.add(key)
                tokens.append(row)
    for word in ocr.get("low_confidence_words", []):
        cleaned = clean_token(str(word.get("cleaned", word.get("text", ""))))
        core = name_core(cleaned)
        if not core:
            continue
        resembles_reviewed_alias = any(
            difflib.SequenceMatcher(None, core, alias["historic_core"]).ratio() >= 0.82
            for alias in (aliases or [])
        )
        if not resembles_reviewed_alias:
            continue
        row = {
            "historic_text": cleaned.title(),
            "historic_core": core,
            "source_x": float(word["source_x"]),
            "source_y": float(word["source_y"]),
            "orientation": str(word["orientation"]),
            "ocr_confidence": float(word.get("confidence", 0)),
            "ocr_strength": "low-confidence-alias",
        }
        key = (
            core,
            round(row["source_x"] / 50),
            round(row["source_y"] / 50),
            row["orientation"],
        )
        if key not in seen:
            seen.add(key)
            tokens.append(row)
    return tokens


def resolve_ocr_names(
    ocr: dict,
    catalog: list[dict],
    aliases: list[dict],
    *,
    fuzzy_threshold: float = 0.82,
) -> list[dict]:
    by_core: dict[str, list[dict]] = defaultdict(list)
    for name in catalog:
        if name["core"]:
            by_core[name["core"]].append(name)

    resolved: list[dict] = []
    for token in _ocr_tokens(ocr, aliases):
        core = token["historic_core"]
        matches: list[dict] = []
        if core in by_core:
            matches.extend(
                {**name, "match_method": "exact-name", "match_score": 1.0}
                for name in by_core[core]
            )
        for alias in aliases:
            ratio = difflib.SequenceMatcher(None, core, alias["historic_core"]).ratio()
            if core == alias["historic_core"] or ratio >= fuzzy_threshold:
                modern = [
                    name
                    for name in catalog
                    if name["normalized"] == alias["modern_normalized"]
                    or name["core"] == name_core(alias["modern_normalized"])
                ]
                method = "approved-alias" if core == alias["historic_core"] else "fuzzy-approved-alias"
                matches.extend(
                    {
                        **name,
                        "match_method": method,
                        "match_score": ratio,
                        "alias": alias,
                    }
                    for name in modern
                )
        if not matches and len(core) >= 5:
            best_ratio = 0.0
            best_cores: set[str] = set()
            for candidate_core in by_core:
                ratio = difflib.SequenceMatcher(None, core, candidate_core).ratio()
                if ratio > best_ratio + 0.01:
                    best_ratio = ratio
                    best_cores = {candidate_core}
                elif abs(ratio - best_ratio) <= 0.01:
                    best_cores.add(candidate_core)
            if best_ratio >= 0.90:
                for candidate_core in best_cores:
                    matches.extend(
                        {
                            **name,
                            "match_method": "fuzzy-name",
                            "match_score": best_ratio,
                        }
                        for name in by_core[candidate_core]
                    )

        unique: dict[str, dict] = {}
        for match in matches:
            key = str(match["normalized"])
            existing = unique.get(key)
            if existing is None or float(match["match_score"]) > float(existing["match_score"]):
                unique[key] = match
        if unique:
            resolved.append({**token, "modern_matches": list(unique.values())[:8]})
    return resolved


def _weighted_axis(labels: list[dict], orientation: str) -> tuple[float, dict]:
    ordered = sorted(labels, key=lambda item: item["ocr_confidence"], reverse=True)
    best = ordered[0]
    coordinate = best["source_y"] if orientation == "horizontal" else best["source_x"]
    # When repeated labels agree, use their median; otherwise the clearest label is safer.
    comparable = [
        item["source_y"] if orientation == "horizontal" else item["source_x"]
        for item in ordered
        if item["ocr_confidence"] >= best["ocr_confidence"] - 15
    ]
    if len(comparable) >= 2 and max(comparable) - min(comparable) < 0.08 * max(comparable):
        coordinate = statistics.median(comparable)
    return float(coordinate), best


def build_street_axes(
    resolved: list[dict], geometry: StreetGeometry | None = None
) -> list[dict]:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for label in resolved:
        for modern in label["modern_matches"]:
            groups[
                (
                    label["historic_core"],
                    modern["normalized"],
                    label["orientation"],
                )
            ].append({**label, "modern": modern})

    axes: list[dict] = []
    for (historic_core, modern_name, orientation), labels in groups.items():
        coordinate, best = _weighted_axis(labels, orientation)
        axes.append(
            {
                "historic_core": historic_core,
                "historic_text": best["historic_text"],
                "modern_normalized": modern_name,
                "modern_raw": best["modern"]["raw_name"],
                "orientation": orientation,
                "axis_coordinate": coordinate,
                "ocr_confidence": best["ocr_confidence"],
                "match_method": best["modern"]["match_method"],
                "match_score": best["modern"]["match_score"],
                "labels": [
                    {
                        key: label[key]
                        for key in (
                            "historic_text",
                            "source_x",
                            "source_y",
                            "orientation",
                            "ocr_confidence",
                            "ocr_strength",
                        )
                    }
                    for label in labels
                ],
            }
        )
    # A suffixless label such as FORT can legitimately match Fort Street, Fort
    # Drive, or Fort Place. Preserve those alternatives so the intersection graph
    # can decide; never let alphabetical order silently choose one. A reviewed
    # historical alias, however, is stronger evidence and supersedes same-core
    # modern false friends such as Butler Lane.
    alternatives: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for axis in axes:
        alternatives[(axis["historic_core"], axis["orientation"])].append(axis)
    retained: list[dict] = []
    method_rank = {
        "approved-alias": 4,
        "exact-name": 3,
        "fuzzy-approved-alias": 2,
        "fuzzy-name": 1,
    }
    for rows in alternatives.values():
        reviewed = [row for row in rows if row["match_method"] == "approved-alias"]
        pool = reviewed or rows
        pool.sort(
            key=lambda row: (
                -method_rank.get(row["match_method"], 0),
                -float(row["match_score"]),
                -float(row["ocr_confidence"]),
                len(str(row["modern_normalized"])),
                str(row["modern_normalized"]),
            )
        )
        retained.extend(pool[:6])
    geometry_cache: dict[tuple[str, str], dict] = {}
    for axis in retained:
        key = (axis["historic_core"], axis["orientation"])
        if key not in geometry_cache:
            if geometry is None:
                geometry_cache[key] = constant_axis(
                    axis["orientation"], axis["axis_coordinate"]
                )
            else:
                geometry_cache[key] = geometry.axis_for_labels(
                    axis["labels"], axis["orientation"]
                )
        axis["axis_model"] = geometry_cache[key]
    return retained


def build_control_candidates(database: Path, axes: list[dict]) -> list[dict]:
    horizontal = [axis for axis in axes if axis["orientation"] == "horizontal"]
    vertical = [axis for axis in axes if axis["orientation"] == "vertical"]
    candidates: list[dict] = []
    seen: set[tuple[str, str, int]] = set()
    for h_axis in horizontal:
        for v_axis in vertical:
            if h_axis["historic_core"] == v_axis["historic_core"]:
                continue
            intersections = lookup_intersections(
                database,
                h_axis["modern_normalized"],
                v_axis["modern_normalized"],
            )
            try:
                source_x, source_y = intersect_axes(
                    h_axis["axis_model"], v_axis["axis_model"]
                )
            except RuntimeError:
                continue
            for intersection in intersections:
                key = (
                    h_axis["historic_core"],
                    v_axis["historic_core"],
                    int(intersection["osm_node_id"]),
                )
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    {
                        "candidate_id": len(candidates) + 1,
                        "historic_street_a": h_axis["historic_text"],
                        "historic_street_b": v_axis["historic_text"],
                        "modern_street_a": h_axis["modern_raw"],
                        "modern_street_b": v_axis["modern_raw"],
                        "label": f"{h_axis['historic_text']} x {v_axis['historic_text']}",
                        "source_x": source_x,
                        "source_y": source_y,
                        "target_x": float(intersection["x_3857"]),
                        "target_y": float(intersection["y_3857"]),
                        "lon": float(intersection["lon"]),
                        "lat": float(intersection["lat"]),
                        "osm_node_id": int(intersection["osm_node_id"]),
                        "osm_matches": intersection["matches"],
                        "source_evidence": {"horizontal": h_axis, "vertical": v_axis},
                        "match_methods": [h_axis["match_method"], v_axis["match_method"]],
                    }
                )
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    for candidate in candidates:
        pair_counts[(candidate["historic_street_a"], candidate["historic_street_b"])] += 1
    for candidate in candidates:
        candidate["pair_ambiguity_count"] = pair_counts[
            (candidate["historic_street_a"], candidate["historic_street_b"])
        ]
    return candidates


def _diagnostics_for_triplet(triplet: tuple[dict, dict, dict], width: int, height: int) -> dict:
    controls = [
        {
            "source_x": candidate["source_x"],
            "source_line_gdal": candidate["source_y"],
            "map_x": candidate["target_x"],
            "map_y": candidate["target_y"],
        }
        for candidate in triplet
    ]
    return affine_diagnostics(controls, width, height)


def rank_triplets(
    candidates: list[dict],
    width: int,
    height: int,
    *,
    limit: int = 12,
    expected_target_seed: tuple[float, float] | None = None,
    max_seed_distance: float = 250.0,
) -> list[dict]:
    ranked: list[dict] = []
    for triplet in combinations(candidates, 3):
        ids = [candidate["candidate_id"] for candidate in triplet]
        if len(set(ids)) != 3:
            continue
        source_points = [(candidate["source_x"], candidate["source_y"]) for candidate in triplet]
        target_points = [(candidate["target_x"], candidate["target_y"]) for candidate in triplet]
        target_centroid = (
            statistics.fmean(point[0] for point in target_points),
            statistics.fmean(point[1] for point in target_points),
        )
        seed_distance = None
        if expected_target_seed is not None:
            if any(
                abs(point[0] - expected_target_seed[0]) > max_seed_distance
                or abs(point[1] - expected_target_seed[1]) > max_seed_distance
                for point in target_points
            ):
                continue
            seed_distance = math.hypot(
                target_centroid[0] - expected_target_seed[0],
                target_centroid[1] - expected_target_seed[1],
            )
            if seed_distance > max_seed_distance:
                continue
        if min(
            math.hypot(a[0] - b[0], a[1] - b[1])
            for a, b in combinations(source_points, 2)
        ) < 120:
            continue
        if max(
            math.hypot(a[0] - b[0], a[1] - b[1])
            for a, b in combinations(target_points, 2)
        ) > 2500:
            continue
        try:
            diagnostics = _diagnostics_for_triplet(triplet, width, height)
        except RuntimeError:
            continue
        ratio = float(diagnostics["scale_ratio"])
        angle = float(diagnostics["axis_angle_degrees"])
        coverage = float(diagnostics["source_triangle_coverage"] or 0)
        if bool(diagnostics["unexpected_mirroring"]) or ratio > 1.35 or not 75 <= angle <= 105:
            continue
        methods = [method for candidate in triplet for method in candidate["match_methods"]]
        method_penalty = sum(
            0 if method == "exact-name" else 2 if method == "approved-alias" else 8
            for method in methods
        )
        ambiguity_penalty = sum(max(0, int(candidate["pair_ambiguity_count"]) - 1) * 4 for candidate in triplet)
        score = (
            abs(math.log(ratio)) * 120
            + abs(angle - 90) * 2
            + max(0, 0.15 - coverage) * 160
            + method_penalty
            + ambiguity_penalty
            + (0.0 if seed_distance is None else 8.0 * seed_distance / max_seed_distance)
        )
        strict_warnings = affine_safety_warnings(diagnostics)
        ranked.append(
            {
                "candidate_ids": ids,
                "score": score,
                "diagnostics": diagnostics,
                "target_centroid": list(target_centroid),
                "target_seed_distance": seed_distance,
                "strict_safety_warnings": strict_warnings,
                "needs_chatgpt_review": bool(strict_warnings)
                or any("fuzzy" in method for method in methods)
                or any(int(candidate["pair_ambiguity_count"]) > 1 for candidate in triplet),
            }
        )
    ranked.sort(key=lambda row: (row["score"], row["candidate_ids"]))
    return ranked[:limit]


def write_points(path: Path, controls: Iterable[dict]) -> None:
    rows = list(controls)
    if len(rows) != 3:
        fail("Exactly three controls are required to write a QGIS points file.")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "#CRS: EPSG:3857",
        "mapX,mapY,sourceX,sourceY,enable",
    ]
    for control in rows:
        lines.append(
            f"{control['target_x']:.15f},{control['target_y']:.15f},"
            f"{control['source_x']:.6f},{-float(control['source_y']):.6f},1"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_propose(args: argparse.Namespace) -> int:
    ocr_path = args.ocr.expanduser().resolve()
    database = args.osm_db.expanduser().resolve()
    aliases_path = args.aliases.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not ocr_path.is_file():
        fail(f"Spatial OCR record does not exist: {ocr_path}")
    ocr = json.loads(ocr_path.read_text(encoding="utf-8"))
    catalog = osm_name_catalog(database)
    aliases = load_aliases(aliases_path)
    resolved = resolve_ocr_names(ocr, catalog, aliases)
    geometry = None
    preview_path = Path(str(ocr.get("preview", {}).get("path", "")))
    if preview_path.is_file():
        geometry = StreetGeometry(
            preview_path,
            int(ocr["source"]["width"]),
            int(ocr["source"]["height"]),
        )
    axes = build_street_axes(resolved, geometry)
    candidates = build_control_candidates(database, axes)
    width = int(ocr["source"]["width"])
    height = int(ocr["source"]["height"])
    expected_seed = tuple(args.expected_target_seed) if args.expected_target_seed else None
    if expected_seed is not None and args.max_seed_distance <= 0:
        fail("Maximum seed distance must be positive.")
    triplets = rank_triplets(
        candidates,
        width,
        height,
        limit=args.limit,
        expected_target_seed=expected_seed,
        max_seed_distance=args.max_seed_distance,
    )
    record = {
        "schema_version": 1,
        "created_utc": utc_now(),
        "tile": args.tile,
        "status": "needs-chatgpt-review",
        "source": ocr["source"],
        "spatial_ocr_path": str(ocr_path),
        "osm_database": str(database),
        "aliases_path": str(aliases_path),
        "resolved_labels": resolved,
        "street_axes": axes,
        "control_candidates": candidates,
        "ranked_triplets": triplets,
        "expected_target_seed": list(expected_seed) if expected_seed else None,
        "max_seed_distance": args.max_seed_distance if expected_seed else None,
        "instructions": (
            "Approve a ranked triplet only after checking its source crosshairs and "
            "OSM/Kauffman overlays. Fuzzy names and multiple OSM nodes remain ambiguous."
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"Control proposal: {output}")
    print(
        f"Resolved OCR labels: {len(resolved)}; street axes: {len(axes)}; "
        f"shared-node candidates: {len(candidates)}; ranked triplets: {len(triplets)}"
    )
    for index, triplet in enumerate(triplets[:5]):
        labels = [
            next(candidate["label"] for candidate in candidates if candidate["candidate_id"] == candidate_id)
            for candidate_id in triplet["candidate_ids"]
        ]
        print(
            f"  {index}: {' | '.join(labels)}; ratio "
            f"{triplet['diagnostics']['scale_ratio']:.4f}; angle "
            f"{triplet['diagnostics']['axis_angle_degrees']:.2f}; "
            f"review={'yes' if triplet['needs_chatgpt_review'] else 'approval only'}"
        )
    if not triplets:
        print("No defensible three-control proposal was found; ChatGPT street review is required.")
    return 0


def _parse_source_corrections(values: list[str]) -> dict[int, tuple[float, float]]:
    corrections: dict[int, tuple[float, float]] = {}
    for value in values:
        parts = value.split(",")
        if len(parts) != 3:
            fail("A source correction must be CONTROL_NUMBER,SOURCE_X,SOURCE_Y.")
        number = int(parts[0])
        if number not in {1, 2, 3}:
            fail("Source correction control number must be 1, 2, or 3.")
        corrections[number] = (float(parts[1]), float(parts[2]))
    return corrections


def cmd_export(args: argparse.Namespace) -> int:
    proposal_path = args.proposal.expanduser().resolve()
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    try:
        triplet = proposal["ranked_triplets"][args.triplet]
    except (IndexError, KeyError):
        fail(f"Triplet {args.triplet} does not exist in {proposal_path}.")
    by_id = {
        int(candidate["candidate_id"]): dict(candidate)
        for candidate in proposal["control_candidates"]
    }
    controls = [by_id[int(candidate_id)] for candidate_id in triplet["candidate_ids"]]
    corrections = _parse_source_corrections(args.source_correction)
    correction_record = []
    for index, control in enumerate(controls, start=1):
        if index in corrections:
            old = [control["source_x"], control["source_y"]]
            control["source_x"], control["source_y"] = corrections[index]
            correction_record.append(
                {
                    "control": index,
                    "old_source": old,
                    "new_source": list(corrections[index]),
                    "reason": args.correction_note,
                }
            )
    width = int(proposal["source"]["width"])
    height = int(proposal["source"]["height"])
    for index, control in enumerate(controls, start=1):
        source_x = float(control["source_x"])
        source_y = float(control["source_y"])
        if not (0 <= source_x < width and 0 <= source_y < height):
            fail(
                f"Control {index} source coordinate ({source_x}, {source_y}) is outside "
                f"the {width} x {height} source scan."
            )
    diagnostics = _diagnostics_for_triplet(tuple(controls), width, height)
    warnings = affine_safety_warnings(diagnostics)
    if correction_record and not args.correction_note.strip():
        fail("A correction note is required when source coordinates are changed.")
    distortion_warnings, hard_warnings = split_affine_safety_warnings(warnings)
    allow_distortion = bool(getattr(args, "allow_distortion", False))
    distortion_note = str(getattr(args, "distortion_note", "") or "").strip()
    if hard_warnings:
        fail("Corrected controls fail a non-overridable affine gate: " + "; ".join(hard_warnings))
    if distortion_warnings and not allow_distortion:
        fail("Corrected controls still fail the affine safety gate: " + "; ".join(distortion_warnings))
    if allow_distortion and not distortion_note:
        fail("--allow-distortion requires a nonblank --distortion-note.")
    if distortion_note and not allow_distortion:
        fail("--distortion-note requires --allow-distortion.")
    if allow_distortion and not distortion_warnings:
        fail("--allow-distortion was supplied, but these controls have no scale or angle warning.")
    points = args.points.expanduser().resolve()
    comparison = args.comparison.expanduser().resolve()
    write_points(points, controls)
    comparison_record = {
        "schema_version": 1,
        "created_utc": utc_now(),
        "tile": proposal.get("tile"),
        "proposal_path": str(proposal_path),
        "selected_triplet": args.triplet,
        "controls": controls,
        "corrections": correction_record,
        "diagnostics": diagnostics,
        "safety_warnings": warnings,
        "distortion_exception": {
            "allowed": allow_distortion,
            "note": distortion_note,
            "warnings": distortion_warnings,
        },
        "status": "proposed-points-awaiting-preview-approval",
    }
    comparison.parent.mkdir(parents=True, exist_ok=True)
    comparison.write_text(json.dumps(comparison_record, indent=2) + "\n", encoding="utf-8")
    print(f"Proposed QGIS points: {points}")
    print(f"Persistent comparison record: {comparison}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    propose = commands.add_parser("propose", help="Match spatial OCR to exact local OSM nodes")
    propose.add_argument("--tile", required=True, type=int)
    propose.add_argument("--ocr", required=True, type=Path)
    propose.add_argument("--osm-db", required=True, type=Path)
    propose.add_argument(
        "--aliases",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config" / "street_aliases.json",
    )
    propose.add_argument("--output", required=True, type=Path)
    propose.add_argument("--limit", type=int, default=12)
    propose.add_argument(
        "--expected-target-seed",
        nargs=2,
        type=float,
        metavar=("X", "Y"),
        help="Approximate EPSG:3857 center read from the georeferenced index",
    )
    propose.add_argument("--max-seed-distance", type=float, default=250.0)
    propose.set_defaults(function=cmd_propose)

    export = commands.add_parser("export", help="Select or correct one proposed triplet")
    export.add_argument("proposal", type=Path)
    export.add_argument("--triplet", type=int, default=0)
    export.add_argument("--source-correction", action="append", default=[])
    export.add_argument("--correction-note", default="")
    export.add_argument("--allow-distortion", action="store_true")
    export.add_argument("--distortion-note", default="")
    export.add_argument("--points", required=True, type=Path)
    export.add_argument("--comparison", required=True, type=Path)
    export.set_defaults(function=cmd_export)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    return arguments.function(arguments)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
