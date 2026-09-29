#!/usr/bin/env python3
"""Prepare verified Sanborn GeoTIFFs for Joel's live QGIS project.

This helper never imports PyQGIS itself.  It validates one or more local
``qgis-import`` manifests, builds a deterministic dry-run plan, and emits either
plain PyQGIS code or the JSON payload accepted by QGIS MCP's ``execute_code``
tool.  The emitted code performs every live check before changing the layer
tree and deliberately contains no project-save call.

Each sheet is shown through its paper recipe file ``<name>.clean.vrt`` (see
``sanborn_paper.py``), never the TIFF directly. ``migrate-to-recipes`` prints
code for the one-time switch of sheets already loaded from their TIFFs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable

import sanborn_archive
import sanborn_collections
import sanborn_paper
from sanborn_layer_names import intersection_for_proposal
from sanborn_review import REQUIRED_ARTIFACTS, require_approval
from sanborn_placement_policy import require_current_placement_evidence


# Which body of sheets this run is placing. Everything that is specific to the
# 1911 Atlanta sheets -- the project, the group, the index, how layers are named
# and drawn -- comes from this record, so another year or city is a new record
# in sanborn_collections rather than an edit here. See --collection.
COLLECTION = sanborn_collections.get()

# Kept as module-level names because the existing tests and callers read them.
PROTECTED_PROJECT = COLLECTION.project
EXPECTED_CRS = COLLECTION.crs
INDEX_LAYER_NAME = COLLECTION.index_layer_name
INDEX_LAYER_SOURCE = COLLECTION.index_layer_source
GROUP_NAME = COLLECTION.group_name
STYLE = dict(COLLECTION.style)
VERIFICATION_METHOD = "local-osm-and-kauffman"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_SHA256_CACHE: dict[tuple[str, int, int, int, int, int], str] = {}


class ManifestError(RuntimeError):
    """Raised when an import record cannot safely drive the live helper."""


def _fail(message: str) -> "NoReturn":
    raise ManifestError(message)


def _is_number(value: object, expected: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isclose(float(value), expected, rel_tol=0.0, abs_tol=1e-12)
    )


def _same_number_list(value: object, expected: list[float]) -> bool:
    return (
        isinstance(value, list)
        and len(value) == len(expected)
        and all(_is_number(actual, target) for actual, target in zip(value, expected))
    )


def tile_number_from_name(name: str) -> int | None:
    """Read a printed tile number without mistaking the map year for a tile."""
    patterns = (
        r"\bTile[\s_-]*(\d{1,4})(?!\d)",
        r"\bSanborn(?:\s+1[89]\d\d)?[\s_-]+(?!(?:1[89]\d\d)\b)(\d{1,4})(?!\d)",
    )
    for pattern in patterns:
        match = re.search(pattern, name, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def proposal_path_for_tile(tile: int, collection=None) -> Path:
    """Where the control proposal for one sheet is kept."""
    del collection  # Reserved: a future collection may keep proposals elsewhere.
    return (
        Path(__file__).resolve().parent.parent
        / "batch"
        / "proposals"
        / f"tile-{tile:04d}.json"
    )


def layer_name_for_tile(tile: int, collection=None) -> str:
    """What this sheet is called in Joel's layer list.

    ``Sanborn 1911 - tile 486 (Main & Washington)``.  The crossroads comes from
    the sheet's own control proposal, which already holds intersections read off
    the printed map and confirmed against a real junction in OpenStreetMap.  A
    sheet with no proposal -- the ones georeferenced before this engine existed
    -- simply gets no parenthesis rather than a wrong or invented one.

    Deliberately built from the tile number and the streets, never from the file
    name, which is how "_georeferenced" and the older working titles stop
    reaching the project.
    """
    active = collection or COLLECTION
    intersection = None
    proposal_file = proposal_path_for_tile(tile, active)
    if proposal_file.exists():
        try:
            proposal = json.loads(proposal_file.read_text(encoding="utf-8"))
            intersection = intersection_for_proposal(proposal, active.osm_database)
        except (OSError, ValueError, KeyError):
            # A naming aid must never stop a verified sheet from being placed.
            intersection = None
    return active.layer_name(tile, intersection)


def _required(record: dict[str, Any], key: str, manifest: Path) -> Any:
    if key not in record:
        _fail(f"{manifest}: missing required field {key!r}")
    return record[key]


def sha256(path: Path) -> str:
    """Return a streaming SHA-256 digest for a local file."""
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
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
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
        _fail(f"file changed while hashing: {resolved}")
    value = digest.hexdigest()
    _SHA256_CACHE[key] = value
    return value


def _required_sha256(record: dict[str, Any], key: str, manifest: Path) -> str:
    value = _required(record, key, manifest)
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        _fail(f"{manifest}: {key} must be a lowercase 64-character SHA-256 digest")
    return value


def _required_object(record: dict[str, Any], key: str, manifest: Path) -> dict[str, Any]:
    value = _required(record, key, manifest)
    if not isinstance(value, dict):
        _fail(f"{manifest}: {key} must be an object")
    return value


def _absolute_file(record: dict[str, Any], key: str, manifest: Path, label: str) -> Path:
    raw_path = _required(record, key, manifest)
    if not isinstance(raw_path, str) or not raw_path.strip():
        _fail(f"{manifest}: {key} must be a non-empty absolute filename")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        _fail(f"{manifest}: {label} path must be absolute")
    resolved = candidate.resolve()
    if not resolved.is_file():
        _fail(f"{manifest}: {label} does not exist: {resolved}")
    return resolved


def _absolute_nested_file(record: dict[str, Any], manifest: Path, label: str) -> Path:
    raw_path = record.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        _fail(f"{manifest}: {label} must name a non-empty absolute file")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        _fail(f"{manifest}: {label} path must be absolute")
    resolved = candidate.resolve()
    if not resolved.is_file():
        _fail(f"{manifest}: {label} does not exist: {resolved}")
    return resolved


def _verified_evidence_file(
    record: object,
    manifest: Path,
    label: str,
) -> dict[str, str]:
    if not isinstance(record, dict):
        _fail(f"{manifest}: {label} has no file evidence record")
    path = _absolute_nested_file(record, manifest, label)
    digest = record.get("sha256")
    if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
        _fail(f"{manifest}: {label} has no valid SHA-256")
    if sha256(path) != digest:
        _fail(f"{manifest}: {label} changed after approval")
    bytes_value = record.get("bytes")
    if bytes_value is not None and bytes_value != path.stat().st_size:
        _fail(f"{manifest}: {label} byte count changed after approval")
    return {"name": label, "path": str(path), "sha256": digest}


def _load_ledger(
    manifest: Path,
    ledger: Path,
    raster: Path,
    *,
    raster_sha256: str,
    source_sha256: str,
    points_sha256: str,
    tile: int,
    target_seed: dict[str, Any],
) -> dict[str, Any]:
    try:
        record = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"{manifest}: cannot read georeference ledger {ledger}: {exc}")
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        _fail(f"{manifest}: georeference ledger must be a schema-version 1 object")

    source = _required_object(record, "source", manifest)
    points = _required_object(record, "points", manifest)
    output = _required_object(record, "output", manifest)
    transformation = _required_object(record, "transformation", manifest)
    protected = _required_object(record, "protected_project", manifest)

    if source.get("sha256") != source_sha256:
        _fail(f"{manifest}: source_sha256 does not match the georeference ledger")
    if points.get("sha256") != points_sha256:
        _fail(f"{manifest}: points_sha256 does not match the georeference ledger")
    if output.get("sha256") != raster_sha256:
        _fail(f"{manifest}: raster_sha256 does not match the georeference ledger")

    source_path = _absolute_nested_file(source, manifest, "ledger source scan")
    points_path = _absolute_nested_file(points, manifest, "ledger control file")
    if sha256(source_path) != source_sha256:
        _fail(f"{manifest}: live source scan no longer matches source_sha256")
    if sha256(points_path) != points_sha256:
        _fail(f"{manifest}: live control file no longer matches points_sha256")

    raw_output = output.get("path")
    if not isinstance(raw_output, str) or not Path(raw_output).expanduser().is_absolute():
        _fail(f"{manifest}: georeference ledger output path must be absolute")
    if Path(raw_output).expanduser().resolve() != raster:
        _fail(f"{manifest}: georeference ledger identifies a different output raster")
    if output.get("bytes") != raster.stat().st_size:
        _fail(f"{manifest}: raster byte count does not match the georeference ledger")
    if output.get("bands") != ["Red", "Green", "Blue", "Alpha"]:
        _fail(f"{manifest}: georeference ledger does not prove an RGBA output")
    checksums = output.get("band_checksums")
    if (
        not isinstance(checksums, list)
        or len(checksums) != 4
        or any(not isinstance(value, int) or isinstance(value, bool) for value in checksums)
    ):
        _fail(f"{manifest}: georeference ledger band checksums are incomplete")
    if output.get("alpha_max") != 255:
        _fail(f"{manifest}: georeference ledger does not prove a fully opaque alpha value")
    if output.get("compression") != "DEFLATE" or output.get("predictor") != 2:
        _fail(f"{manifest}: georeference ledger does not prove required lossless compression")
    if str(transformation.get("target_crs", "")).upper() != EXPECTED_CRS:
        _fail(f"{manifest}: georeference ledger target CRS must be {EXPECTED_CRS}")

    if target_seed.get("status") != "selected" or target_seed.get("tile") != tile:
        _fail(f"{manifest}: target_seed must be the selected seed for tile {tile}")
    try:
        seed_x = float(target_seed["map_x"])
        seed_y = float(target_seed["map_y"])
        seed_distance = float(target_seed["suggested_max_distance"])
    except (KeyError, TypeError, ValueError):
        _fail(f"{manifest}: selected target_seed is missing numeric location values")
    if (
        not math.isfinite(seed_x)
        or not math.isfinite(seed_y)
        or not math.isfinite(seed_distance)
        or not 0 < seed_distance <= 250.0
    ):
        _fail(f"{manifest}: selected target_seed has an invalid location or tolerance")
    seed_record = target_seed.get("seed")
    if (
        not isinstance(seed_record, dict)
        or seed_record.get("ambiguous") is not False
        or str(seed_record.get("crs", "")).upper() != EXPECTED_CRS
    ):
        _fail(f"{manifest}: target_seed is ambiguous or not in {EXPECTED_CRS}")
    expected_bbox = [
        seed_x - seed_distance,
        seed_y - seed_distance,
        seed_x + seed_distance,
        seed_y + seed_distance,
    ]
    limits = transformation.get("safety_limits")
    if not isinstance(limits, dict):
        _fail(f"{manifest}: georeference ledger has no fit safety limits")
    if not _same_number_list(limits.get("expected_target_seed"), [seed_x, seed_y]):
        _fail(f"{manifest}: ledger target seed differs from the selected index seed")
    if not _same_number_list(limits.get("expected_target_bbox"), expected_bbox):
        _fail(f"{manifest}: ledger target bounds differ from the selected index seed")
    if not _is_number(limits.get("max_target_seed_distance"), seed_distance):
        _fail(f"{manifest}: ledger target-seed tolerance differs from the selected seed")
    diagnostics = transformation.get("diagnostics")
    location = diagnostics.get("target_location_check") if isinstance(diagnostics, dict) else None
    if (
        not isinstance(location, dict)
        or location.get("passed") is not True
        or location.get("target_seed_within_tolerance") is not True
        or location.get("all_target_controls_inside_expected_bbox") is not True
        or not _same_number_list(location.get("expected_target_seed"), [seed_x, seed_y])
        or not _same_number_list(location.get("expected_target_bbox"), expected_bbox)
    ):
        _fail(f"{manifest}: ledger does not prove the independent target-location gate passed")

    raw_project = protected.get("path")
    if not isinstance(raw_project, str) or not Path(raw_project).expanduser().is_absolute():
        _fail(f"{manifest}: georeference ledger protected-project path must be absolute")
    if not COLLECTION.names_protected_project(raw_project):
        _fail(f"{manifest}: georeference ledger names the wrong protected QGIS project")
    before = protected.get("mtime_ns_before")
    after = protected.get("mtime_ns_after")
    if (
        protected.get("unchanged_during_run") is not True
        or not isinstance(before, int)
        or isinstance(before, bool)
        or not isinstance(after, int)
        or isinstance(after, bool)
        or before != after
    ):
        _fail(f"{manifest}: ledger does not prove the protected QGIS project stayed unchanged")
    record["_validated_source_path"] = str(source_path)
    record["_validated_points_path"] = str(points_path)
    return record


def load_manifest(path: Path, *, for_import: bool = True) -> dict[str, Any]:
    """Verify frozen completion evidence; import also requires current eligibility.

    for_import=False is for preserving withdrawn results in the archive only.
    It must never produce an import plan. Display uses the collection style.
    """
    manifest = path.expanduser().resolve()
    if not manifest.is_file():
        _fail(f"QGIS import manifest does not exist: {manifest}")
    try:
        record = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"Cannot read QGIS import manifest {manifest}: {exc}")
    if not isinstance(record, dict):
        _fail(f"{manifest}: top-level JSON value must be an object")
    if record.get("schema_version") != 3:
        _fail(f"{manifest}: schema_version must be 3")

    tile = _required(record, "tile", manifest)
    if not isinstance(tile, int) or isinstance(tile, bool) or tile <= 0:
        _fail(f"{manifest}: tile must be a positive integer")
    if record.get("sort_key") != tile:
        _fail(f"{manifest}: sort_key must exactly match tile {tile}")
    if record.get("group") != GROUP_NAME:
        _fail(f"{manifest}: group must be exactly {GROUP_NAME!r}")
    if record.get("save_project") is not False:
        _fail(f"{manifest}: save_project must be false")
    if record.get("expanded") is not False:
        _fail(f"{manifest}: expanded must be false so RGB rows stay collapsed")

    # Completed manifests retain their display settings as history. Import
    # always applies STYLE from the current collection, never these old values.
    recorded_style = {key: record.get(key) for key in ("brightness", "gamma", "contrast", "opacity")}
    if any(not isinstance(value, (int, float)) or isinstance(value, bool)
           or not math.isfinite(value) for value in recorded_style.values()):
        _fail(f"{manifest}: historical display settings must be finite numbers")

    raster = _absolute_file(record, "path", manifest, "finished raster")
    printed = tile_number_from_name(raster.name)
    if printed != tile:
        _fail(
            f"{manifest}: raster filename identifies tile {printed!r}, "
            f"not manifest tile {tile}"
        )

    token = record.get("review_token")
    if not isinstance(token, str) or SHA256_PATTERN.fullmatch(token) is None:
        _fail(f"{manifest}: review_token must be a lowercase SHA-256 approval token")
    verification = record.get("geographic_verification")
    if not isinstance(verification, dict):
        _fail(f"{manifest}: geographic_verification must be an object")
    method = verification.get("reference_method")
    note = verification.get("note")
    if method != VERIFICATION_METHOD:
        _fail(
            f"{manifest}: geographic verification method must be exactly "
            f"{VERIFICATION_METHOD!r}"
        )
    if not isinstance(note, str) or not note.strip():
        _fail(f"{manifest}: geographic verification note must not be empty")
    artifact_sha256 = verification.get("artifact_sha256")
    if (
        not isinstance(artifact_sha256, dict)
        or not set(REQUIRED_ARTIFACTS).issubset(artifact_sha256)
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or SHA256_PATTERN.fullmatch(value) is None
            for key, value in artifact_sha256.items()
        )
    ):
        _fail(
            f"{manifest}: geographic verification must hash exactly the eight "
            "required review artifacts and every additional historical view"
        )

    target_seed = record.get("target_seed")
    if not isinstance(target_seed, dict):
        _fail(f"{manifest}: target_seed must contain the selected independent location seed")

    raw_review_dir = record.get("review_dir")
    if not isinstance(raw_review_dir, str) or not Path(raw_review_dir).expanduser().is_absolute():
        _fail(f"{manifest}: review_dir must be an absolute approval-packet folder")
    review_dir = Path(raw_review_dir).expanduser().resolve()
    if not review_dir.is_dir():
        _fail(f"{manifest}: review_dir does not exist: {review_dir}")
    review_json = review_dir / "review.json"
    approval_json = review_dir / "approval.json"
    if not review_json.is_file() or not approval_json.is_file():
        _fail(f"{manifest}: review_dir lacks review.json or approval.json")
    expected_review_digest = _required_sha256(record, "review_sha256", manifest)
    expected_approval_digest = _required_sha256(record, "approval_sha256", manifest)
    if sha256(review_json) != expected_review_digest:
        _fail(f"{manifest}: review.json changed after final verification")
    if sha256(approval_json) != expected_approval_digest:
        _fail(f"{manifest}: approval.json changed after final verification")
    try:
        approved_review, approval = require_approval(review_dir, frozen=True)
    except (RuntimeError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        _fail(f"{manifest}: approval packet no longer verifies: {exc}")
    approved_verification = approval.get("geographic_verification", {})
    if approved_review.get("approval_token") != token or approval.get("approval_token") != token:
        _fail(f"{manifest}: review_token does not match the approved packet")
    if approved_verification.get("reference_method") != "local-osm-and-kauffman-packet":
        _fail(f"{manifest}: approval packet does not certify both required local references")
    if approved_verification.get("note") != note:
        _fail(f"{manifest}: geographic verification note differs from the approval packet")
    if approved_verification.get("artifact_sha256") != artifact_sha256:
        _fail(f"{manifest}: artifact hashes do not match the approved packet")
    if approved_review.get("safety_limits", {}).get("target_seed_context") != target_seed:
        _fail(f"{manifest}: target_seed differs from the approved review packet")

    additional_evidence: list[dict[str, str]] = []
    review_provenance = approved_review.get("provenance")
    if not isinstance(review_provenance, dict):
        _fail(f"{manifest}: approved review lacks provenance inputs")
    for key, label in (
        ("osm_database", "approved local OSM database"),
        ("kauffman_map", "approved Kauffman reference map"),
    ):
        additional_evidence.append(
            _verified_evidence_file(review_provenance.get(key), manifest, label)
        )
    for key in ("historical_evidence", "historical_reference", "historical_measurement_preview"):
        if key in review_provenance:
            additional_evidence.append(_verified_evidence_file(review_provenance[key], manifest, key))
    for key, artifact in approved_review.get("artifacts", {}).items():
        if key not in REQUIRED_ARTIFACTS:
            additional_evidence.append(_verified_evidence_file(artifact, manifest, key))
    renderer_code = review_provenance.get("renderer_code")
    if not isinstance(renderer_code, dict) or not renderer_code:
        _fail(f"{manifest}: approved review lacks renderer-code provenance")
    # Renderer/font bytes and software versions stay bound by the original
    # token, but upgrading them cannot invalidate an already completed raster.

    seed_provenance = target_seed.get("provenance")
    seed_file = _verified_evidence_file(
        seed_provenance,
        manifest,
        "independent tile-seed record",
    )
    additional_evidence.append(seed_file)
    try:
        seed_record = json.loads(Path(seed_file["path"]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"{manifest}: independent tile-seed record is unreadable: {exc}")
    if not isinstance(seed_record, dict):
        _fail(f"{manifest}: independent tile-seed record must be an object")
    if seed_record.get("record_type") == "sanborn-manual-tile-seed-override":
        additional_evidence.append(
            _verified_evidence_file(
                seed_record.get("base_index"), manifest, "manual seed base OCR index"
            )
        )
        additional_evidence.append(
            _verified_evidence_file(
                seed_record.get("index_raster"), manifest, "manual seed index raster"
            )
        )
        if isinstance(seed_record.get("preview"), dict):
            additional_evidence.append(
                _verified_evidence_file(
                    seed_record.get("preview"), manifest, "manual seed index preview"
                )
            )
    else:
        additional_evidence.append(
            _verified_evidence_file(
                seed_record.get("index"), manifest, "automatic seed index raster"
            )
        )
        if isinstance(seed_record.get("preview"), dict):
            additional_evidence.append(
                _verified_evidence_file(
                    seed_record.get("preview"), manifest, "automatic seed index preview"
                )
            )

    deduplicated_evidence: list[dict[str, str]] = []
    seen_evidence_paths: set[str] = set()
    for evidence in additional_evidence:
        if evidence["path"] in seen_evidence_paths:
            continue
        seen_evidence_paths.add(evidence["path"])
        deduplicated_evidence.append(evidence)

    raster_digest = _required_sha256(record, "raster_sha256", manifest)
    ledger_digest = _required_sha256(record, "ledger_sha256", manifest)
    source_digest = _required_sha256(record, "source_sha256", manifest)
    points_digest = _required_sha256(record, "points_sha256", manifest)
    ledger = _absolute_file(record, "ledger_path", manifest, "georeference ledger")
    if ledger != raster.with_suffix(".georef.json"):
        _fail(f"{manifest}: ledger_path is not the raster's adjacent georeference ledger")
    try:
        actual_raster_digest = sha256(raster)
        if actual_raster_digest != raster_digest:
            _fail(f"{manifest}: finished raster SHA-256 does not match raster_sha256")
        vrt_path: Path | None = None
        vrt_digest: str | None = None
        if for_import:
            # QGIS shows the sheet through its paper recipe file. The recipe must
            # be exactly the one this hashed TIFF produces; the live code then
            # rehashes both files before it changes anything.
            try:
                vrt_path = sanborn_paper.vrt_path_for(raster)
            except sanborn_paper.RecipeError as exc:
                _fail(f"{manifest}: {exc}")
            recorded_vrt = record.get("vrt_path")
            if recorded_vrt is not None and Path(str(recorded_vrt)).expanduser().resolve() != vrt_path.resolve():
                _fail(f"{manifest}: vrt_path is not the raster's adjacent paper recipe file")
            try:
                sanborn_paper.verify_vrt(vrt_path, raster)
            except sanborn_paper.RecipeError as exc:
                _fail(
                    f"{manifest}: {exc}. Write it with: python3 tools/sanborn_paper.py "
                    f"backfill {str(raster.parent)!r}"
                )
            vrt_path = vrt_path.resolve()
            vrt_digest = sha256(vrt_path)
            if sha256(raster) != raster_digest:
                _fail(f"{manifest}: finished raster changed while its paper recipe was checked")
        actual_ledger_digest = sha256(ledger)
        if actual_ledger_digest != ledger_digest:
            _fail(f"{manifest}: georeference ledger SHA-256 does not match ledger_sha256")
        ledger_record = _load_ledger(
            manifest,
            ledger,
            raster,
            raster_sha256=raster_digest,
            source_sha256=source_digest,
            points_sha256=points_digest,
            tile=tile,
            target_seed=target_seed,
        )
    except OSError as exc:
        _fail(f"{manifest}: a verified QGIS input changed or became unreadable: {exc}")

    source_path = Path(ledger_record["_validated_source_path"])
    points_path = Path(ledger_record["_validated_points_path"])
    if Path(str(approved_review.get("source", {}).get("path", ""))).resolve() != source_path:
        _fail(f"{manifest}: approved packet names a different source scan")
    if Path(str(approved_review.get("points", {}).get("path", ""))).resolve() != points_path:
        _fail(f"{manifest}: approved packet names a different control file")
    if approved_review.get("source", {}).get("sha256") != source_digest:
        _fail(f"{manifest}: approved packet source hash differs from the final ledger")
    if approved_review.get("points", {}).get("sha256") != points_digest:
        _fail(f"{manifest}: approved packet control hash differs from the final ledger")
    artifact_files = []
    for key in REQUIRED_ARTIFACTS:
        artifact_record = approved_review.get("artifacts", {}).get(key, {})
        artifact_path = _absolute_nested_file(
            artifact_record, manifest, f"approved review artifact {key}"
        )
        if sha256(artifact_path) != artifact_sha256[key]:
            _fail(f"{manifest}: approved review artifact {key} changed")
        artifact_files.append(
            {"name": key, "path": str(artifact_path), "sha256": artifact_sha256[key]}
        )

    if for_import:
        try:
            require_current_placement_evidence(tile, approved_review)
        except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
            _fail(f"{manifest}: {exc}")

    return {
        "recorded_style": recorded_style,
        "tile": tile,
        "sort_key": tile,
        "path": str(raster),
        "vrt_path": str(vrt_path) if vrt_path is not None else None,
        "vrt_sha256": vrt_digest,
        "layer_name": layer_name_for_tile(tile),
        "manifest": str(manifest),
        "raster_sha256": raster_digest,
        "ledger_path": str(ledger),
        "ledger_sha256": ledger_digest,
        "source_sha256": source_digest,
        "points_sha256": points_digest,
        "source_path": str(source_path),
        "points_path": str(points_path),
        "review_dir": str(review_dir),
        "review_path": str(review_json),
        "review_sha256": expected_review_digest,
        "approval_path": str(approval_json),
        "approval_sha256": expected_approval_digest,
        "review_artifacts": artifact_files,
        "additional_evidence": deduplicated_evidence,
        "target_seed": target_seed,
        "review_token": token,
        "geographic_verification": verification,
    }


def load_manifests(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Load, reject duplicate tiles, and return manifests in numeric order."""
    records = [load_manifest(Path(path)) for path in paths]
    if not records:
        _fail("At least one QGIS import manifest is required")
    by_tile: dict[int, dict[str, Any]] = {}
    for record in records:
        tile = int(record["tile"])
        if tile in by_tile:
            _fail(f"Tile {tile} appears in more than one import manifest")
        by_tile[tile] = record
    return [by_tile[tile] for tile in sorted(by_tile)]


def load_preserved_variant_pair(path: Path) -> dict[str, Any]:
    """Bind an explicit decision to keep two existing full/alpha raster variants.

    This permits coexistence only; it never certifies geography or authorizes
    adding/replacing either variant through the verified-sheet import route.
    """
    record_path = path.expanduser().resolve()
    try:
        record_digest = sha256(record_path)
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(f"Cannot read existing-variant preservation record {record_path}: {exc}")
    if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("purpose") != "preserve-existing-full-alpha-pair":
        _fail("Existing-variant preservation requires schema 1 and purpose preserve-existing-full-alpha-pair")
    tile = record.get("tile")
    if not isinstance(tile, int) or isinstance(tile, bool) or tile <= 0:
        _fail("Existing-variant preservation requires a positive printed tile number")
    for field in ("approved_by", "note"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            _fail(f"Existing-variant preservation requires a nonblank {field}")
    variants = record.get("variants")
    if not isinstance(variants, list) or len(variants) != 2:
        _fail("Preserve exactly one full raster and one alpha sibling")
    checked = {}
    for variant in variants:
        if not isinstance(variant, dict) or variant.get("role") not in {"full", "alpha"} or variant["role"] in checked:
            _fail("Existing variants must have distinct full and alpha roles")
        raster = _absolute_file(variant, "path", record_path, "existing raster variant")
        digest = _required_sha256(variant, "sha256", record_path)
        if tile_number_from_name(raster.name) != tile:
            _fail("Existing variant source filename does not match the recorded tile")
        if sha256(raster) != digest:
            _fail("Existing raster variant changed after the preservation decision")
        checked[variant["role"]] = {"role": variant["role"], "path": str(raster), "sha256": digest}
    full, alpha = Path(checked["full"]["path"]), Path(checked["alpha"]["path"])
    if full.suffix.lower() not in {".tif", ".tiff"} or alpha != full.with_name(full.stem + " alpha" + full.suffix):
        _fail("The alpha variant must be the same-folder full raster's named alpha sibling")
    if sha256(record_path) != record_digest:
        _fail("Existing-variant preservation record changed during validation")
    return {"tile": tile, "record_path": str(record_path), "record_sha256": record_digest,
            "approved_by": record["approved_by"], "note": record["note"],
            "variants": [checked["full"], checked["alpha"]]}


def build_plan(manifests: Iterable[Path], *, preserve_existing_variants: Path | None = None) -> dict[str, Any]:
    """Create the complete, serializable dry-run plan."""
    tiles = load_manifests(manifests)
    preserved = [load_preserved_variant_pair(preserve_existing_variants)] if preserve_existing_variants else []
    if any(item["tile"] == pair["tile"] for item in tiles for pair in preserved):
        _fail("Cannot import a tile whose existing full/alpha pair is being preserved")
    if any(not item.get("vrt_path") or not item.get("vrt_sha256") for item in tiles):
        _fail("Every imported sheet needs its verified paper recipe file")
    return {
        "preserved_existing_variants": preserved,
        "schema_version": 1,
        "mode": "qgis-layer-preparation",
        "protected_project": str(PROTECTED_PROJECT),
        "expected_project_crs": EXPECTED_CRS,
        "expected_raster_crs": EXPECTED_CRS,
        "index_layer": INDEX_LAYER_NAME,
        "index_layer_source": str(INDEX_LAYER_SOURCE),
        "group": GROUP_NAME,
        "subfolders": [
            {"name": name, "first_tile": first, "last_tile": last}
            for name, first, last in COLLECTION.subfolders
        ],
        "style": dict(STYLE),
        "tiles": tiles,
        "actions": [
            "Rehash every raster, paper recipe file and georeference ledger before any QGIS mutation.",
            "Verify the exact protected project path and project CRS before mutation.",
            "Verify every raster is valid EPSG:3857 RGBA with band 4 as alpha.",
            "Create or safely reposition the exact Sanborn group immediately below the index.",
            "Reuse each sheet by canonical path (its raster or its paper recipe file) or add it exactly once.",
            "Prove QGIS can draw each paper recipe file (its two GDAL settings are present) before any mutation.",
            "Show every sheet through its paper recipe file; switch a reused layer from its raster in place.",
            "Apply the fixed rendering formula and alpha band.",
            "Place each sheet in its area subfolder, creating a missing one in its fixed position.",
            "Sort the sheets inside each subfolder by printed tile number.",
            "Expand the group and collapse every sheet layer's legend.",
            "Verify the protected project file was not written.",
        ],
        "save_project": False,
    }


_PYQGIS_BODY = r'''
import hashlib
import json
import math
import os
import re

from qgis.core import (
    Qgis,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsProject,
    QgsRasterLayer,
    QgsMultiBandColorRenderer,
    QgsContrastEnhancement,
    QgsDataProvider,
    QgsRectangle,
)

_SANBORN_HASH_CACHE = {}


def _sanborn_fail(message):
    raise RuntimeError("Sanborn QGIS preparation stopped: " + message)


def _sanborn_canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))


def _sanborn_sha256(path):
    canonical = _sanborn_canonical(path)
    stat = os.stat(canonical)
    key = (
        canonical,
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )
    cached = _SANBORN_HASH_CACHE.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with open(canonical, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    after = os.stat(canonical)
    after_key = (
        canonical,
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if after_key != key:
        _sanborn_fail("file changed while hashing: " + canonical)
    value = digest.hexdigest()
    _SANBORN_HASH_CACHE[key] = value
    return value


def _sanborn_verify_file_hashes(item):
    checks = [
        ("raster", item["path"], item["raster_sha256"]),
        ("paper recipe file", item["vrt_path"], item["vrt_sha256"]),
        ("georeference ledger", item["ledger_path"], item["ledger_sha256"]),
        ("source scan", item["source_path"], item["source_sha256"]),
        ("control file", item["points_path"], item["points_sha256"]),
        ("review record", item["review_path"], item["review_sha256"]),
        ("approval record", item["approval_path"], item["approval_sha256"]),
    ]
    checks.extend(
        ("review artifact " + artifact["name"], artifact["path"], artifact["sha256"])
        for artifact in item["review_artifacts"]
    )
    checks.extend(
        ("approval evidence " + evidence["name"], evidence["path"], evidence["sha256"])
        for evidence in item["additional_evidence"]
    )
    for label, path, expected in checks:
        canonical = _sanborn_canonical(path)
        if not os.path.isfile(canonical):
            _sanborn_fail("{} disappeared before QGIS preparation: {}".format(label, canonical))
        actual = _sanborn_sha256(canonical)
        if actual != expected:
            _sanborn_fail(
                "{} SHA-256 changed after manifest validation: {}".format(label, canonical)
            )


def _sanborn_layer_source(layer):
    return _sanborn_canonical(layer.source().split("|", 1)[0])


_SANBORN_RECIPE_SUFFIX = ".clean.vrt"
_SANBORN_RECIPE_SETTINGS_MESSAGE = (
    "QGIS cannot draw the whitened paper for {}. QGIS needs two settings under "
    "Settings > Options > System > Environment: GDAL_VRT_ENABLE_PYTHON set to "
    "TRUSTED_MODULES and GDAL_VRT_PYTHON_TRUSTED_MODULES set to sanborn_paper, and "
    "the paper module installed with: python3 tools/sanborn_paper.py install-module. "
    "Restart QGIS after setting them. Nothing was changed."
)


def _sanborn_source_identity(source):
    """The sheet a source shows: a paper recipe file counts as the TIFF beside it."""
    if source.endswith(_SANBORN_RECIPE_SUFFIX):
        return source[: -len(_SANBORN_RECIPE_SUFFIX)] + ".tif"
    return source


def _sanborn_layer_identity(layer):
    return _sanborn_source_identity(_sanborn_layer_source(layer))


def _sanborn_require_recipe_pixels(layer, recipe):
    """Read a small block through the recipe; it fails when the GDAL settings are missing."""
    extent = layer.extent()
    center = extent.center()
    half_width = extent.width() / 64.0
    half_height = extent.height() / 64.0
    sample = QgsRectangle(
        center.x() - half_width,
        center.y() - half_height,
        center.x() + half_width,
        center.y() + half_height,
    )
    block = layer.dataProvider().block(1, sample, 8, 8)
    if block is None or not block.isValid():
        _sanborn_fail(_SANBORN_RECIPE_SETTINGS_MESSAGE.format(recipe))


def _sanborn_switch_to_recipe(layer, recipe):
    """Point an existing layer at its paper recipe file, keeping its id, node, name and style."""
    layer.setDataSource(recipe, layer.name(), "gdal", QgsDataProvider.ProviderOptions())
    if not layer.isValid() or _sanborn_layer_source(layer) != _sanborn_canonical(recipe):
        _sanborn_fail("QGIS could not switch {} to its paper recipe file".format(layer.name()))


def _sanborn_tile_number(name):
    for pattern in (
        r"\bTile[\s_-]*(\d{1,4})(?!\d)",
        r"\bSanborn(?:\s+1[89]\d\d)?[\s_-]+(?!(?:1[89]\d\d)\b)(\d{1,4})(?!\d)",
    ):
        match = re.search(pattern, name, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _sanborn_exact_index(root):
    matches = [
        node
        for node in root.findLayers()
        if node.layer() is not None and node.layer().name() == PLAN["index_layer"]
    ]
    if len(matches) != 1:
        _sanborn_fail(
            "expected exactly one layer named {!r}; found {}".format(
                PLAN["index_layer"], len(matches)
            )
        )
    node = matches[0]
    if node.parent() is not root:
        _sanborn_fail("the exact index layer is not a root-level layer")
    layer = node.layer()
    if not isinstance(layer, QgsRasterLayer) or not layer.isValid():
        _sanborn_fail("the exact index layer is not a valid raster")
    if _sanborn_layer_source(layer) != _sanborn_canonical(PLAN["index_layer_source"]):
        _sanborn_fail("the exact index layer points to an unexpected raster source")
    if layer.crs().authid().upper() != PLAN["expected_raster_crs"]:
        _sanborn_fail("the exact index layer is not in required EPSG:3857")
    return node


def _sanborn_existing_group(root):
    matches = [
        group
        for group in root.findGroups(True)
        if group.name() == PLAN["group"]
    ]
    if len(matches) > 1:
        _sanborn_fail("more than one exact Sanborn group exists")
    return matches[0] if matches else None


def _sanborn_subfolders():
    return PLAN.get("subfolders") or []


def _sanborn_subfolder_name(tile):
    """The area subfolder for a printed tile number, or None for a flat group."""
    folders = _sanborn_subfolders()
    if not folders:
        return None
    for folder in folders:
        last = folder["last_tile"]
        if tile >= folder["first_tile"] and (last is None or tile <= last):
            return folder["name"]
    _sanborn_fail("tile {} falls outside every Sanborn subfolder".format(tile))


def _sanborn_containers(group):
    """The nodes that hold sheet layers: the area subfolders, or the group itself."""
    folders = _sanborn_subfolders()
    if not folders:
        return [(group, None)]
    order = [folder["name"] for folder in folders]
    found = []
    for node in group.children():
        if not isinstance(node, QgsLayerTreeGroup):
            _sanborn_fail(
                "layer {!r} sits directly in the Sanborn group instead of an area "
                "subfolder".format(node.name())
            )
        if node.name() not in order:
            _sanborn_fail("the Sanborn group contains an unexpected subfolder {!r}".format(node.name()))
        found.append(node)
    names = [node.name() for node in found]
    if len(set(names)) != len(names):
        _sanborn_fail("the Sanborn group contains the same area subfolder more than once")
    if names != sorted(names, key=order.index):
        _sanborn_fail("the Sanborn area subfolders are out of order: " + ", ".join(names))
    return [(node, node.name()) for node in found]


def _sanborn_sheet_container(group, tile):
    """The subfolder a sheet belongs in, created in its fixed position if missing."""
    name = _sanborn_subfolder_name(tile)
    if name is None:
        return group
    containers = _sanborn_containers(group)
    for container, container_name in containers:
        if container_name == name:
            return container
    order = [folder["name"] for folder in _sanborn_subfolders()]
    position = sum(1 for _, container_name in containers
                   if order.index(container_name) < order.index(name))
    created = group.insertGroup(position, name)
    if not isinstance(created, QgsLayerTreeGroup):
        _sanborn_fail("QGIS could not create the Sanborn subfolder " + name)
    return created


def _sanborn_is_within(node, group):
    parent = node.parent()
    while parent is not None:
        if parent is group:
            return True
        parent = parent.parent()
    return False


def _sanborn_source_filename_tile(layer):
    return _sanborn_tile_number(os.path.basename(_sanborn_layer_source(layer)))


def _sanborn_preserved_variant_pairs():
    pairs = {}
    for record in PLAN.get("preserved_existing_variants", []):
        tile = int(record["tile"])
        if tile in pairs:
            _sanborn_fail("duplicate existing-variant preservation records")
        evidence = [(record["record_path"], record["record_sha256"])]
        evidence.extend((variant["path"], variant["sha256"]) for variant in record["variants"])
        for path, digest in evidence:
            if not os.path.isfile(path) or _sanborn_sha256(path) != digest:
                _sanborn_fail("preserved existing-variant evidence changed: " + path)
        paths = {_sanborn_canonical(variant["path"]) for variant in record["variants"]}
        if len(paths) != 2 or len(record["variants"]) != 2:
            _sanborn_fail("existing-variant preservation must identify two distinct raster paths")
        pairs[tile] = paths
    return pairs


def _sanborn_check_group_contents(group, incoming):
    preserved = _sanborn_preserved_variant_pairs()
    if group is None:
        if preserved:
            _sanborn_fail("the declared full/alpha pair is not already present in the Sanborn group")
        return
    seen = {}
    nodes = [
        (node, folder_name)
        for container, folder_name in _sanborn_containers(group)
        for node in container.children()
    ]
    for node, folder_name in nodes:
        if not isinstance(node, QgsLayerTreeLayer):
            _sanborn_fail("the Sanborn group contains a subgroup or unknown node")
        layer = node.layer()
        if not isinstance(layer, QgsRasterLayer):
            _sanborn_fail("the Sanborn group contains a non-raster layer")
        displayed_tile = _sanborn_tile_number(node.name())
        source_tile = _sanborn_source_filename_tile(layer)
        if displayed_tile is None:
            _sanborn_fail(
                "cannot read a displayed tile number from group layer {!r}".format(
                    node.name()
                )
            )
        if source_tile is None:
            _sanborn_fail(
                "cannot read a tile number from the source filename for group layer {!r}".format(
                    node.name()
                )
            )
        if displayed_tile != source_tile:
            _sanborn_fail(
                "Sanborn group layer {!r} displays tile {} but its source filename "
                "is tile {}".format(
                    node.name(), displayed_tile, source_tile
                )
            )
        expected_folder = _sanborn_subfolder_name(displayed_tile)
        if expected_folder != folder_name:
            _sanborn_fail(
                "tile {} belongs in subfolder {!r} but sits in {!r}".format(
                    displayed_tile, expected_folder, folder_name
                )
            )
        seen.setdefault(displayed_tile, []).append(_sanborn_layer_identity(layer))
    for tile, paths in seen.items():
        if len(paths) > 1 and (
            tile in incoming or len(paths) != 2 or len(set(paths)) != 2
            or set(paths) != preserved.get(tile)
        ):
            _sanborn_fail("tile {} already occurs more than once in the group".format(tile))
    for tile, paths in preserved.items():
        if tile in incoming:
            _sanborn_fail("cannot import a tile whose existing full/alpha pair is being preserved")
        if len(seen.get(tile, [])) != 2 or set(seen[tile]) != paths:
            _sanborn_fail("the declared full/alpha pair is not already present in the Sanborn group")
    for tile, item in incoming.items():
        if tile in seen and seen[tile] != [_sanborn_canonical(item["path"])]:
            _sanborn_fail(
                "tile {} already points to a different raster; refusing replacement".format(tile)
            )


def _sanborn_preflight_project_tiles(project, root, incoming):
    """Reject a same-number raster pointing anywhere to a different source path."""
    expected = {
        int(tile): _sanborn_canonical(item["path"])
        for tile, item in incoming.items()
    }

    def check_raster(layer, origin, extra_name=None):
        source = _sanborn_layer_identity(layer)
        parsed = {
            _sanborn_tile_number(layer.name()),
            _sanborn_source_filename_tile(layer),
        }
        if extra_name is not None:
            parsed.add(_sanborn_tile_number(extra_name))
        for tile in parsed:
            if tile in expected and source != expected[tile]:
                _sanborn_fail(
                    "{} identifies tile {} but points to a different raster: {}".format(
                        origin, tile, source
                    )
                )

    for layer in project.mapLayers().values():
        if isinstance(layer, QgsRasterLayer):
            check_raster(layer, "registered project raster {!r}".format(layer.name()))

    for node in root.findLayers():
        node_tile = _sanborn_tile_number(node.name())
        layer = node.layer()
        if layer is None:
            if node_tile in expected:
                _sanborn_fail(
                    "unresolved layer-tree node {!r} identifies incoming tile {}".format(
                        node.name(), node_tile
                    )
                )
            continue
        if isinstance(layer, QgsRasterLayer):
            check_raster(
                layer,
                "project layer-tree node {!r}".format(node.name()),
                extra_name=node.name(),
            )

    # An explicit preservation record allows exactly the two existing variants,
    # not additional same-number layers or repeated nodes elsewhere in QGIS.
    for tile, paths in _sanborn_preserved_variant_pairs().items():
        registered = [layer for layer in project.mapLayers().values()
                      if isinstance(layer, QgsRasterLayer)
                      and tile in {_sanborn_tile_number(layer.name()), _sanborn_source_filename_tile(layer)}]
        nodes = [node for node in root.findLayers()
                 if _sanborn_tile_number(node.name()) == tile
                 or (isinstance(node.layer(), QgsRasterLayer)
                     and tile in {_sanborn_tile_number(node.layer().name()), _sanborn_source_filename_tile(node.layer())})]
        if (len(registered) != 2 or {_sanborn_layer_identity(layer) for layer in registered} != paths
                or len(nodes) != 2
                or any(not isinstance(node.layer(), QgsRasterLayer) for node in nodes)
                or {_sanborn_layer_identity(node.layer()) for node in nodes} != paths):
            _sanborn_fail("preserved tile {} must have exactly one full and one alpha layer in the project".format(tile))


def _sanborn_check_sheet_layer(layer, label):
    if not layer.isValid():
        _sanborn_fail("QGIS cannot open " + label)
    if layer.crs().authid().upper() != PLAN["expected_raster_crs"]:
        _sanborn_fail(
            "{} uses {}, expected {}".format(
                label, layer.crs().authid(), PLAN["expected_raster_crs"]
            )
        )
    if layer.bandCount() != 4:
        _sanborn_fail("sheet must contain exactly Red, Green, Blue, and Alpha bands: " + label)
    provider = layer.dataProvider()
    if provider.colorInterpretation(4) != Qgis.RasterColorInterpretation.AlphaBand:
        _sanborn_fail("band 4 is not identified as a true alpha band: " + label)
    renderer = layer.renderer()
    if renderer is None or not hasattr(renderer, "setAlphaBand"):
        _sanborn_fail("renderer cannot use the required alpha band: " + label)


def _sanborn_preflight_raster(project, item):
    path = _sanborn_canonical(item["path"])
    recipe = _sanborn_canonical(item["vrt_path"])
    if _sanborn_source_identity(recipe) != path:
        _sanborn_fail("paper recipe file does not sit beside its raster: " + recipe)
    # A sheet already in the project may point at its raster (before the
    # paper-whitening switch) or at its recipe; either is the same sheet.
    existing = [
        layer
        for layer in project.mapLayers().values()
        if isinstance(layer, QgsRasterLayer) and _sanborn_layer_identity(layer) == path
    ]
    if len(existing) > 1:
        _sanborn_fail("sheet is already registered more than once (raster or paper recipe): " + path)
    probe = QgsRasterLayer(recipe, item["layer_name"] + " verification probe", "gdal")
    if not probe.isValid():
        _sanborn_fail("QGIS cannot freshly open the paper recipe file: " + recipe)
    _sanborn_check_sheet_layer(probe, "paper recipe file " + recipe)
    _sanborn_require_recipe_pixels(probe, recipe)
    layer = existing[0] if existing else probe
    if existing:
        # A corrected tile deliberately reuses its fixed disk filename.  Force
        # QGIS to discard the former provider cache, then compare it with a
        # separately opened provider before reusing the registered layer.
        layer.reload()
        _sanborn_check_sheet_layer(layer, "registered sheet " + _sanborn_layer_source(layer))
        live_extent = layer.extent()
        probe_extent = probe.extent()
        live_provider = layer.dataProvider()
        probe_provider = probe.dataProvider()
        live_signature = (
            live_provider.xSize(),
            live_provider.ySize(),
            live_extent.xMinimum(),
            live_extent.yMinimum(),
            live_extent.xMaximum(),
            live_extent.yMaximum(),
        )
        probe_signature = (
            probe_provider.xSize(),
            probe_provider.ySize(),
            probe_extent.xMinimum(),
            probe_extent.yMinimum(),
            probe_extent.xMaximum(),
            probe_extent.yMaximum(),
        )
        if any(
            not math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-8)
            for actual, expected in zip(live_signature, probe_signature)
        ):
            _sanborn_fail("registered raster did not refresh to the current corrected file: " + path)
    return {
        "layer": layer,
        "already_registered": bool(existing),
        "provider_reloaded": bool(existing),
        "switch_to_recipe": bool(existing) and _sanborn_layer_source(layer) != recipe,
    }


def _sanborn_position_group(root, index_node, group, rollback):
    index_position = root.children().index(index_node)
    if group is None:
        created = root.insertGroup(index_position + 1, PLAN["group"])
        rollback["active_group"] = created
        rollback["group_action"] = "created"
        return created, "created"
    if group.parent() is root and root.children().index(group) == index_position + 1:
        rollback["active_group"] = group
        rollback["group_action"] = "reused"
        return group, "reused"

    # Clone first so every registered raster keeps a live tree node while the
    # former group node is removed.  This avoids the registry-bridge failure
    # caused by clearing a group before its replacement nodes exist.
    former_parent = group.parent()
    if former_parent is None:
        _sanborn_fail("existing Sanborn group has no parent")
    replacement = group.clone()
    if not isinstance(replacement, QgsLayerTreeGroup):
        _sanborn_fail("QGIS did not clone the Sanborn group as a group node")
    root.insertChildNode(index_position + 1, replacement)
    rollback["active_group"] = replacement
    rollback["group_action"] = "repositioned"
    former_parent.removeChildNode(group)
    return replacement, "repositioned"


def _sanborn_ensure_single_group_node(root, container, layer):
    # The container is the sheet's area subfolder (or the group when flat).
    nodes = [node for node in root.findLayers() if node.layerId() == layer.id()]
    inside = [node for node in nodes if node.parent() is container]
    keeper = inside[0] if inside else container.insertLayer(len(container.children()), layer)
    # The keeper exists before any duplicate or misplaced node is removed.
    for node in list(root.findLayers()):
        if node.layerId() == layer.id() and node is not keeper:
            parent = node.parent()
            if parent is None:
                _sanborn_fail("duplicate layer node has no parent")
            parent.removeChildNode(node)
    return keeper


def _sanborn_snapshot_rollback(root, existing_group, prepared):
    group_snapshot = None
    if existing_group is not None:
        parent = existing_group.parent()
        if parent is None:
            _sanborn_fail("existing Sanborn group has no parent before mutation")
        clone = existing_group.clone()
        if not isinstance(clone, QgsLayerTreeGroup):
            _sanborn_fail("QGIS could not snapshot the existing Sanborn group")
        group_snapshot = {
            "parent": parent,
            "index": parent.children().index(existing_group),
            "clone": clone,
        }

    reused = {}
    for tile, entry in prepared.items():
        if not entry["already_registered"]:
            continue
        layer = entry["layer"]
        brightness = layer.brightnessFilter()
        renderer = layer.renderer()
        external_nodes = []
        for node in root.findLayers():
            if node.layerId() != layer.id() or _sanborn_is_within(node, existing_group):
                continue
            parent = node.parent()
            if parent is None:
                _sanborn_fail("existing tile {} has a layer-tree node with no parent".format(tile))
            external_nodes.append(
                {
                    "parent": parent,
                    "index": parent.children().index(node),
                    "clone": node.clone(),
                }
            )
        reused[layer.id()] = {
            "layer": layer,
            "name": layer.name(),
            "source": layer.source(),
            "provider": layer.providerType(),
            "brightness": brightness.brightness(),
            "gamma": brightness.gamma(),
            "contrast": brightness.contrast(),
            "opacity": renderer.opacity(),
            "alpha_band": renderer.alphaBand(),
            "renderer_clone": renderer.clone(),
            "external_nodes": external_nodes,
        }
    return {
        "existing_group": group_snapshot,
        "active_group": None,
        "group_action": None,
        "added_layer_ids": [],
        "reused": reused,
    }


def _sanborn_adjusted_restore_index(parent, original_index, active_group):
    adjusted = original_index
    if active_group is not None and active_group.parent() is parent:
        active_index = parent.children().index(active_group)
        if active_index <= original_index:
            adjusted += 1
    return min(adjusted, len(parent.children()))


def _sanborn_restore_after_failure(project, root, rollback):
    """Best-effort rollback; never saves the project or masks the original error."""
    errors = []
    active_group = rollback.get("active_group")

    for layer_id in reversed(rollback.get("added_layer_ids", [])):
        try:
            if layer_id in project.mapLayers():
                project.removeMapLayer(layer_id)
        except Exception as exc:
            errors.append("could not remove newly registered layer {}: {}".format(layer_id, exc))

    restored_group = None
    group_snapshot = rollback.get("existing_group")
    if group_snapshot is not None and active_group is not None:
        try:
            parent = group_snapshot["parent"]
            insert_at = _sanborn_adjusted_restore_index(
                parent, group_snapshot["index"], active_group
            )
            restored_group = group_snapshot["clone"]
            parent.insertChildNode(insert_at, restored_group)
        except Exception as exc:
            errors.append("could not restore the original Sanborn group: {}".format(exc))
            restored_group = None

    # Restore nodes which originally lived outside the Sanborn group before
    # removing the active group.  Keeping a live node avoids registry-bridge
    # deletion of reused layers while the tree is being repaired.
    for layer_id, snapshot in rollback.get("reused", {}).items():
        records_by_parent = {}
        for record in snapshot["external_nodes"]:
            records_by_parent.setdefault(record["parent"], []).append(record)
        for parent, records in records_by_parent.items():
            records.sort(key=lambda record: record["index"])
            try:
                current_count = sum(
                    1
                    for child in parent.children()
                    if isinstance(child, QgsLayerTreeLayer) and child.layerId() == layer_id
                )
                for record in records[current_count:]:
                    insert_at = _sanborn_adjusted_restore_index(
                        parent, record["index"], active_group
                    )
                    parent.insertChildNode(insert_at, record["clone"])
            except Exception as exc:
                errors.append(
                    "could not restore prior tree nodes for layer {}: {}".format(layer_id, exc)
                )

    try:
        if active_group is not None and active_group.parent() is not None:
            active_group.parent().removeChildNode(active_group)
    except Exception as exc:
        errors.append("could not remove the newly added or changed Sanborn group: {}".format(exc))

    for layer_id, snapshot in rollback.get("reused", {}).items():
        try:
            layer = snapshot["layer"]
            if layer.source() != snapshot["source"]:
                # Undo a switch to the paper recipe before restoring the style.
                layer.setDataSource(
                    snapshot["source"], snapshot["name"], snapshot["provider"],
                    QgsDataProvider.ProviderOptions(),
                )
            layer.setName(snapshot["name"])
            brightness = layer.brightnessFilter()
            brightness.setBrightness(snapshot["brightness"])
            brightness.setGamma(snapshot["gamma"])
            brightness.setContrast(snapshot["contrast"])
            layer.setRenderer(snapshot["renderer_clone"].clone())
            renderer = layer.renderer()
            renderer.setOpacity(snapshot["opacity"])
            renderer.setAlphaBand(snapshot["alpha_band"])
            layer.triggerRepaint()
        except Exception as exc:
            errors.append("could not restore style for reused layer {}: {}".format(layer_id, exc))
    return errors


def _sanborn_apply_style(layer):
    # A fresh RGB renderer avoids QGIS's automatic per-channel stretch. Keep
    # source pixel appearance intact; this is display setup, not image editing.
    renderer = QgsMultiBandColorRenderer(layer.dataProvider(), 1, 2, 3)
    renderer.setRedContrastEnhancement(None)
    renderer.setGreenContrastEnhancement(None)
    renderer.setBlueContrastEnhancement(None)
    layer.setRenderer(renderer)
    brightness = layer.brightnessFilter()
    brightness.setBrightness(int(PLAN["style"]["brightness"]))
    brightness.setGamma(float(PLAN["style"]["gamma"]))
    brightness.setContrast(int(PLAN["style"]["contrast"]))
    renderer = layer.renderer()
    renderer.setOpacity(float(PLAN["style"]["opacity"]))
    renderer.setAlphaBand(int(PLAN["style"]["alpha_band"]))
    layer.triggerRepaint()


def _sanborn_numeric_order(project, group):
    # Recheck the exact preserved pair, including file hashes, before ordering.
    # Each area subfolder is sorted on its own; the result lists every tile in
    # layer-panel order.
    _sanborn_check_group_contents(group, {})
    order = []
    for container, _ in _sanborn_containers(group):
        order.extend(_sanborn_order_nodes(project, container))
    return order


def _sanborn_folder_order(group):
    return [
        (folder_name or group.name(), [_sanborn_tile_number(node.name()) for node in container.children()])
        for container, folder_name in _sanborn_containers(group)
    ]


def _sanborn_sheet_nodes(group):
    return [node for container, _ in _sanborn_containers(group) for node in container.children()]


def _sanborn_order_nodes(project, group):
    # Sorting remains stable for the two intentional variants of one tile.
    numbered = []
    for node in group.children():
        if not isinstance(node, QgsLayerTreeLayer) or not isinstance(node.layer(), QgsRasterLayer):
            _sanborn_fail("only raster layer nodes may be placed in the Sanborn group")
        # Group contents were already required to make the displayed number
        # agree with the source filename, so order by that verified display
        # identity rather than a potentially stale registry-layer name.
        tile = _sanborn_tile_number(node.name())
        if tile is None:
            _sanborn_fail("cannot order group layer {!r} numerically".format(node.layer().name()))
        numbered.append((tile, node))
    numbered.sort(key=lambda pair: pair[0])
    # Move the existing nodes without deleting them or touching layer registry
    # ownership. The bulk layer-list reorder crashes in QGIS 3.42.1.
    registry_ids = set(project.mapLayers())
    bridge = project.layerTreeRegistryBridge()
    bridge_enabled = bridge.isEnabled()
    bridge.setEnabled(False)
    try:
        for position, (_, node) in enumerate(numbered):
            if group.children()[position] is node:
                continue
            original_position = group.children().index(node)
            if not group.takeChild(node):
                _sanborn_fail("could not detach Sanborn node for numeric ordering")
            try:
                group.insertChildNode(position, node)
            except Exception:
                # Retain a live node for the caller's existing full rollback.
                if node.parent() is None:
                    group.insertChildNode(original_position, node)
                raise
        if list(group.children()) != [node for _, node in numbered]:
            _sanborn_fail("Sanborn numeric node ordering verification failed")
    finally:
        bridge.setEnabled(bridge_enabled)
        if set(project.mapLayers()) != registry_ids:
            _sanborn_fail("layer registry changed during Sanborn numeric ordering")
    return [tile for tile, _ in numbered]


def _sanborn_verify_style(layer):
    brightness = layer.brightnessFilter()
    renderer = layer.renderer()
    expected = PLAN["style"]
    if not isinstance(renderer, QgsMultiBandColorRenderer) or (renderer.redBand(), renderer.greenBand(), renderer.blueBand()) != (1, 2, 3):
        _sanborn_fail("neutral RGB bands verification failed for " + layer.name())
    for enhancement in (renderer.redContrastEnhancement(), renderer.greenContrastEnhancement(), renderer.blueContrastEnhancement()):
        if enhancement is not None and enhancement.contrastEnhancementAlgorithm() != QgsContrastEnhancement.NoEnhancement:
            _sanborn_fail("channel stretch must remain disabled for " + layer.name())
    if brightness.brightness() != int(expected["brightness"]):
        _sanborn_fail("brightness verification failed for " + layer.name())
    if not math.isclose(brightness.gamma(), float(expected["gamma"]), abs_tol=1e-9):
        _sanborn_fail("gamma verification failed for " + layer.name())
    if brightness.contrast() != int(expected["contrast"]):
        _sanborn_fail("contrast verification failed for " + layer.name())
    if not math.isclose(renderer.opacity(), float(expected["opacity"]), abs_tol=1e-12):
        _sanborn_fail("opacity verification failed for " + layer.name())
    if renderer.alphaBand() != int(expected["alpha_band"]):
        _sanborn_fail("alpha-band verification failed for " + layer.name())


def prepare_sanborn_layers():
    if PLAN.get("save_project") is not False:
        _sanborn_fail("plan must explicitly prohibit project saving")
    project = QgsProject.instance()
    current_project = _sanborn_canonical(project.fileName())
    protected_project = _sanborn_canonical(PLAN["protected_project"])
    if current_project != protected_project:
        _sanborn_fail(
            "open project is {!r}, expected protected project {!r}".format(
                project.fileName(), PLAN["protected_project"]
            )
        )
    if not os.path.isfile(protected_project):
        _sanborn_fail("protected project file does not exist")
    if project.crs().authid().upper() != PLAN["expected_project_crs"]:
        _sanborn_fail(
            "project CRS is {}, expected {}".format(
                project.crs().authid(), PLAN["expected_project_crs"]
            )
        )
    # The dated archive is a copy of the project file on disk. If the open
    # project holds edits that have not been saved, that copy is not the thing
    # about to be changed, and the way back it promises does not exist. Stop
    # before touching anything and say so plainly.
    if project.isDirty():
        _sanborn_fail(
            "Your QGIS project has changes you have not saved yet. The dated "
            "backup copies the project file on disk, so it would not include "
            "them, and it could not put things back the way they are now. "
            "Save the project in QGIS, then run this again."
        )

    project_mtime = os.stat(protected_project).st_mtime_ns
    root = project.layerTreeRoot()
    index_node = _sanborn_exact_index(root)
    existing_group = _sanborn_existing_group(root)
    incoming = {int(item["tile"]): item for item in PLAN["tiles"]}
    for item in PLAN["tiles"]:
        _sanborn_verify_file_hashes(item)
    _sanborn_check_group_contents(existing_group, incoming)
    _sanborn_preflight_project_tiles(project, root, incoming)

    # Hashes, every same-number project raster, and renderer capabilities are
    # checked before the first registry or layer-tree mutation.
    prepared = {
        int(item["tile"]): _sanborn_preflight_raster(project, item)
        for item in PLAN["tiles"]
    }
    rollback = _sanborn_snapshot_rollback(root, existing_group, prepared)
    try:
        group, group_action = _sanborn_position_group(
            root, index_node, existing_group, rollback
        )
        imported = []
        reused = []
        switched = []
        for item in PLAN["tiles"]:
            tile = int(item["tile"])
            entry = prepared[tile]
            layer = entry["layer"]
            if entry["already_registered"]:
                reused.append(tile)
                if entry["switch_to_recipe"]:
                    _sanborn_switch_to_recipe(layer, item["vrt_path"])
                    switched.append(tile)
            else:
                if project.addMapLayer(layer, False) is None:
                    _sanborn_fail("QGIS refused to register tile {}".format(tile))
                rollback["added_layer_ids"].append(layer.id())
                imported.append(tile)
            layer.setName(item["layer_name"])
            container = _sanborn_sheet_container(group, tile)
            node = _sanborn_ensure_single_group_node(root, container, layer)
            _sanborn_apply_style(layer)
            node.setExpanded(False)

        order = _sanborn_numeric_order(project, group)
        group.setExpanded(True)
        for node in _sanborn_sheet_nodes(group):
            node.setExpanded(False)

        # Postconditions: exact group position, one registry entry and one group
        # node per incoming path, exact source/display tile identity, exact style,
        # numeric order, and no disk write.
        _sanborn_check_group_contents(group, incoming)
        _sanborn_preflight_project_tiles(project, root, incoming)
        root_children = root.children()
        if root_children.index(group) != root_children.index(index_node) + 1:
            _sanborn_fail("Sanborn group is not immediately beneath the exact index layer")
        folder_order = _sanborn_folder_order(group)
        for folder_name, tiles in folder_order:
            if tiles != sorted(tiles):
                _sanborn_fail("{} is not in numeric tile order".format(folder_name))
        if any(node.isExpanded() for node in _sanborn_sheet_nodes(group)):
            _sanborn_fail("a Sanborn sheet legend remained expanded")
        for item in PLAN["tiles"]:
            path = _sanborn_canonical(item["path"])
            matches = [
                layer
                for layer in project.mapLayers().values()
                if isinstance(layer, QgsRasterLayer) and _sanborn_layer_identity(layer) == path
            ]
            if len(matches) != 1:
                _sanborn_fail("tile path is not registered exactly once: " + path)
            layer = matches[0]
            if _sanborn_layer_source(layer) != _sanborn_canonical(item["vrt_path"]):
                _sanborn_fail("tile does not draw through its paper recipe file: " + path)
            nodes = [node for node in root.findLayers() if node.layerId() == layer.id()]
            expected_folder = _sanborn_subfolder_name(int(item["tile"]))
            parent = nodes[0].parent() if len(nodes) == 1 else None
            if (
                parent is None
                or (expected_folder is None and parent is not group)
                or (expected_folder is not None and (
                    parent.parent() is not group or parent.name() != expected_folder
                ))
            ):
                _sanborn_fail(
                    "tile does not have exactly one tree node in its Sanborn subfolder"
                )
            if nodes[0].isExpanded():
                _sanborn_fail("tile layer node remained expanded: " + layer.name())
            _sanborn_verify_style(layer)
        if not group.isExpanded():
            _sanborn_fail("Sanborn group did not remain expanded")
        for item in PLAN["tiles"]:
            _sanborn_verify_file_hashes(item)
        if os.stat(protected_project).st_mtime_ns != project_mtime:
            _sanborn_fail("protected project file changed during layer preparation")

        result = {
            "status": "success",
            "project": project.fileName(),
            "project_crs": project.crs().authid(),
            "project_dirty_unsaved": project.isDirty(),
            "project_file_unchanged": True,
            "group": group.name(),
            "group_action": group_action,
            "group_expanded": group.isExpanded(),
            "tile_order": order,
            "folder_order": dict(folder_order),
            "imported_tiles": imported,
            "reused_tiles": reused,
            "switched_to_recipe_tiles": switched,
            "child_nodes_collapsed": True,
            "save_project": False,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return result
    except Exception as exc:
        rollback_errors = _sanborn_restore_after_failure(project, root, rollback)
        if rollback_errors:
            raise RuntimeError(
                "{}; rollback was incomplete: {}".format(exc, "; ".join(rollback_errors))
            ) from exc
        raise


SANBORN_QGIS_RESULT = prepare_sanborn_layers()
'''


def generate_pyqgis_code(plan: dict[str, Any]) -> str:
    """Return self-contained code suitable for QGIS MCP ``execute_code``."""
    serialized = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    return (
        "# Generated by tools/sanborn_qgis.py; deliberately never saves the project.\n"
        "import json\n"
        f"PLAN = json.loads({serialized!r})\n"
        + _PYQGIS_BODY
    )


def execute_code_payload(plan: dict[str, Any]) -> dict[str, str]:
    """Return the exact argument object expected by QGIS MCP execute_code."""
    return {"code": generate_pyqgis_code(plan)}


# --- One-time switch of already-loaded sheets to their paper recipe files ---

DEFAULT_SHEET_FOLDER = Path(__file__).resolve().parent.parent / "1911 SANBORN DOWNLOADS"


def build_recipe_migration_plan(folders: Iterable[Path]) -> dict[str, Any]:
    """List every sheet TIFF whose paper recipe file verifies, for the one-time switch.

    The TIFF's size and modification time are recorded so the live code can
    refuse a sheet rebuilt after this plan was made; the recipe is bound by hash.
    """
    recipes: list[dict[str, Any]] = []
    unverified: list[dict[str, str]] = []
    for folder in folders:
        for tif in sorted(Path(folder).expanduser().resolve().glob("*_georeferenced.tif")):
            vrt = sanborn_paper.vrt_path_for(tif)
            if not vrt.is_file():
                continue
            before = tif.stat()
            try:
                sanborn_paper.verify_vrt(vrt, tif)
                digest = sha256(vrt)
            except (sanborn_paper.RecipeError, OSError, ManifestError) as exc:
                unverified.append({"path": str(tif), "reason": str(exc)})
                continue
            after = tif.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                unverified.append({"path": str(tif), "reason": "the sheet changed while its recipe was checked"})
                continue
            recipes.append({
                "path": str(tif),
                "vrt_path": str(vrt),
                "vrt_sha256": digest,
                "raster_bytes": after.st_size,
                "raster_mtime_ns": after.st_mtime_ns,
            })
    return {
        "schema_version": 1,
        "mode": "qgis-paper-recipe-migration",
        "protected_project": str(PROTECTED_PROJECT),
        "group": GROUP_NAME,
        "recipes": recipes,
        "unverified": unverified,
        "save_project": False,
    }


_RECIPE_MIGRATION_BODY = r'''
import hashlib
import json
import math
import os

from qgis.core import QgsDataProvider, QgsProject, QgsRasterLayer, QgsRectangle

_RECIPE_SUFFIX = ".clean.vrt"


def _recipe_fail(message):
    raise RuntimeError("Sanborn paper recipe switch stopped: " + message)


def _recipe_canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))


def _recipe_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _recipe_source(layer):
    return _recipe_canonical(layer.source().split("|", 1)[0])


def _recipe_style(layer):
    renderer = layer.renderer()
    brightness = layer.brightnessFilter()
    hue = layer.hueSaturationFilter()
    return (
        layer.name(),
        type(renderer).__name__ if renderer is not None else None,
        renderer.opacity() if renderer is not None else None,
        renderer.alphaBand() if renderer is not None and hasattr(renderer, "alphaBand") else None,
        brightness.brightness(),
        round(brightness.gamma(), 9),
        brightness.contrast(),
        hue.invertColors() if hue is not None else None,
    )


def _recipe_require_pixels(layer, recipe):
    extent = layer.extent()
    center = extent.center()
    half_width = extent.width() / 64.0
    half_height = extent.height() / 64.0
    sample = QgsRectangle(center.x() - half_width, center.y() - half_height,
                          center.x() + half_width, center.y() + half_height)
    block = layer.dataProvider().block(1, sample, 8, 8)
    if block is None or not block.isValid():
        _recipe_fail(
            "QGIS cannot draw the whitened paper for {}. QGIS needs two settings under "
            "Settings > Options > System > Environment: GDAL_VRT_ENABLE_PYTHON set to "
            "TRUSTED_MODULES and GDAL_VRT_PYTHON_TRUSTED_MODULES set to sanborn_paper, and "
            "the paper module installed with: python3 tools/sanborn_paper.py install-module. "
            "Restart QGIS after setting them. Nothing was changed.".format(recipe)
        )


def _recipe_rollback(snapshots):
    errors = []
    for snapshot in reversed(snapshots):
        layer = snapshot["layer"]
        try:
            if layer.source() != snapshot["source"]:
                layer.setDataSource(snapshot["source"], snapshot["name"], snapshot["provider"],
                                    QgsDataProvider.ProviderOptions())
            layer.setName(snapshot["name"])
            layer.setRenderer(snapshot["renderer"].clone())
            brightness = layer.brightnessFilter()
            brightness.setBrightness(snapshot["brightness"])
            brightness.setGamma(snapshot["gamma"])
            brightness.setContrast(snapshot["contrast"])
            layer.triggerRepaint()
        except Exception as exc:
            errors.append("could not restore {}: {}".format(snapshot["name"], exc))
    return errors


def switch_sanborn_sheets_to_recipes():
    if PLAN.get("save_project") is not False:
        _recipe_fail("plan must explicitly prohibit project saving")
    project = QgsProject.instance()
    if _recipe_canonical(project.fileName()) != _recipe_canonical(PLAN["protected_project"]):
        _recipe_fail("open project is {!r}, expected {!r}".format(project.fileName(), PLAN["protected_project"]))
    root = project.layerTreeRoot()
    groups = [group for group in root.findGroups(True) if group.name() == PLAN["group"]]
    if len(groups) != 1:
        _recipe_fail("expected exactly one group named {!r}; found {}".format(PLAN["group"], len(groups)))
    group = groups[0]
    recipes = {_recipe_canonical(record["path"]): record for record in PLAN["recipes"]}
    unverified = {_recipe_canonical(record["path"]): record["reason"] for record in PLAN["unverified"]}

    candidates = []
    skipped = []
    seen = set()
    for node in group.findLayers():
        layer = node.layer()
        if layer is None:
            skipped.append({"layer": node.name(), "reason": "the layer is missing from the project"})
            continue
        if layer.id() in seen:
            continue
        seen.add(layer.id())
        if not isinstance(layer, QgsRasterLayer):
            skipped.append({"layer": node.name(), "reason": "not a raster layer"})
            continue
        source = _recipe_source(layer)
        if source.endswith(_RECIPE_SUFFIX):
            skipped.append({"layer": node.name(), "reason": "already draws through its paper recipe file"})
            continue
        if layer.providerType() != "gdal" or not source.endswith("_georeferenced.tif"):
            skipped.append({"layer": node.name(), "reason": "its source is not a *_georeferenced.tif sheet"})
            continue
        if source in unverified:
            skipped.append({"layer": node.name(), "reason": "its paper recipe file does not verify: " + unverified[source]})
            continue
        record = recipes.get(source)
        if record is None:
            skipped.append({"layer": node.name(), "reason": "no paper recipe file beside its sheet (run backfill, then regenerate this code)"})
            continue
        stat = os.stat(source)
        if (stat.st_size, stat.st_mtime_ns) != (record["raster_bytes"], record["raster_mtime_ns"]):
            skipped.append({"layer": node.name(), "reason": "the sheet changed after this code was generated; regenerate it"})
            continue
        recipe = _recipe_canonical(record["vrt_path"])
        if not os.path.isfile(recipe) or _recipe_sha256(recipe) != record["vrt_sha256"]:
            skipped.append({"layer": node.name(), "reason": "the paper recipe file changed after this code was generated; regenerate it"})
            continue
        candidates.append((node, layer, recipe))

    # Every candidate opens, matches its layer and draws before anything changes.
    for node, layer, recipe in candidates:
        probe = QgsRasterLayer(recipe, node.name() + " recipe probe", "gdal")
        if not probe.isValid():
            _recipe_fail("QGIS cannot open the paper recipe file: " + recipe)
        if probe.bandCount() != 4 or probe.crs().authid() != layer.crs().authid():
            _recipe_fail("the paper recipe file does not match its layer's bands or CRS: " + recipe)
        live, fresh = layer.extent(), probe.extent()
        if any(not math.isclose(a, b, rel_tol=0.0, abs_tol=1e-6) for a, b in (
                (live.xMinimum(), fresh.xMinimum()), (live.yMinimum(), fresh.yMinimum()),
                (live.xMaximum(), fresh.xMaximum()), (live.yMaximum(), fresh.yMaximum()))):
            _recipe_fail("the paper recipe file covers a different area than its layer: " + recipe)
        _recipe_require_pixels(probe, recipe)

    snapshots = []
    switched = []
    try:
        for node, layer, recipe in candidates:
            parent = node.parent()
            position = parent.children().index(node)
            style = _recipe_style(layer)
            brightness = layer.brightnessFilter()
            snapshots.append({
                "layer": layer,
                "source": layer.source(),
                "provider": layer.providerType(),
                "name": layer.name(),
                "renderer": layer.renderer().clone(),
                "brightness": brightness.brightness(),
                "gamma": brightness.gamma(),
                "contrast": brightness.contrast(),
            })
            layer.setDataSource(recipe, layer.name(), "gdal", QgsDataProvider.ProviderOptions())
            if not layer.isValid() or _recipe_source(layer) != recipe:
                _recipe_fail("QGIS could not switch {} to its paper recipe file".format(node.name()))
            if _recipe_style(layer) != style:
                _recipe_fail("switching {} changed its name or style".format(node.name()))
            if node.parent() is not parent or parent.children().index(node) != position:
                _recipe_fail("switching {} moved it in the layer panel".format(node.name()))
            layer.triggerRepaint()
            switched.append(node.name())
    except Exception as exc:
        errors = _recipe_rollback(snapshots)
        failed = {"switched": 0, "rolled_back": len(snapshots), "failed": 1, "error": str(exc),
                  "rollback_errors": errors, "save_project": False}
        print(json.dumps(failed, indent=2, sort_keys=True))
        raise

    result = {
        "status": "success",
        "switched": len(switched),
        "switched_layers": switched,
        "skipped": len(skipped),
        "skipped_layers": skipped,
        "failed": 0,
        "project_dirty_unsaved": project.isDirty(),
        "save_project": False,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


SANBORN_RECIPE_SWITCH_RESULT = switch_sanborn_sheets_to_recipes()
'''


def generate_recipe_migration_code(plan: dict[str, Any]) -> str:
    """Self-contained PyQGIS code for the one-time switch; it never saves the project."""
    serialized = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    return (
        "# Generated by tools/sanborn_qgis.py migrate-to-recipes; deliberately never saves the project.\n"
        "import json\n"
        f"PLAN = json.loads({serialized!r})\n"
        + _RECIPE_MIGRATION_BODY
    )


def migration_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="sanborn_qgis.py migrate-to-recipes",
        description=(
            "Print PyQGIS code that switches every loaded sheet in the Sanborn group from its "
            "*_georeferenced.tif to its verified paper recipe file, keeping name, place and style. "
            "It never saves the project and undoes every switch if one fails."
        ),
    )
    parser.add_argument("--folder", type=Path, action="append",
                        help=f"folder of sheet TIFFs and recipe files (default: {DEFAULT_SHEET_FOLDER})")
    parser.add_argument("--emit-payload", action="store_true",
                        help="print the JSON argument object for QGIS MCP execute_code")
    parser.add_argument("--skip-archive", action="store_true",
                        help="do not copy the QGIS project into the dated archive first (tests only)")
    args = parser.parse_args(argv)
    plan = build_recipe_migration_plan(args.folder or [DEFAULT_SHEET_FOLDER])
    print(
        f"{len(plan['recipes'])} sheets have a verified paper recipe file; "
        f"{len(plan['unverified'])} recipe files do not verify.",
        file=sys.stderr,
    )
    if not args.skip_archive:
        try:
            target, replaced = sanborn_archive.archive_project(PROTECTED_PROJECT)
        except sanborn_archive.ArchiveError as exc:
            print(f"Stopped before changing anything: {exc}", file=sys.stderr)
            return 3
        verb = "Replaced today's" if replaced else "Saved a"
        print(f"{verb} copy of your QGIS project at {target}", file=sys.stderr)
    code = generate_recipe_migration_code(plan)
    if args.emit_payload:
        print(json.dumps({"code": code}, indent=2))
    else:
        print(code, end="")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate finished-tile QGIS manifests and emit a safe layer-preparation plan "
            "or PyQGIS execution code. The default is a dry run."
        )
    )
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument(
        "--preserve-existing-variants", type=Path,
        help="Local hash-bound decision preserving an already-loaded full/alpha pair; never imports that tile",
    )
    parser.add_argument(
        "--collection",
        default=sanborn_collections.DEFAULT_COLLECTION,
        choices=sorted(sanborn_collections.COLLECTIONS),
        help=(
            "Which body of sheets is being placed. Defaults to the 1911 Atlanta "
            "sheets; other years and cities are added in sanborn_collections.py."
        ),
    )
    parser.add_argument(
        "--skip-archive",
        action="store_true",
        help=(
            "Do not copy the QGIS project into the dated archive first. Only for "
            "dry runs and tests; a real import should always leave a way back."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_const", const="plan", dest="mode")
    mode.add_argument("--emit-code", action="store_const", const="code", dest="mode")
    mode.add_argument(
        "--emit-payload",
        action="store_const",
        const="payload",
        dest="mode",
        help="Print the JSON argument object for QGIS MCP execute_code",
    )
    parser.set_defaults(mode="plan")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["migrate-to-recipes"]:
        return migration_main(argv[1:])
    try:
        args = parse_args(argv)
        collection = sanborn_collections.get(args.collection)
        plan = build_plan(args.manifests, preserve_existing_variants=args.preserve_existing_variants)

        # Take the dated copy before emitting anything that would change the
        # project. A dry run is only a printout, so it does not need one.
        if args.mode != "plan" and not args.skip_archive:
            target, replaced = sanborn_archive.archive_project(collection.project)
            verb = "Replaced today's" if replaced else "Saved a"
            print(
                f"{verb} copy of your QGIS project at {target}",
                file=sys.stderr,
            )

        if args.mode == "code":
            print(generate_pyqgis_code(plan), end="")
        elif args.mode == "payload":
            print(json.dumps(execute_code_payload(plan), indent=2))
        else:
            print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    except ManifestError as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 2
    except sanborn_archive.ArchiveError as exc:
        print(f"Stopped before changing anything: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
