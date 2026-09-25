#!/usr/bin/env python3
"""Local-first queue for downloading, reviewing, and finishing 1911 Sanborn tiles.

The Mac performs every deterministic operation. ChatGPT or Joel only chooses
controls and approves the compact review packet.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image

from sanborn_review import require_approval, validate_control_labels
from sanborn_qgis import load_manifest as validate_qgis_manifest
from sanborn_georeference import (
    AFFINE_METADATA_KEY,
    PIPELINE_METADATA_KEY,
    PIPELINE_METADATA_VALUE,
    POINTS_METADATA_KEY,
    SOURCE_METADATA_KEY,
    affine_diagnostics,
    affine_provenance_signature,
    affine_safety_warnings,
    read_points,
    require_expected_crs,
    require_target_location,
    split_affine_safety_warnings,
    target_location_diagnostics,
)
from sanborn_index import (
    DEFAULT_INDEX as DEFAULT_INDEX_RASTER,
    map_to_pixel as index_map_to_pixel,
    pixel_to_map as index_pixel_to_map,
)


PROJECT_ROOT = Path(
    os.environ.get("SANBORN_PROJECT_ROOT", Path(__file__).resolve().parents[1])
).expanduser().resolve()
DOWNLOAD_DIR = Path(
    os.environ.get("SANBORN_DOWNLOAD_DIR", PROJECT_ROOT / "1911 SANBORN DOWNLOADS")
).expanduser().resolve()
BATCH_DIR = Path(
    os.environ.get("SANBORN_BATCH_DIR", PROJECT_ROOT / "batch")
).expanduser().resolve()
DEFAULT_DB = BATCH_DIR / "sanborn_batch.sqlite3"
DEFAULT_OSM_CACHE = BATCH_DIR / "osm" / "atlanta-streets.osm"
DEFAULT_OSM_DB = BATCH_DIR / "osm" / "atlanta-streets.sqlite3"
DEFAULT_INDEX_JSON = BATCH_DIR / "index" / "tile-location-seeds.json"
DEFAULT_INDEX_CACHE = BATCH_DIR / "index" / "cache"
LOCK_DIR = BATCH_DIR / "locks"
DEFAULT_ALIASES = PROJECT_ROOT / "config" / "street_aliases.json"
PROTECTED_PROJECT = PROJECT_ROOT.parent / "JLS Master Map File.qgz"
USER_AGENT = "Joel-Silverman-Sanborn-Bot/1.0 (single-sheet historical map research)"
DATABASE_SCHEMA_VERSION = 4
MAX_TARGET_SEED_DISTANCE = 250.0
VOLUMES = {
    1: "sanborn01378_006",
    2: "sanborn01378_007",
    3: "sanborn01378_008",
    4: "sanborn01378_009",
}

# State changes are deliberately narrow.  A failed or interrupted item never
# silently re-enters the worker: Joel or ChatGPT must use the explicit reopen
# command after the cause has been inspected.
LEGAL_TRANSITIONS = {
    "queued": {"downloading", "failed"},
    "downloading": {"review-ready", "number-check-required", "failed"},
    "number-check-required": {"review-ready", "failed"},
    "review-ready": {"proposing", "awaiting-approval", "failed"},
    "proposing": {"needs-chatgpt-review", "failed"},
    "needs-chatgpt-review": {
        "awaiting-approval",
        "proposal-rejected",
        "proposal-stale",
        "failed",
    },
    "proposal-stale": {"proposing", "failed"},
    "proposal-rejected": set(),
    "awaiting-approval": {"approved", "failed"},
    "approved": {"verified", "failed"},
    "verified": set(),
    "failed": set(),
}
REOPENABLE_STATUSES = {
    "downloading",
    "proposing",
    "failed",
    "number-check-required",
    "needs-chatgpt-review",
    "proposal-rejected",
    "awaiting-approval",
    "approved",
    "proposal-stale",
    "verified",
}
_INDEX_RECORD_CACHE: dict[tuple[str, int, int, int, int, int], tuple[dict, str]] = {}
_SHA256_CACHE: dict[tuple[str, int, int, int, int, int], str] = {}


def fail(message: str) -> "NoReturn":
    raise RuntimeError(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def tile_file_lock(tile: int):
    """Hold the one cross-process lock that owns a tile's files and queue state."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_DIR / f"tile-{tile:04d}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def tile_locked(function):
    @wraps(function)
    def locked(db: sqlite3.Connection, tile: int, *args, **kwargs):
        with tile_file_lock(tile):
            return function(db, tile, *args, **kwargs)

    return locked


def command_tile_locked(function):
    @wraps(function)
    def locked(args: argparse.Namespace):
        with tile_file_lock(int(args.tile)):
            return function(args)

    return locked


def source_name(tile: int) -> str:
    return f"Sanborn 1911 -- Tile {tile}.jp2"


def points_name(tile: int) -> str:
    return f"Sanborn 1911 -- Tile {tile}_3points.points"


def output_name(tile: int) -> str:
    return f"Sanborn 1911 -- Tile {tile}_georeferenced.tif"


def deterministic_url(tile: int, volume: int) -> str:
    return (
        "https://tile.loc.gov/storage-services/service/gmd/gmd392m/g3924m/"
        f"g3924am/g3924am_g0137819110{volume}/01378_0{volume}_1911-{tile:04d}.jp2"
    )


def sha256(path: Path) -> str:
    """Hash a stable file once per process, while detecting mid-read changes."""
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


def require_program(name: str) -> str:
    path = shutil.which(name)
    if not path:
        fail(f"Required local program is missing: {name}")
    return path


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS catalog (
            tile INTEGER PRIMARY KEY,
            volume INTEGER NOT NULL,
            url TEXT NOT NULL,
            item_url TEXT NOT NULL,
            refreshed_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tiles (
            tile INTEGER PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'queued',
            volume INTEGER,
            url TEXT,
            source_path TEXT,
            source_sha256 TEXT,
            source_bytes INTEGER,
            width INTEGER,
            height INTEGER,
            preview_path TEXT,
            ocr_path TEXT,
            spatial_ocr_path TEXT,
            spatial_ocr_sha256 TEXT,
            printed_number_seen INTEGER,
            proposal_path TEXT,
            proposal_sha256 TEXT,
            proposal_inputs_sha256 TEXT,
            proposal_evidence_json TEXT,
            proposal_corrections_json TEXT,
            proposal_rejections_json TEXT,
            target_seed_x REAL,
            target_seed_y REAL,
            target_seed_max_distance REAL,
            target_seed_quality TEXT,
            target_seed_ambiguous INTEGER,
            target_seed_json TEXT,
            target_seed_provenance_json TEXT,
            points_path TEXT,
            review_dir TEXT,
            output_path TEXT,
            error TEXT,
            failure_stage TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            updated_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tile INTEGER NOT NULL,
            created_utc TEXT NOT NULL,
            event TEXT NOT NULL,
            detail TEXT
        );
        CREATE TABLE IF NOT EXISTS proposal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tile INTEGER NOT NULL,
            created_utc TEXT NOT NULL,
            proposal_path TEXT NOT NULL,
            proposal_sha256 TEXT NOT NULL,
            input_sha256 TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            corrections_json TEXT NOT NULL DEFAULT '[]',
            rejections_json TEXT NOT NULL DEFAULT '[]',
            FOREIGN KEY(tile) REFERENCES tiles(tile)
        );
        """
    )
    _migrate_schema(connection)
    return connection


def _migrate_schema(db: sqlite3.Connection) -> None:
    """Bring an existing phase-one database forward without losing its queue."""
    columns = {
        str(row["name"])
        for row in db.execute("PRAGMA table_info(tiles)").fetchall()
    }
    additions = {
        "spatial_ocr_path": "TEXT",
        "spatial_ocr_sha256": "TEXT",
        "proposal_path": "TEXT",
        "proposal_sha256": "TEXT",
        "proposal_inputs_sha256": "TEXT",
        "proposal_evidence_json": "TEXT",
        "proposal_corrections_json": "TEXT",
        "proposal_rejections_json": "TEXT",
        "target_seed_x": "REAL",
        "target_seed_y": "REAL",
        "target_seed_max_distance": "REAL",
        "target_seed_quality": "TEXT",
        "target_seed_ambiguous": "INTEGER",
        "target_seed_json": "TEXT",
        "target_seed_provenance_json": "TEXT",
        "failure_stage": "TEXT",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, declaration in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE tiles ADD COLUMN {name} {declaration}")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS proposal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tile INTEGER NOT NULL,
            created_utc TEXT NOT NULL,
            proposal_path TEXT NOT NULL,
            proposal_sha256 TEXT NOT NULL,
            input_sha256 TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            corrections_json TEXT NOT NULL DEFAULT '[]',
            rejections_json TEXT NOT NULL DEFAULT '[]',
            FOREIGN KEY(tile) REFERENCES tiles(tile)
        )
        """
    )
    db.execute(f"PRAGMA user_version={DATABASE_SCHEMA_VERSION}")
    db.commit()


def event(db: sqlite3.Connection, tile: int, name: str, detail: str = "") -> None:
    db.execute(
        "INSERT INTO events(tile, created_utc, event, detail) VALUES (?, ?, ?, ?)",
        (tile, utc_now(), name, detail),
    )


def update_tile(db: sqlite3.Connection, tile: int, **fields: object) -> None:
    fields["updated_utc"] = utc_now()
    assignments = ", ".join(f"{key}=?" for key in fields)
    db.execute(
        f"UPDATE tiles SET {assignments} WHERE tile=?",
        [*fields.values(), tile],
    )


def transition_tile(
    db: sqlite3.Connection,
    tile: int,
    new_status: str,
    *,
    detail: str = "",
    **fields: object,
) -> bool:
    """Apply one declared queue transition and record it atomically."""
    row = db.execute("SELECT status FROM tiles WHERE tile=?", (tile,)).fetchone()
    if not row:
        fail(f"Tile {tile} is not in the queue.")
    current = str(row["status"])
    if current == new_status:
        return False
    if current not in LEGAL_TRANSITIONS:
        fail(f"Tile {tile} has unknown queue status {current!r}.")
    if new_status not in LEGAL_TRANSITIONS[current]:
        fail(f"Illegal tile {tile} state change: {current} -> {new_status}.")
    update_tile(db, tile, status=new_status, **fields)
    event(db, tile, new_status, detail)
    return True


def _write_json_atomic(path: Path, record: dict) -> None:
    """Publish a small control record without exposing a half-written file."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _restore_exact_interrupted_archive(
    archive_root: Path,
    candidates: list[Path],
    tile: int,
) -> bool:
    """Restore only a hash-bound retirement that crashed before DB transition."""
    expected_paths = {str(path.resolve()) for path in candidates}
    matches: list[tuple[Path, dict, dict[str, dict]]] = []
    for prior in sorted(archive_root.glob("*"), reverse=True):
        state_path = prior / "retirement-state.json"
        if not prior.is_dir() or not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entries = state.get("originals")
        if (
            state.get("schema_version") != 1
            or state.get("state") != "retiring"
            or state.get("tile") != tile
            or not isinstance(entries, list)
        ):
            continue
        by_path = {
            str(entry.get("path")): entry
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        if set(by_path) != expected_paths:
            continue
        valid = True
        originals = prior / "originals"
        for path in candidates:
            entry = by_path[str(path.resolve())]
            digest = entry.get("sha256")
            saved = Path(str(entry.get("copy", ""))).expanduser().resolve()
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or saved != (originals / path.name).resolve()
                or not saved.is_file()
                or sha256(saved) != digest
                or (path.exists() and (not path.is_file() or sha256(path) != digest))
            ):
                valid = False
                break
        if valid:
            matches.append((state_path, state, by_path))
    if not matches:
        return False
    if len(matches) != 1:
        fail(
            f"Tile {tile} has more than one matching interrupted archive retirement; "
            "inspect them before reopening."
        )
    state_path, state, by_path = matches[0]
    for path in candidates:
        if path.is_file():
            continue
        saved = Path(str(by_path[str(path.resolve())]["copy"])).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(saved, path)
        if sha256(path) != by_path[str(path.resolve())]["sha256"]:
            fail(f"Interrupted archive recovery did not verify for {path}.")
    state["state"] = "restored-after-interruption"
    state["restored_utc"] = utc_now()
    _write_json_atomic(state_path, state)
    return True


def _archive_verified_result(row: sqlite3.Row) -> list[str]:
    """Create an immutable, relocation-aware bundle before reusing a fixed name."""
    tile = int(row["tile"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_root = BATCH_DIR / "archive" / f"tile-{tile:04d}"
    archive = archive_root / stamp
    candidates: list[Path] = []
    if row["output_path"]:
        output = Path(str(row["output_path"])).expanduser().resolve()
        candidates.extend([output, output.with_suffix(".georef.json")])
    candidates.append(BATCH_DIR / "qgis-import" / f"tile-{tile:04d}.json")
    # A process may have stopped while retiring fixed-name originals. Restore
    # only an explicitly incomplete attempt that is bound to these exact paths
    # and hashes. Never infer identity from an arbitrary older archive.
    if row["output_path"] and any(not path.is_file() for path in candidates):
        _restore_exact_interrupted_archive(archive_root, candidates, tile)
    present = [path for path in candidates if path.is_file()]
    if not present:
        if row["output_path"]:
            fail(
                "The database still marks this tile verified, but its fixed-name "
                "GeoTIFF, ledger, and QGIS manifest are all missing. No matching "
                "interrupted retirement record was found."
            )
        return []
    if not all(path.is_file() for path in candidates):
        fail(
            "The verified GeoTIFF, ledger, and QGIS manifest are not a complete set; "
            "repair them before reopening."
        )
    counter = 1
    while archive.exists():
        archive = archive_root / f"{stamp}-{counter}"
        counter += 1
    archive.mkdir(parents=True, exist_ok=False)
    originals = archive / "originals"
    bundle = archive / "verified-bundle"
    originals.mkdir()
    bundle.mkdir()
    copied: list[tuple[Path, Path]] = []
    try:
        for path in candidates:
            destination = originals / path.name
            shutil.copy2(path, destination)
            if destination.stat().st_size != path.stat().st_size or sha256(destination) != sha256(path):
                fail(f"Archived copy did not verify for {path}.")
            copied.append((path, destination))

        output, ledger, manifest = candidates
        archived_output = bundle / output.name
        shutil.copy2(output, archived_output)
        if sha256(archived_output) != sha256(output):
            fail("Relocated archive raster did not preserve the verified pixels.")

        ledger_record = json.loads(ledger.read_text(encoding="utf-8"))
        if ledger_record.get("schema_version") != 1:
            fail("The verified ledger cannot be relocated because its schema is unsupported.")
        ledger_record.setdefault("archive", {})
        ledger_record["archive"] = {
            "archived_utc": utc_now(),
            "original_path": str(ledger),
            "original_sha256": sha256(ledger),
        }
        ledger_record["output"]["path"] = str(archived_output)
        archived_ledger = bundle / ledger.name
        archived_ledger.write_text(
            json.dumps(ledger_record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        manifest_record = json.loads(manifest.read_text(encoding="utf-8"))
        if manifest_record.get("schema_version") != 3:
            fail("The verified QGIS manifest cannot be relocated because its schema is unsupported.")
        manifest_record["path"] = str(archived_output)
        manifest_record["ledger_path"] = str(archived_ledger)
        manifest_record["ledger_sha256"] = sha256(archived_ledger)
        manifest_record["archive"] = {
            "archived_utc": utc_now(),
            "original_path": str(manifest),
            "original_sha256": sha256(manifest),
        }
        archived_manifest = bundle / manifest.name
        archived_manifest.write_text(
            json.dumps(manifest_record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        validate_qgis_manifest(archived_manifest)

        evidence_copies: list[dict[str, str]] = []
        review_value = manifest_record.get("review_dir")
        if isinstance(review_value, str):
            review_dir = Path(review_value).expanduser().resolve()
            if review_dir.is_dir():
                evidence_dir = archive / "evidence" / "review"
                shutil.copytree(review_dir, evidence_dir)
                for saved in sorted(path for path in evidence_dir.rglob("*") if path.is_file()):
                    original = review_dir / saved.relative_to(evidence_dir)
                    if sha256(saved) != sha256(original):
                        fail(f"Archived review evidence did not verify for {original}.")
                    evidence_copies.append(
                        {
                            "original": str(original),
                            "copy": str(saved),
                            "sha256": sha256(saved),
                        }
                    )

        archive_index = archive / "archive-index.json"
        archive_index.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "tile": tile,
                    "created_utc": utc_now(),
                    "purpose": "immutable prior verified result before corrected fixed-name reuse",
                    "originals": [
                        {
                            "path": str(original),
                            "sha256": sha256(saved),
                            "copy": str(saved),
                        }
                        for original, saved in copied
                    ],
                    "relocated_verified_manifest": str(archived_manifest),
                    "relocated_verified_manifest_sha256": sha256(archived_manifest),
                    "evidence_copies": evidence_copies,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        retirement_state = archive / "retirement-state.json"
        retirement_record = {
            "schema_version": 1,
            "state": "retiring",
            "tile": tile,
            "created_utc": utc_now(),
            "originals": [
                {
                    "path": str(original.resolve()),
                    "sha256": sha256(saved),
                    "copy": str(saved.resolve()),
                }
                for original, saved in copied
            ],
        }
        _write_json_atomic(retirement_state, retirement_record)
        for path, _ in copied:
            path.unlink()
        retirement_record["state"] = "retired"
        retirement_record["retired_utc"] = utc_now()
        _write_json_atomic(retirement_state, retirement_record)
    except Exception:
        for original, saved in copied:
            if not original.exists() and saved.is_file():
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(saved, original)
        state_path = archive / "retirement-state.json"
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["state"] = "rolled-back"
                state["rolled_back_utc"] = utc_now()
                _write_json_atomic(state_path, state)
            except (OSError, json.JSONDecodeError):
                pass
        raise
    return [str(archive / "archive-index.json"), str(bundle / manifest.name)]


def reopen_tile(
    db: sqlite3.Connection,
    tile: int,
    destination: str,
    note: str,
) -> None:
    """Explicitly release a stopped item after somebody has inspected it."""
    row = db.execute("SELECT * FROM tiles WHERE tile=?", (tile,)).fetchone()
    if not row:
        fail(f"Tile {tile} is not in the queue.")
    current = str(row["status"])
    if current not in REOPENABLE_STATUSES:
        fail(f"Tile {tile} in status {current} cannot be reopened.")
    if destination not in {"queued", "review-ready", "approved"}:
        fail("A reopened tile must return to queued, review-ready, or approved.")
    if destination == "approved":
        if current != "verified":
            fail("Only a previously verified tile can be reopened directly to approved.")
        if not row["review_dir"] or not row["points_path"]:
            fail(f"Tile {tile} no longer has its approved review evidence.")
        require_approval(Path(str(row["review_dir"])))
    if destination == "review-ready":
        required = ("source_path", "source_sha256", "spatial_ocr_path")
        missing = [name for name in required if not row[name]]
        if missing or row["printed_number_seen"] != 1:
            fail(
                f"Tile {tile} cannot return to review-ready; its verified source, "
                "spatial OCR, or printed-number check is missing. Reopen it to queued."
            )
        if not Path(str(row["spatial_ocr_path"])).is_file():
            fail(f"Tile {tile}'s spatial OCR record no longer exists. Reopen it to queued.")
    archived: list[str] = []
    if current == "verified" and destination in {"queued", "review-ready"}:
        archived = _archive_verified_result(row)
    update_tile(
        db,
        tile,
        status=destination,
        error=None,
        failure_stage=None,
        points_path=(
            None
            if destination in {"queued", "review-ready"}
            and current in {"awaiting-approval", "approved", "verified"}
            else row["points_path"]
        ),
        review_dir=(
            None
            if destination in {"queued", "review-ready"}
            and current in {"awaiting-approval", "approved", "verified"}
            else row["review_dir"]
        ),
        output_path=(
            None
            if current == "verified" and destination in {"queued", "review-ready"}
            else row["output_path"]
        ),
        # A rejected proposal must be rebuilt even when its data files did not
        # change.  This also picks up improvements to the local matching code.
        proposal_inputs_sha256=(
            None if current == "proposal-rejected" and destination == "review-ready"
            else row["proposal_inputs_sha256"]
        ),
    )
    event(db, tile, "reopened", f"{current} -> {destination}: {note}")
    if archived:
        event(db, tile, "prior-result-archived", json.dumps(archived, sort_keys=True))


def parse_tiles(values: list[str]) -> list[int]:
    tiles: set[int] = set()
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start_text, end_text = part.split("-", 1)
                start, end = int(start_text), int(end_text)
                if end < start:
                    fail(f"Invalid descending tile range: {part}")
                tiles.update(range(start, end + 1))
            else:
                tiles.add(int(part))
    if not tiles or min(tiles) < 1:
        fail("Provide one or more positive tile numbers or ranges.")
    return sorted(tiles)


def request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None):
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json,*/*"}
    merged.update(headers or {})
    last_error = None
    for attempt in range(4):
        try:
            response = urlopen(Request(url, method=method, headers=merged), timeout=90)
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                sample = response.read(4096).decode("utf-8", "ignore").lower()
                response.close()
                if "prove you are human" in sample or "captcha" in sample:
                    fail("Library of Congress requested human verification. Pause for Joel.")
                fail(f"Expected data but received an HTML page from {url}")
            return response
        except HTTPError as error:
            last_error = error
            if error.code not in {429, 500, 502, 503, 504, 520, 522, 524}:
                raise
        except URLError as error:
            last_error = error
        if attempt < 3:
            wait = 3 * (2 ** attempt)
            print(f"LOC request paused {wait}s after a temporary error...", file=sys.stderr)
            time.sleep(wait)
    raise last_error


def all_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from all_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_strings(child)


def refresh_catalog(db: sqlite3.Connection) -> int:
    found: dict[int, tuple[int, str, str]] = {}
    for volume, item in VOLUMES.items():
        item_url = f"https://www.loc.gov/item/{item}/"
        api_url = f"{item_url}?fo=json&at=resources"
        print(f"Reading LOC Volume {volume} catalog...")
        with closing(request(api_url)) as response:
            payload = json.load(response)
        for text in all_strings(payload):
            for match in re.finditer(
                rf"https?://[^\s\"']*01378_0{volume}_1911-(\d{{4}})\.jp2(?:\?[^\s\"']*)?",
                text,
            ):
                tile = int(match.group(1))
                found[tile] = (volume, match.group(0), item_url)
        time.sleep(2.0)
    if not found:
        fail("No JPEG2000 resources were found in the four LOC volume catalogs.")
    refreshed = utc_now()
    for tile, (volume, url, item_url) in found.items():
        db.execute(
            """
            INSERT INTO catalog(tile, volume, url, item_url, refreshed_utc)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tile) DO UPDATE SET
              volume=excluded.volume, url=excluded.url,
              item_url=excluded.item_url, refreshed_utc=excluded.refreshed_utc
            """,
            (tile, volume, url, item_url, refreshed),
        )
    db.commit()
    print(f"Cataloged {len(found)} printed tiles from four official LOC volumes.")
    return len(found)


def url_exists(url: str) -> tuple[bool, int | None]:
    try:
        with closing(request(url, method="HEAD")) as response:
            length = response.headers.get("Content-Length")
            return response.status == 200, int(length) if length else None
    except HTTPError as error:
        if error.code == 404:
            return False, None
        raise


def resolve_tile(db: sqlite3.Connection, tile: int) -> tuple[int, str]:
    row = db.execute("SELECT volume, url FROM catalog WHERE tile=?", (tile,)).fetchone()
    if row:
        return int(row["volume"]), str(row["url"])
    queued = db.execute("SELECT volume, url FROM tiles WHERE tile=?", (tile,)).fetchone()
    if queued and queued["volume"]:
        volume = int(queued["volume"])
        return volume, str(queued["url"] or deterministic_url(tile, volume))
    print(f"Tile {tile} is not cached; conservatively checking the four volume URLs.")
    for volume in VOLUMES:
        url = deterministic_url(tile, volume)
        exists, _ = url_exists(url)
        if exists:
            item_url = f"https://www.loc.gov/item/{VOLUMES[volume]}/"
            db.execute(
                "INSERT OR REPLACE INTO catalog VALUES (?, ?, ?, ?, ?)",
                (tile, volume, url, item_url, utc_now()),
            )
            db.commit()
            return volume, url
        time.sleep(0.5)
    fail(f"Tile {tile} was not found in any of the four 1911 LOC volumes.")


def quarantine(path: Path, reason: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantined = path.with_name(f"{path.stem}.invalid-{stamp}{path.suffix}")
    path.replace(quarantined)
    print(f"Quarantined {path.name}: {reason}; saved as {quarantined.name}")
    return quarantined


def download(url: str, destination: Path, expected_bytes: int | None = None) -> None:
    if destination.exists():
        if expected_bytes is None or destination.stat().st_size == expected_bytes:
            return
        quarantine(
            destination,
            f"local size {destination.stat().st_size} differs from LOC size {expected_bytes}",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    if partial.exists() and expected_bytes is not None:
        partial_bytes = partial.stat().st_size
        if partial_bytes == expected_bytes:
            partial.replace(destination)
            return
        if partial_bytes > expected_bytes:
            quarantine(
                partial,
                f"partial size {partial_bytes} exceeds LOC size {expected_bytes}",
            )
    start = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={start}-"} if start else {}
    with closing(request(url, headers=headers)) as response:
        if start and response.status != 206:
            partial.unlink()
            start = 0
        mode = "ab" if start else "wb"
        with partial.open(mode) as handle:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                handle.write(block)
    partial.replace(destination)
    if expected_bytes is not None and destination.stat().st_size != expected_bytes:
        actual = destination.stat().st_size
        quarantine(destination, f"download size {actual} differs from LOC size {expected_bytes}")
        fail("The completed download did not match the Library of Congress byte length.")
    clear_macos_download_warning(destination)


def clear_macos_download_warning(path: Path) -> None:
    """Remove the marking that makes macOS ask before opening a saved scan.

    The scans come from the Library of Congress and their byte length is
    checked above, so the warning only stands between Joel and a file he
    already trusts. A missing marking, or a system without the tool, is
    normal and must never interrupt a download.
    """
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["/usr/bin/xattr", "-d", "com.apple.quarantine", str(path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def image_info(path: Path) -> tuple[int, int]:
    gdalinfo = require_program("gdalinfo")
    result = subprocess.run(
        [gdalinfo, "-json", str(path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    info = json.loads(result.stdout)
    if info.get("driverShortName") not in {"JP2OpenJPEG", "JP2KAK", "JPEG2000"}:
        fail(f"Downloaded file is not recognized as JPEG2000: {path}")
    return int(info["size"][0]), int(info["size"][1])


def _valid_spatial_ocr(path: Path, tile: int, source: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        recorded_source = record["source"]
        if int(record.get("schema_version", 0)) != 2:
            return None
        if record.get("printed_tile_number_method") != "apple-vision-title-region":
            return None
        if int(record.get("expected_tile")) != tile:
            return None
        if str(recorded_source["sha256"]) != sha256(source):
            return None
        preview_record = record["preview"]
        if not isinstance(preview_record, dict):
            return None
        preview_path = preview_record.get("path")
        preview_digest = preview_record.get("sha256")
        preview_bytes = preview_record.get("bytes")
        preview_width = preview_record.get("width")
        preview_height = preview_record.get("height")
        if (
            not isinstance(preview_path, str)
            or not Path(preview_path).expanduser().is_absolute()
            or not isinstance(preview_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", preview_digest) is None
            or not isinstance(preview_bytes, int)
            or isinstance(preview_bytes, bool)
            or preview_bytes <= 0
            or not isinstance(preview_width, int)
            or isinstance(preview_width, bool)
            or preview_width <= 0
            or not isinstance(preview_height, int)
            or isinstance(preview_height, bool)
            or preview_height <= 0
        ):
            return None
        preview = Path(preview_path).expanduser().resolve()
        if (
            not preview.is_file()
            or preview.stat().st_size != preview_bytes
            or sha256(preview) != preview_digest
        ):
            return None
        with Image.open(preview) as image:
            live_preview_size = (int(image.width), int(image.height))
            image.verify()
        if live_preview_size != (preview_width, preview_height):
            return None
        verification = record.get("printed_tile_number_verification")
        if not isinstance(verification, dict):
            return None
        verification_provenance = verification.get("provenance")
        if (
            not isinstance(verification_provenance, dict)
            or verification_provenance.get("preview_sha256") != preview_digest
        ):
            return None
        if bool(record.get("printed_tile_number_seen")) != (
            verification.get("status") == "verified"
        ):
            return None
        return record
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return None


def create_preview(tile: int, source: Path) -> tuple[Path, Path, bool]:
    """Create or reuse spatial OCR whose coordinates refer to the full scan."""
    preview_dir = BATCH_DIR / "previews"
    ocr_dir = BATCH_DIR / "ocr"
    preview_dir.mkdir(parents=True, exist_ok=True)
    ocr_dir.mkdir(parents=True, exist_ok=True)
    ocr = ocr_dir / f"tile-{tile:04d}.spatial.json"
    existing = _valid_spatial_ocr(ocr, tile, source)
    if existing is None:
        command = [
            sys.executable,
            str(Path(__file__).with_name("sanborn_ocr.py")),
            "--source",
            str(source),
            "--output",
            str(ocr),
            "--tile",
            str(tile),
        ]
        subprocess.run(command, check=True)
        existing = _valid_spatial_ocr(ocr, tile, source)
        if existing is None:
            fail(f"Spatial OCR for tile {tile} did not produce a valid, source-bound record.")
    preview = Path(str(existing["preview"]["path"])).expanduser().resolve()
    return preview, ocr, bool(existing["printed_tile_number_seen"])


@tile_locked
def _mark_failed(
    db: sqlite3.Connection,
    tile: int,
    stage: str,
    error: Exception,
) -> None:
    row = db.execute("SELECT status FROM tiles WHERE tile=?", (tile,)).fetchone()
    if not row:
        return
    current = str(row["status"])
    message = str(error)
    expected_active = {
        "prepare": {"downloading"},
        "propose": {"proposing"},
    }.get(stage)
    if expected_active is not None and current not in expected_active and current != "failed":
        event(
            db,
            tile,
            "stale-failure-ignored",
            f"{stage}: queue already advanced to {current}; original error: {message}",
        )
        db.commit()
        return
    if current == "failed":
        update_tile(db, tile, error=message, failure_stage=stage)
        event(db, tile, "failure-updated", f"{stage}: {message}")
    elif "failed" in LEGAL_TRANSITIONS.get(current, set()):
        transition_tile(
            db,
            tile,
            "failed",
            detail=f"{stage}: {message}",
            error=message,
            failure_stage=stage,
        )
    else:
        fail(f"Tile {tile} failed during {stage}, but status {current} cannot fail: {message}")


def _ensure_spatial_ocr(db: sqlite3.Connection, row: sqlite3.Row) -> Path:
    """Upgrade a phase-one prepared tile in place, without downloading it again."""
    tile = int(row["tile"])
    source = Path(str(row["source_path"] or ""))
    if not source.is_file():
        fail(f"Tile {tile}'s prepared source scan no longer exists; reopen it to queued.")
    if row["source_sha256"] and sha256(source) != row["source_sha256"]:
        fail(f"Tile {tile}'s prepared source scan changed; reopen it to queued.")
    for value in (row["spatial_ocr_path"], row["ocr_path"]):
        if not value:
            continue
        candidate = Path(str(value)).expanduser().resolve()
        record = _valid_spatial_ocr(candidate, tile, source)
        if record is not None:
            if row["spatial_ocr_path"] != str(candidate):
                update_tile(
                    db,
                    tile,
                    spatial_ocr_path=str(candidate),
                    spatial_ocr_sha256=sha256(candidate),
                    preview_path=str(Path(str(record["preview"]["path"])).resolve()),
                )
                db.commit()
            return candidate

    preview, spatial_ocr, newly_seen = create_preview(tile, source)
    printed_seen = row["printed_number_seen"] == 1 or newly_seen
    update_tile(
        db,
        tile,
        preview_path=str(preview),
        ocr_path=str(spatial_ocr),
        spatial_ocr_path=str(spatial_ocr),
        spatial_ocr_sha256=sha256(spatial_ocr),
        printed_number_seen=int(printed_seen),
    )
    event(db, tile, "spatial-ocr-upgraded", str(spatial_ocr))
    db.commit()
    if not printed_seen:
        fail(f"Tile {tile} still needs a visual printed-number check before matching.")
    return spatial_ocr


def _read_index_record(index_json: Path) -> tuple[dict, str]:
    resolved = index_json.expanduser().resolve()
    before = resolved.stat()
    key = (
        str(resolved),
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    cached = _INDEX_RECORD_CACHE.get(key)
    if cached is not None:
        return cached
    payload = resolved.read_bytes()
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
        fail(f"Tile-location seed file changed while it was being read: {resolved}")
    record = json.loads(payload.decode("utf-8"))
    if not isinstance(record.get("seeds", []), list):
        fail(f"Tile-location seed file has no seed list: {resolved}")
    digest = hashlib.sha256(payload).hexdigest()
    _SHA256_CACHE[key] = digest
    result = (record, digest)
    _INDEX_RECORD_CACHE.clear()
    _INDEX_RECORD_CACHE[key] = result
    return result


def _validate_index_raster_evidence(record: dict, tile: int) -> None:
    """Require the seed file to remain bound to the same georeferenced index raster."""
    evidence = record.get("index")
    if not isinstance(evidence, dict):
        fail(f"Tile {tile}'s seed file has no georeferenced index-raster evidence.")
    raw_path = evidence.get("path")
    expected_digest = evidence.get("sha256")
    if (
        not isinstance(raw_path, str)
        or not Path(raw_path).expanduser().is_absolute()
        or not isinstance(expected_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
    ):
        fail(f"Tile {tile}'s index-raster path or SHA-256 evidence is invalid; rebuild the index.")
    raster = Path(raw_path).expanduser().resolve()
    if not raster.is_file():
        fail(f"Tile {tile}'s georeferenced index raster is missing: {raster}")
    actual_digest = sha256(raster)
    if actual_digest != expected_digest:
        fail(f"Tile {tile}'s georeferenced index raster changed; rebuild or reconfirm the seed.")
    try:
        width = int(evidence["width"])
        height = int(evidence["height"])
        transform = [float(value) for value in evidence["geotransform"]]
    except (KeyError, TypeError, ValueError):
        fail(f"Tile {tile}'s index-raster geometry evidence is incomplete.")
    if (
        width <= 0
        or height <= 0
        or len(transform) != 6
        or not all(math.isfinite(value) for value in transform)
        or str(evidence.get("crs", "")).upper() != "EPSG:3857"
    ):
        fail(f"Tile {tile}'s index-raster geometry evidence is invalid.")
    if record.get("record_type") != "sanborn-manual-tile-seed-override":
        preview = record.get("preview")
        if not isinstance(preview, dict):
            fail(f"Tile {tile}'s automatic seed file has no hashed index-preview evidence.")
        preview_path_value = preview.get("path")
        preview_digest = preview.get("sha256")
        if (
            not isinstance(preview_path_value, str)
            or not Path(preview_path_value).expanduser().is_absolute()
            or not isinstance(preview_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", preview_digest) is None
        ):
            fail(f"Tile {tile}'s automatic index-preview evidence is invalid; rebuild the index.")
        preview_path = Path(preview_path_value).expanduser().resolve()
        if (
            not preview_path.is_file()
            or preview_path.stat().st_size != int(preview.get("bytes", -1))
            or sha256(preview_path) != preview_digest
        ):
            fail(f"Tile {tile}'s automatic index preview changed; rebuild the seed index.")


def _manual_seed_override_path(index_json: Path, tile: int) -> Path:
    resolved = index_json.expanduser().resolve()
    return (
        resolved.parent
        / f"{resolved.stem}-manual-overrides"
        / f"tile-{tile:04d}.json"
    )


def _validated_manual_seed_override(
    path: Path,
    tile: int,
    *,
    expected_base: Path | None = None,
) -> tuple[dict, str]:
    """Verify one immutable per-tile steering decision and its index evidence."""
    record, digest = _read_index_record(path)
    if (
        record.get("schema_version") != 1
        or record.get("record_type") != "sanborn-manual-tile-seed-override"
        or record.get("tile") != tile
    ):
        fail(f"Tile {tile}'s manual seed override has the wrong schema or identity: {path}")
    base = record.get("base_index")
    raster = record.get("index_raster")
    if not isinstance(base, dict) or not isinstance(raster, dict):
        fail(f"Tile {tile}'s manual seed override lacks base-index or raster evidence.")
    base_path_value = base.get("path")
    raster_path_value = raster.get("path")
    if (
        not isinstance(base_path_value, str)
        or not Path(base_path_value).expanduser().is_absolute()
        or not isinstance(raster_path_value, str)
        or not Path(raster_path_value).expanduser().is_absolute()
    ):
        fail(f"Tile {tile}'s manual seed override evidence paths must be absolute.")
    base_path = Path(base_path_value).expanduser().resolve()
    raster_path = Path(raster_path_value).expanduser().resolve()
    if expected_base is not None and base_path != expected_base.expanduser().resolve():
        fail(f"Tile {tile}'s manual seed belongs to a different shared OCR index.")
    if not base_path.is_file() or sha256(base_path) != base.get("sha256"):
        fail(f"Tile {tile}'s shared index changed after its manual seed was confirmed.")
    if not raster_path.is_file() or sha256(raster_path) != raster.get("sha256"):
        fail(f"Tile {tile}'s georeferenced index raster changed after manual confirmation.")
    base_record, _ = _read_index_record(base_path)
    base_raster = base_record.get("index")
    if not isinstance(base_raster, dict):
        fail(f"Tile {tile}'s shared index has no raster evidence block.")
    if (
        Path(str(base_raster.get("path", ""))).expanduser().resolve() != raster_path
        or base_raster.get("sha256") != raster.get("sha256")
        or int(base_raster.get("width", -1)) != int(raster.get("width", -2))
        or int(base_raster.get("height", -1)) != int(raster.get("height", -2))
        or base_raster.get("geotransform") != raster.get("geotransform")
        or str(base_raster.get("crs", "")).upper() != "EPSG:3857"
        or str(raster.get("crs", "")).upper() != "EPSG:3857"
    ):
        fail(f"Tile {tile}'s manual seed no longer matches the shared index geometry.")
    seeds = record.get("seeds")
    if not isinstance(seeds, list) or len(seeds) != 1 or not isinstance(seeds[0], dict):
        fail(f"Tile {tile}'s manual override must contain exactly one seed.")
    seed = seeds[0]
    try:
        seed_tile = int(seed["tile"])
        seed_x = float(seed["map_x"])
        seed_y = float(seed["map_y"])
        tolerance = float(seed["suggested_max_distance"])
    except (KeyError, TypeError, ValueError):
        fail(f"Tile {tile}'s manual seed lacks numeric location evidence.")
    if (
        seed_tile != tile
        or not math.isfinite(seed_x)
        or not math.isfinite(seed_y)
        or not (0 < tolerance <= MAX_TARGET_SEED_DISTANCE)
        or seed.get("quality") != "high"
        or seed.get("ambiguous") is not False
        or seed.get("source") != "manual-confirmation"
        or str(seed.get("crs", "")).upper() != "EPSG:3857"
    ):
        fail(f"Tile {tile}'s manual seed is not one selected high-quality EPSG:3857 seed.")
    confirmation = seed.get("manual_confirmation")
    if not isinstance(confirmation, dict):
        fail(f"Tile {tile}'s manual seed has no reviewed confirmation record.")
    if any(
        not isinstance(confirmation.get(key), str) or not str(confirmation.get(key)).strip()
        for key in ("reviewer", "note", "confirmed_utc")
    ):
        fail(f"Tile {tile}'s manual seed lacks a reviewer, reason, or timestamp.")
    if confirmation.get("index_json_path") != str(base_path):
        fail(f"Tile {tile}'s manual seed confirmation names a different shared index.")
    if confirmation.get("base_index_sha256") != base.get("sha256"):
        fail(f"Tile {tile}'s manual seed confirmation has stale base-index evidence.")
    if confirmation.get("index_raster") != raster:
        fail(f"Tile {tile}'s manual seed confirmation has different raster evidence.")
    if record.get("seed_count") != 1:
        fail(f"Tile {tile}'s manual override seed count is not exactly one.")
    base_seeds = base_record.get("seeds")
    if not isinstance(base_seeds, list):
        fail(f"Tile {tile}'s shared OCR index has a malformed seed list.")
    base_matches = [
        candidate
        for candidate in base_seeds
        if isinstance(candidate, dict) and int(candidate.get("tile", -1)) == tile
    ]
    strong_ocr = (
        len(base_matches) == 1
        and base_matches[0].get("quality") == "high"
        and base_matches[0].get("ambiguous") is False
    )
    if confirmation.get("overrode_high_confidence_ocr") is not strong_ocr:
        fail(f"Tile {tile}'s manual seed does not explicitly record its OCR override status.")
    if record.get("base_seed_records") != base_matches or seed.get("original_ocr_evidence") != base_matches:
        fail(f"Tile {tile}'s manual seed does not preserve the original OCR evidence.")
    history = record.get("history")
    if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
        fail(f"Tile {tile}'s manual seed has no confirmation history.")
    latest = history[-1]
    for key in (
        "reviewer",
        "note",
        "confirmed_utc",
        "method",
        "input",
        "index_json_path",
        "base_index_sha256",
        "index_raster",
        "overrode_high_confidence_ocr",
    ):
        if latest.get(key) != confirmation.get(key):
            fail(f"Tile {tile}'s latest manual-seed history differs from its confirmation.")
    if latest.get("tile") != tile or not math.isclose(
        float(latest.get("tolerance", -1)), tolerance, rel_tol=0.0, abs_tol=1e-9
    ):
        fail(f"Tile {tile}'s latest manual-seed history has a different tile or tolerance.")

    method = confirmation.get("method") if isinstance(confirmation, dict) else None
    input_evidence = confirmation.get("input")
    if not isinstance(input_evidence, dict):
        fail(f"Tile {tile}'s manual seed has no coordinate input evidence.")
    transform = [float(value) for value in raster["geotransform"]]
    raster_width = int(raster["width"])
    raster_height = int(raster["height"])
    try:
        stored_pixel_x = float(seed["index_pixel_x"])
        stored_pixel_y = float(seed["index_pixel_y"])
    except (KeyError, TypeError, ValueError):
        fail(f"Tile {tile}'s manual seed lacks index-pixel coordinates.")
    if not (
        0 <= stored_pixel_x < raster_width
        and 0 <= stored_pixel_y < raster_height
    ):
        fail(f"Tile {tile}'s manual seed lies outside the georeferenced index raster.")
    if method == "preview-pixel":
        base_preview = base_record.get("preview")
        override_preview = record.get("preview")
        if not isinstance(base_preview, dict) or not isinstance(override_preview, dict):
            fail(f"Tile {tile}'s preview-click seed lacks immutable preview evidence.")
        keys = ("path", "sha256", "bytes", "width", "height")
        if any(override_preview.get(key) != base_preview.get(key) for key in keys):
            fail(f"Tile {tile}'s manual seed preview differs from the shared index preview.")
        nested_preview = input_evidence.get("preview") if isinstance(input_evidence, dict) else None
        if not isinstance(nested_preview, dict) or any(
            nested_preview.get(key) != override_preview.get(key) for key in keys
        ):
            fail(f"Tile {tile}'s manual seed confirmation is not bound to its preview.")
        preview_path_value = override_preview.get("path")
        preview_digest = override_preview.get("sha256")
        if (
            not isinstance(preview_path_value, str)
            or not Path(preview_path_value).expanduser().is_absolute()
            or not isinstance(preview_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", preview_digest) is None
        ):
            fail(f"Tile {tile}'s manual seed preview path or digest is invalid.")
        preview_path = Path(preview_path_value).expanduser().resolve()
        if (
            not preview_path.is_file()
            or preview_path.stat().st_size != int(override_preview.get("bytes", -1))
            or sha256(preview_path) != preview_digest
        ):
            fail(f"Tile {tile}'s index preview changed after manual confirmation.")
        try:
            preview_x = float(input_evidence["preview_x"])
            preview_y = float(input_evidence["preview_y"])
            preview_width = int(override_preview["width"])
            preview_height = int(override_preview["height"])
        except (KeyError, TypeError, ValueError):
            fail(f"Tile {tile}'s preview-click seed has invalid pixel evidence.")
        expected_pixel_x = preview_x * raster_width / preview_width
        expected_pixel_y = preview_y * raster_height / preview_height
        expected_map_x, expected_map_y = index_pixel_to_map(
            expected_pixel_x, expected_pixel_y, transform
        )
    elif method == "map-coordinate":
        try:
            expected_map_x = float(input_evidence["map_x"])
            expected_map_y = float(input_evidence["map_y"])
        except (KeyError, TypeError, ValueError):
            fail(f"Tile {tile}'s map-coordinate seed has invalid input evidence.")
        if str(input_evidence.get("crs", "")).upper() != "EPSG:3857":
            fail(f"Tile {tile}'s map-coordinate seed input is not EPSG:3857.")
        expected_pixel_x, expected_pixel_y = index_map_to_pixel(
            expected_map_x, expected_map_y, transform
        )
    else:
        fail(f"Tile {tile}'s manual seed has an unknown confirmation method.")
    if not all(
        math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-6)
        for actual, expected in (
            (seed_x, expected_map_x),
            (seed_y, expected_map_y),
            (stored_pixel_x, expected_pixel_x),
            (stored_pixel_y, expected_pixel_y),
        )
    ):
        fail(f"Tile {tile}'s manual seed coordinates do not follow from its index evidence.")
    return record, digest


def tile_seed_context(index_json: Path, tile: int, *, missing_ok: bool = True) -> dict:
    """Return an independent location seed, or an explicit reviewable absence."""
    resolved = index_json.expanduser().resolve()
    if not resolved.is_file():
        if not missing_ok:
            fail(f"Tile-location seed file does not exist: {resolved}")
        return {
            "status": "missing-index",
            "tile": tile,
            "review_required": True,
            "reason": "The local georeferenced index seed file has not been built.",
            "provenance": {"path": str(resolved), "exists": False},
        }
    record, digest = _read_index_record(resolved)
    if record.get("record_type") == "sanborn-manual-tile-seed-override":
        record, digest = _validated_manual_seed_override(resolved, tile)
    else:
        override = _manual_seed_override_path(resolved, tile)
        if override.is_file():
            record, digest = _validated_manual_seed_override(
                override,
                tile,
                expected_base=resolved,
            )
            resolved = override
    _validate_index_raster_evidence(record, tile)
    provenance = {
        "path": str(resolved),
        "sha256": digest,
        "schema_version": record.get("schema_version"),
        "created_utc": record.get("created_utc"),
        "purpose": record.get("purpose"),
        "recognizer": record.get("recognizer"),
        "index": record.get("index"),
    }
    matches = [
        seed for seed in record.get("seeds", [])
        if int(seed.get("tile", -1)) == tile
    ]
    if not matches:
        return {
            "status": "missing-seed",
            "tile": tile,
            "review_required": True,
            "reason": "The index OCR did not produce a location seed for this tile.",
            "provenance": provenance,
        }
    if len(matches) != 1 or bool(matches[0].get("ambiguous")):
        return {
            "status": "ambiguous",
            "tile": tile,
            "review_required": True,
            "reason": "The index OCR has competing readings for this printed tile number.",
            "alternatives": matches,
            "provenance": provenance,
        }
    seed = dict(matches[0])
    if str(seed.get("crs", "")).upper() != "EPSG:3857":
        fail(f"Tile {tile}'s index seed is not in required EPSG:3857.")
    try:
        map_x = float(seed["map_x"])
        map_y = float(seed["map_y"])
        max_distance = min(
            float(seed["suggested_max_distance"]), MAX_TARGET_SEED_DISTANCE
        )
    except (KeyError, TypeError, ValueError):
        fail(f"Tile {tile}'s index seed is missing numeric map coordinates or tolerance.")
    if max_distance <= 0:
        fail(f"Tile {tile}'s suggested seed distance must be positive.")
    return {
        "status": "selected",
        "tile": tile,
        "review_required": seed.get("quality") != "high",
        "seed": seed,
        "map_x": map_x,
        "map_y": map_y,
        "suggested_max_distance": max_distance,
        "quality": seed.get("quality", "review"),
        "provenance": provenance,
    }


def store_tile_seed(
    db: sqlite3.Connection,
    tile: int,
    index_json: Path,
    *,
    missing_ok: bool = True,
) -> dict:
    row = db.execute("SELECT * FROM tiles WHERE tile=?", (tile,)).fetchone()
    if not row:
        fail(f"Tile {tile} is not in the queue.")
    context = tile_seed_context(index_json, tile, missing_ok=missing_ok)
    context_json = json.dumps(context, sort_keys=True)
    provenance_json = json.dumps(context.get("provenance", {}), sort_keys=True)
    selected = context["status"] == "selected"
    changed = (
        row["target_seed_json"] != context_json
        or row["target_seed_provenance_json"] != provenance_json
    )
    if changed and row["status"] in {"awaiting-approval", "approved", "verified"}:
        fail(
            f"Tile {tile}'s review is already locked at status {row['status']}; "
            "do not replace its index seed without rebuilding that review."
        )
    update_tile(
        db,
        tile,
        target_seed_x=context.get("map_x") if selected else None,
        target_seed_y=context.get("map_y") if selected else None,
        target_seed_max_distance=(
            context.get("suggested_max_distance") if selected else None
        ),
        target_seed_quality=context.get("quality") if selected else context["status"],
        target_seed_ambiguous=int(context["status"] == "ambiguous"),
        target_seed_json=context_json,
        target_seed_provenance_json=provenance_json,
    )
    if changed:
        event(db, tile, "target-seed-" + str(context["status"]), str(index_json))
        if row["status"] == "needs-chatgpt-review" and row["proposal_path"]:
            transition_tile(
                db,
                tile,
                "proposal-stale",
                detail="The independent index seed changed; local controls must be proposed again.",
            )
    db.commit()
    return context


def _target_seed_command_args(row: sqlite3.Row) -> list[str]:
    """Build the verified georeferencer flags only for a unique selected seed."""
    if row["target_seed_ambiguous"] or row["target_seed_x"] is None or row["target_seed_y"] is None:
        fail(f"Tile {row['tile']} has no unique independent index-map location seed.")
    distance = row["target_seed_max_distance"]
    if distance is None or float(distance) <= 0:
        fail(f"Tile {row['tile']} has a selected target seed without a positive tolerance.")
    seed_x = float(row["target_seed_x"])
    seed_y = float(row["target_seed_y"])
    tolerance = float(distance)
    return [
        "--expected-target-bbox",
        str(seed_x - tolerance),
        str(seed_y - tolerance),
        str(seed_x + tolerance),
        str(seed_y + tolerance),
        "--expected-target-seed",
        str(seed_x),
        str(seed_y),
        "--max-target-seed-distance",
        str(tolerance),
    ]


def _require_fresh_stored_seed(row: sqlite3.Row) -> dict:
    """Require the selected index seed and its source file to remain unchanged."""
    try:
        stored = json.loads(str(row["target_seed_json"] or ""))
        provenance = json.loads(str(row["target_seed_provenance_json"] or ""))
    except json.JSONDecodeError:
        fail(f"Tile {row['tile']} has no valid stored index-seed evidence.")
    if not isinstance(stored, dict) or stored.get("status") != "selected":
        fail(
            f"Tile {row['tile']} needs one unique index-map seed before a review "
            "packet or final warp can proceed."
        )
    try:
        selected_x = float(stored["map_x"])
        selected_y = float(stored["map_y"])
        selected_distance = float(stored["suggested_max_distance"])
        row_x = float(row["target_seed_x"])
        row_y = float(row["target_seed_y"])
        row_distance = float(row["target_seed_max_distance"])
    except (KeyError, TypeError, ValueError):
        fail(f"Tile {row['tile']}'s selected index seed is incomplete in the queue.")
    if (
        row["target_seed_ambiguous"] != 0
        or not all(
            math.isfinite(value)
            for value in (selected_x, selected_y, selected_distance, row_x, row_y, row_distance)
        )
        or not (0 < selected_distance <= MAX_TARGET_SEED_DISTANCE)
        or not math.isclose(row_x, selected_x, rel_tol=0.0, abs_tol=1e-9)
        or not math.isclose(row_y, selected_y, rel_tol=0.0, abs_tol=1e-9)
        or not math.isclose(row_distance, selected_distance, rel_tol=0.0, abs_tol=1e-9)
        or str(row["target_seed_quality"]) != str(stored.get("quality", "review"))
    ):
        fail(
            f"Tile {row['tile']}'s queue columns disagree with its selected index seed. "
            "Store the seed again before review or warping."
        )
    path_value = provenance.get("path") if isinstance(provenance, dict) else None
    expected_digest = provenance.get("sha256") if isinstance(provenance, dict) else None
    if not isinstance(path_value, str) or not isinstance(expected_digest, str):
        fail(f"Tile {row['tile']}'s stored index-seed provenance is incomplete.")
    if provenance != stored.get("provenance"):
        fail(f"Tile {row['tile']}'s stored index-seed provenance fields disagree.")
    index_path = Path(path_value).expanduser().resolve()
    if not index_path.is_file() or sha256(index_path) != expected_digest:
        fail(
            f"Tile {row['tile']}'s index-seed file changed after controls were prepared. "
            "Rebuild the proposal and review packet."
        )
    current = tile_seed_context(index_path, int(row["tile"]), missing_ok=False)
    if json.dumps(current, sort_keys=True) != json.dumps(stored, sort_keys=True):
        fail(
            f"Tile {row['tile']}'s selected index seed changed after controls were "
            "prepared. Rebuild the proposal and review packet."
        )
    return stored


def _proposal_input_token(row: sqlite3.Row, osm_db: Path, aliases: Path) -> str:
    if not osm_db.is_file():
        fail(f"Local OSM street index does not exist: {osm_db}")
    if not aliases.is_file():
        fail(f"Reviewed street-alias file does not exist: {aliases}")
    spatial_ocr = Path(str(row["spatial_ocr_path"] or ""))
    if not spatial_ocr.is_file():
        fail(f"Tile {row['tile']} has no usable spatial OCR record.")
    osm_stat = osm_db.stat()
    algorithm_files = [
        Path(__file__).with_name("sanborn_controls.py"),
        Path(__file__).with_name("sanborn_geometry.py"),
    ]
    ingredients = {
        "source_sha256": str(row["source_sha256"]),
        "spatial_ocr_sha256": sha256(spatial_ocr),
        "osm_database": str(osm_db.resolve()),
        "osm_bytes": osm_stat.st_size,
        "osm_modified_ns": osm_stat.st_mtime_ns,
        "aliases_sha256": sha256(aliases),
        "target_seed": row["target_seed_json"],
        "target_seed_provenance": row["target_seed_provenance_json"],
        "algorithm_sha256": {
            path.name: sha256(path) for path in algorithm_files if path.is_file()
        },
    }
    return hashlib.sha256(
        json.dumps(ingredients, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _proposal_evidence(record: dict, target_seed: dict | None = None) -> dict:
    return {
        "status": record.get("status"),
        "source_sha256": record.get("source", {}).get("sha256"),
        "osm_database": record.get("osm_database"),
        "aliases_path": record.get("aliases_path"),
        "target_seed": target_seed,
        "resolved_label_count": len(record.get("resolved_labels", [])),
        "street_axis_count": len(record.get("street_axes", [])),
        "shared_node_candidate_count": len(record.get("control_candidates", [])),
        "control_candidates": [
            {
                key: candidate.get(key)
                for key in (
                    "candidate_id",
                    "label",
                    "historic_street_a",
                    "historic_street_b",
                    "modern_street_a",
                    "modern_street_b",
                    "source_x",
                    "source_y",
                    "osm_node_id",
                    "target_x",
                    "target_y",
                    "pair_ambiguity_count",
                    "match_methods",
                )
            }
            for candidate in record.get("control_candidates", [])
        ],
        "ranked_triplets": [
            {
                "candidate_ids": triplet.get("candidate_ids", []),
                "score": triplet.get("score"),
                    "needs_chatgpt_review": triplet.get("needs_chatgpt_review", True),
                    "strict_safety_warnings": triplet.get("strict_safety_warnings", []),
                    "target_centroid": triplet.get("target_centroid"),
                    "target_seed_distance": triplet.get("target_seed_distance"),
            }
            for triplet in record.get("ranked_triplets", [])
        ],
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"Project: {PROJECT_ROOT}")
    print(f"Batch database: {args.database}")
    for program in (
        "python3",
        "gdal_translate",
        "gdalwarp",
        "gdalinfo",
        "gdal_edit.py",
        "tesseract",
        "clang",
    ):
        print(f"{program}: {require_program(program)}")
    try:
        import PIL
        print(f"Pillow: {PIL.__version__}")
    except ImportError:
        fail("Python Pillow is unavailable.")
    try:
        import cv2
        print(f"OpenCV: {cv2.__version__}")
    except ImportError:
        fail("Python OpenCV is unavailable; street-line geometry cannot run.")
    print(
        f"Local OSM street index: "
        f"{args.osm_db if args.osm_db.is_file() else 'not built yet (run osm-refresh or osm-import)'}"
    )
    usage = shutil.disk_usage(PROJECT_ROOT)
    print(f"Free disk space: {usage.free / (1024 ** 3):.1f} GB")
    print("Local worker is ready.")
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    with connect(args.database) as db:
        count = refresh_catalog(db)
    return 0 if count else 1


def cmd_add(args: argparse.Namespace) -> int:
    tiles = parse_tiles(args.tiles)
    if args.volume and not 1 <= args.volume <= 4:
        fail("--volume must be 1, 2, 3, or 4.")
    with connect(args.database) as db:
        for tile in tiles:
            url = deterministic_url(tile, args.volume) if args.volume else None
            db.execute(
                """
                INSERT INTO tiles(tile, status, volume, url, updated_utc)
                VALUES (?, 'queued', ?, ?, ?)
                ON CONFLICT(tile) DO UPDATE SET
                  volume=COALESCE(excluded.volume, tiles.volume),
                  url=COALESCE(excluded.url, tiles.url),
                  updated_utc=excluded.updated_utc
                """,
                (tile, args.volume, url, utc_now()),
            )
            event(db, tile, "queued")
        db.commit()
    print(f"Queued {len(tiles)} tile(s): {', '.join(map(str, tiles))}")
    return 0


@tile_locked
def prepare_one(db: sqlite3.Connection, tile: int) -> str:
    row = db.execute("SELECT * FROM tiles WHERE tile=?", (tile,)).fetchone()
    if not row:
        fail(f"Tile {tile} is not in the queue.")
    if row["status"] != "queued":
        return str(row["status"])
    transition_tile(
        db,
        tile,
        "downloading",
        detail="Starting local source verification and spatial OCR",
        error=None,
        failure_stage=None,
        attempt_count=int(row["attempt_count"] or 0) + 1,
    )
    db.commit()
    volume, url = resolve_tile(db, tile)
    destination = DOWNLOAD_DIR / source_name(tile)
    update_tile(db, tile, volume=volume, url=url)
    db.commit()
    print(f"Tile {tile}: Volume {volume}; downloading/verifying locally...")
    exists, expected_bytes = url_exists(url)
    if not exists:
        fail(f"The official LOC scan URL no longer exists: {url}")
    download(url, destination, expected_bytes)
    try:
        width, height = image_info(destination)
    except Exception:
        if destination.exists():
            quarantine(destination, "not a readable JPEG2000 scan")
        raise
    preview, spatial_ocr, seen = create_preview(tile, destination)
    digest = sha256(destination)
    spatial_digest = sha256(spatial_ocr)
    ready_status = "review-ready" if seen else "number-check-required"
    transition_tile(
        db,
        tile,
        ready_status,
        detail=f"{width}x{height}; sha256={digest}",
        source_path=str(destination),
        source_sha256=digest,
        source_bytes=destination.stat().st_size,
        width=width,
        height=height,
        preview_path=str(preview),
        # Keep the phase-one field populated for older reports while making its
        # spatial meaning explicit in the new columns.
        ocr_path=str(spatial_ocr),
        spatial_ocr_path=str(spatial_ocr),
        spatial_ocr_sha256=spatial_digest,
        printed_number_seen=int(seen),
        error=None,
        failure_stage=None,
    )
    db.commit()
    print(
        f"Tile {tile}: {width} x {height}; spatial OCR ready; "
        f"printed-number check={'passed' if seen else 'BLOCKED pending visual confirmation'}"
    )
    return ready_status


def cmd_prepare(args: argparse.Namespace) -> int:
    requested = parse_tiles(args.tiles) if args.tiles else []
    with connect(args.database) as db:
        if requested:
            placeholders = ",".join("?" for _ in requested)
            rows = db.execute(
                f"SELECT * FROM tiles WHERE tile IN ({placeholders}) ORDER BY tile",
                requested,
            ).fetchall()
            missing = set(requested) - {int(row["tile"]) for row in rows}
            if missing:
                fail(f"Queue these tiles first: {sorted(missing)}")
        else:
            rows = db.execute(
                "SELECT * FROM tiles WHERE status='queued' ORDER BY tile"
            ).fetchall()
        if args.limit:
            rows = rows[: args.limit]
        if not rows:
            print("No queued tiles need preparation.")
            return 0
        for index, row in enumerate(rows):
            tile = int(row["tile"])
            if row["status"] != "queued":
                print(
                    f"Tile {tile}: already {row['status']}; preparation skipped "
                    "(use reopen for an inspected retry)."
                )
                continue
            try:
                prepare_one(db, tile)
            except Exception as error:
                _mark_failed(db, tile, "prepare", error)
                db.commit()
                print(f"Tile {tile}: FAILED: {error}", file=sys.stderr)
                if not args.keep_going:
                    raise
            if index < len(rows) - 1:
                time.sleep(args.delay)
    return 0


@command_tile_locked
def cmd_confirm_number(args: argparse.Namespace) -> int:
    note = args.note.strip()
    if not note:
        fail("Describe where the printed tile number was visually confirmed.")
    with connect(args.database) as db:
        row = db.execute("SELECT * FROM tiles WHERE tile=?", (args.tile,)).fetchone()
        if not row or row["status"] != "number-check-required":
            fail(f"Tile {args.tile} is not waiting for a printed-number check.")
        transition_tile(
            db,
            args.tile,
            "review-ready",
            detail=note,
            printed_number_seen=1,
            error=None,
        )
        event(db, args.tile, "printed-number-confirmed", note)
        db.commit()
    print(f"Tile {args.tile}: printed number visually confirmed; review may proceed.")
    return 0


@tile_locked
def propose_one(
    db: sqlite3.Connection,
    tile: int,
    osm_db: Path,
    aliases: Path,
    *,
    index_json: Path = DEFAULT_INDEX_JSON,
) -> str:
    row = db.execute("SELECT * FROM tiles WHERE tile=?", (tile,)).fetchone()
    if not row:
        fail(f"Tile {tile} is not in the queue.")
    if row["status"] not in {
        "review-ready",
        "proposal-stale",
        "needs-chatgpt-review",
    }:
        return str(row["status"])
    if row["status"] == "needs-chatgpt-review":
        store_tile_seed(db, tile, index_json, missing_ok=True)
        row = db.execute("SELECT * FROM tiles WHERE tile=?", (tile,)).fetchone()
        if row["status"] == "needs-chatgpt-review":
            return "needs-chatgpt-review"
    spatial_ocr = _ensure_spatial_ocr(db, row)
    target_seed = store_tile_seed(db, tile, index_json, missing_ok=True)
    row = db.execute("SELECT * FROM tiles WHERE tile=?", (tile,)).fetchone()
    if row["printed_number_seen"] != 1:
        fail(f"Tile {tile} has not passed its printed-number check.")
    input_token = _proposal_input_token(row, osm_db, aliases)
    proposal_dir = BATCH_DIR / "proposals"
    proposal_dir.mkdir(parents=True, exist_ok=True)
    proposal = proposal_dir / f"tile-{tile:04d}.json"

    # Recover cleanly if the proposal file was completed just before a process
    # interruption but its final database transition was not committed.
    record = None
    if (
        proposal.is_file()
        and row["proposal_inputs_sha256"] == input_token
        and row["proposal_sha256"] == sha256(proposal)
    ):
        candidate = json.loads(proposal.read_text(encoding="utf-8"))
        if (
            int(candidate.get("tile", -1)) == tile
            and candidate.get("source", {}).get("sha256") == row["source_sha256"]
        ):
            record = candidate

    transition_tile(
        db,
        tile,
        "proposing",
        detail="Matching spatial street labels to exact local OSM shared nodes",
        error=None,
        failure_stage=None,
        attempt_count=int(row["attempt_count"] or 0) + 1,
    )
    db.commit()
    if record is None:
        command = [
            sys.executable,
            str(Path(__file__).with_name("sanborn_controls.py")),
            "propose",
            "--tile",
            str(tile),
            "--ocr",
            str(spatial_ocr),
            "--osm-db",
            str(osm_db),
            "--aliases",
            str(aliases),
            "--output",
            str(proposal),
        ]
        if target_seed["status"] == "selected":
            command.extend(
                [
                    "--expected-target-seed",
                    str(target_seed["map_x"]),
                    str(target_seed["map_y"]),
                    "--max-seed-distance",
                    str(target_seed["suggested_max_distance"]),
                ]
            )
        subprocess.run(command, check=True)
        record = json.loads(proposal.read_text(encoding="utf-8"))
    if int(record.get("tile", -1)) != tile:
        fail(f"Control proposal claims the wrong tile number: {proposal}")
    if record.get("source", {}).get("sha256") != row["source_sha256"]:
        fail(f"Control proposal does not match tile {tile}'s source scan.")
    if record.get("target_seed") != target_seed:
        record["target_seed"] = target_seed
        proposal.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    evidence = _proposal_evidence(record, target_seed)
    evidence_json = json.dumps(evidence, sort_keys=True)
    proposal_digest = sha256(proposal)
    transition_tile(
        db,
        tile,
        "needs-chatgpt-review",
        detail=(
            f"{evidence['shared_node_candidate_count']} shared-node candidates; "
            f"{len(evidence['ranked_triplets'])} ranked triplets"
        ),
        proposal_path=str(proposal),
        proposal_sha256=proposal_digest,
        proposal_inputs_sha256=input_token,
        proposal_evidence_json=evidence_json,
        proposal_corrections_json=row["proposal_corrections_json"] or "[]",
        proposal_rejections_json=row["proposal_rejections_json"] or "[]",
        error=None,
        failure_stage=None,
    )
    db.execute(
        """
        INSERT INTO proposal_history(
            tile, created_utc, proposal_path, proposal_sha256, input_sha256,
            evidence_json, corrections_json, rejections_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tile,
            utc_now(),
            str(proposal),
            proposal_digest,
            input_token,
            evidence_json,
            row["proposal_corrections_json"] or "[]",
            row["proposal_rejections_json"] or "[]",
        ),
    )
    db.commit()
    print(
        f"Tile {tile}: local proposal ready; ChatGPT or Joel must review it "
        "before any points can be approved."
    )
    return "needs-chatgpt-review"


def _selected_rows(
    db: sqlite3.Connection,
    tile_values: list[str],
    eligible: tuple[str, ...],
    limit: int | None,
) -> list[sqlite3.Row]:
    requested = parse_tiles(tile_values) if tile_values else []
    if requested:
        placeholders = ",".join("?" for _ in requested)
        rows = db.execute(
            f"SELECT * FROM tiles WHERE tile IN ({placeholders}) ORDER BY tile",
            requested,
        ).fetchall()
        missing = set(requested) - {int(row["tile"]) for row in rows}
        if missing:
            fail(f"Queue these tiles first: {sorted(missing)}")
    else:
        placeholders = ",".join("?" for _ in eligible)
        rows = db.execute(
            f"SELECT * FROM tiles WHERE status IN ({placeholders}) ORDER BY tile",
            eligible,
        ).fetchall()
    return rows[:limit] if limit else rows


def recover_interrupted_tile(db: sqlite3.Connection, tile: int) -> str:
    """Recover a claim left behind by a stopped process after its lock is released."""
    with tile_file_lock(tile):
        row = db.execute("SELECT * FROM tiles WHERE tile=?", (tile,)).fetchone()
        if not row:
            fail(f"Tile {tile} is not in the queue.")
        current = str(row["status"])
        if current == "downloading":
            destination = "queued"
        elif current == "proposing":
            source_ok = all(
                row[name]
                for name in ("source_path", "source_sha256", "spatial_ocr_path")
            ) and row["printed_number_seen"] == 1
            destination = "review-ready" if source_ok else "queued"
        else:
            return current
        update_tile(
            db,
            tile,
            status=destination,
            error=None,
            failure_stage=None,
        )
        event(
            db,
            tile,
            "interrupted-work-recovered",
            f"{current} -> {destination} after acquiring the abandoned tile lock",
        )
        db.commit()
        print(f"Tile {tile}: recovered interrupted {current} work to {destination}.")
        return destination


def cmd_propose(args: argparse.Namespace) -> int:
    with connect(args.database) as db:
        rows = _selected_rows(
            db,
            args.tiles,
            ("review-ready", "proposal-stale", "needs-chatgpt-review"),
            args.limit,
        )
        if not rows:
            print("No prepared tiles need a control proposal.")
            return 0
        failures = 0
        for row in rows:
            tile = int(row["tile"])
            if row["status"] not in {
                "review-ready",
                "proposal-stale",
                "needs-chatgpt-review",
            }:
                print(f"Tile {tile}: already {row['status']}; proposal skipped.")
                continue
            try:
                propose_one(
                    db,
                    tile,
                    args.osm_db,
                    args.aliases,
                    index_json=args.index_json,
                )
            except Exception as error:
                _mark_failed(db, tile, "propose", error)
                db.commit()
                failures += 1
                print(f"Tile {tile}: proposal FAILED: {error}", file=sys.stderr)
                if not args.keep_going:
                    raise
    return 1 if failures else 0


def cmd_work(args: argparse.Namespace) -> int:
    """Advance a range locally while containing every tile's failure."""
    failures = 0
    with connect(args.database) as db:
        rows = _selected_rows(
            db,
            args.tiles,
            (
                "queued",
                "downloading",
                "review-ready",
                "proposing",
                "proposal-stale",
                "needs-chatgpt-review",
            ),
            args.limit,
        )
        if not rows:
            print("No queued or prepared tiles need local work.")
            return 0
        for index, original in enumerate(rows):
            tile = int(original["tile"])
            stage = "inspect-state"
            try:
                row = db.execute("SELECT * FROM tiles WHERE tile=?", (tile,)).fetchone()
                if row["status"] in {"downloading", "proposing"}:
                    recover_interrupted_tile(db, tile)
                    row = db.execute(
                        "SELECT * FROM tiles WHERE tile=?", (tile,)
                    ).fetchone()
                if row["status"] == "queued":
                    stage = "prepare"
                    prepare_one(db, tile)
                    row = db.execute("SELECT * FROM tiles WHERE tile=?", (tile,)).fetchone()
                if row["status"] in {
                    "review-ready",
                    "proposal-stale",
                    "needs-chatgpt-review",
                }:
                    stage = "propose"
                    propose_one(
                        db,
                        tile,
                        args.osm_db,
                        args.aliases,
                        index_json=args.index_json,
                    )
                elif row["status"] not in {
                    "number-check-required",
                    "needs-chatgpt-review",
                    "proposal-rejected",
                    "awaiting-approval",
                    "approved",
                    "verified",
                }:
                    print(f"Tile {tile}: status {row['status']} requires an explicit reopen.")
            except Exception as error:
                _mark_failed(db, tile, stage, error)
                db.commit()
                failures += 1
                print(f"Tile {tile}: worker FAILED: {error}", file=sys.stderr)
            if index < len(rows) - 1 and args.delay:
                time.sleep(args.delay)
    if failures:
        print(
            f"Local worker completed the range with {failures} isolated failure(s); "
            "all other tiles continued."
        )
    return 1 if failures else 0


def cmd_osm_refresh(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(Path(__file__).with_name("sanborn_osm.py")),
        "refresh",
        "--cache",
        str(args.cache),
        "--db",
        str(args.osm_db),
    ]
    if args.bbox:
        command.extend(["--bbox", *map(str, args.bbox)])
    if args.endpoint:
        command.extend(["--endpoint", args.endpoint])
    if args.replace:
        command.append("--replace")
    subprocess.run(command, check=True)
    return 0


def cmd_osm_import(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(Path(__file__).with_name("sanborn_osm.py")),
        "import",
        str(args.source),
        "--db",
        str(args.osm_db),
    ]
    if args.bbox:
        command.extend(["--bbox", *map(str, args.bbox)])
    if args.replace:
        command.append("--replace")
    subprocess.run(command, check=True)
    return 0


def cmd_index_build(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(Path(__file__).with_name("sanborn_index.py")),
        "build",
        "--output",
        str(args.index_json),
        "--cache",
        str(args.index_cache),
        "--preview-width",
        str(args.preview_width),
        "--cluster-radius",
        str(args.cluster_radius),
        "--maximum-tile",
        str(args.maximum_tile),
    ]
    if args.index_raster is not None:
        command.extend(["--index", str(args.index_raster)])
    for crop_size in args.crop_size:
        command.extend(["--crop-size", str(crop_size)])
    if args.stride:
        command.extend(["--stride", str(args.stride)])
    subprocess.run(command, check=True)
    return 0


@command_tile_locked
def cmd_index_lookup(args: argparse.Namespace) -> int:
    with connect(args.database) as db:
        context = store_tile_seed(
            db,
            args.tile,
            args.index_json,
            missing_ok=False,
        )
    print(json.dumps(context, indent=2, sort_keys=True))
    if context["status"] != "selected":
        print(
            f"Tile {args.tile}: index location is {context['status']}; "
            "it remains reviewable and no coordinates were chosen."
        )
    return 0


@command_tile_locked
def cmd_confirm_seed(args: argparse.Namespace) -> int:
    """Record one reviewed index location without modifying the shared OCR index."""
    with connect(args.database) as db:
        row = db.execute("SELECT * FROM tiles WHERE tile=?", (args.tile,)).fetchone()
        if row and row["status"] in {"awaiting-approval", "approved", "verified"}:
            fail(
                f"Tile {args.tile} already has locked review evidence. Reopen and rebuild "
                "that review before changing its independent location seed."
            )
    command = [
        sys.executable,
        str(Path(__file__).with_name("sanborn_index.py")),
        "confirm",
        str(args.index_json),
        str(args.tile),
        "--index-raster",
        str(args.index_raster),
        "--reviewer",
        args.reviewer,
        "--note",
        args.note,
        "--tolerance",
        str(args.tolerance),
    ]
    if args.preview_pixel is not None:
        command.extend(["--preview-pixel", *map(str, args.preview_pixel)])
    if args.map_coordinate is not None:
        command.extend(["--map-coordinate", *map(str, args.map_coordinate)])
    if args.replace_existing:
        command.append("--replace-existing")
    if args.override_high_confidence:
        command.append("--override-high-confidence")
    subprocess.run(command, check=True)
    if row:
        with connect(args.database) as db:
            context = store_tile_seed(db, args.tile, args.index_json, missing_ok=False)
        print(json.dumps(context, indent=2, sort_keys=True))
    return 0


@command_tile_locked
def cmd_reopen(args: argparse.Namespace) -> int:
    note = args.note.strip()
    if not note:
        fail("Explain what was inspected or changed before reopening this tile.")
    with connect(args.database) as db:
        reopen_tile(db, args.tile, args.to, note)
        db.commit()
    print(f"Tile {args.tile}: reopened to {args.to}.")
    return 0


def _append_json_list(existing: object, records: list[dict]) -> str:
    try:
        values = json.loads(str(existing or "[]"))
    except json.JSONDecodeError:
        values = []
    if not isinstance(values, list):
        values = []
    values.extend(records)
    return json.dumps(values, sort_keys=True)


@command_tile_locked
def cmd_review_proposal(args: argparse.Namespace) -> int:
    if not args.correction and not args.reject:
        fail("Record at least one correction or a rejection reason.")
    with connect(args.database) as db:
        row = db.execute("SELECT * FROM tiles WHERE tile=?", (args.tile,)).fetchone()
        if not row or row["status"] != "needs-chatgpt-review":
            fail(f"Tile {args.tile} is not waiting for proposal review.")
        stamp = utc_now()
        corrections = [
            {"created_utc": stamp, "note": note.strip()}
            for note in args.correction
            if note.strip()
        ]
        rejections = (
            [{"created_utc": stamp, "reason": args.reject.strip()}]
            if args.reject and args.reject.strip()
            else []
        )
        if not corrections and not rejections:
            fail("Correction and rejection notes cannot be blank.")
        correction_json = _append_json_list(row["proposal_corrections_json"], corrections)
        rejection_json = _append_json_list(row["proposal_rejections_json"], rejections)
        update_tile(
            db,
            args.tile,
            proposal_corrections_json=correction_json,
            proposal_rejections_json=rejection_json,
        )
        if corrections:
            event(db, args.tile, "proposal-correction", json.dumps(corrections, sort_keys=True))
        if rejections:
            transition_tile(
                db,
                args.tile,
                "proposal-rejected",
                detail=args.reject.strip(),
            )
        history = db.execute(
            "SELECT id FROM proposal_history WHERE tile=? ORDER BY id DESC LIMIT 1",
            (args.tile,),
        ).fetchone()
        if history:
            db.execute(
                "UPDATE proposal_history SET corrections_json=?, rejections_json=? WHERE id=?",
                (correction_json, rejection_json, history["id"]),
            )
        db.commit()
    print(f"Tile {args.tile}: proposal review notes recorded; no approval was granted.")
    return 0


@command_tile_locked
def cmd_packet(args: argparse.Namespace) -> int:
    tile = args.tile
    labels = validate_control_labels(args.control_label)
    with connect(args.database) as db:
        row = db.execute("SELECT * FROM tiles WHERE tile=?", (tile,)).fetchone()
        if not row or not row["source_path"]:
            fail(f"Tile {tile} has not been prepared.")
        if row["status"] not in {"review-ready", "needs-chatgpt-review"}:
            fail(
                f"Tile {tile} is {row['status']}; a review packet can only be built "
                "from a prepared or locally proposed tile."
            )
        if row["printed_number_seen"] != 1:
            fail(
                f"Tile {tile} has not passed its printed-number check. Inspect its preview, "
                "then use confirm-number before building controls."
            )
        source = Path(row["source_path"])
        if not source.is_file() or sha256(source) != row["source_sha256"]:
            fail(
                f"Tile {tile}'s prepared source scan changed after its printed-number "
                "identity check. Reopen it to queued and verify the replacement."
            )
        store_tile_seed(db, tile, args.index_json, missing_ok=False)
        row = db.execute("SELECT * FROM tiles WHERE tile=?", (tile,)).fetchone()
        if row["status"] not in {"review-ready", "needs-chatgpt-review"}:
            fail(
                f"Tile {tile}'s index seed changed and invalidated its proposal. "
                "Rebuild the local proposal before making a packet."
            )
        seed_context = _require_fresh_stored_seed(row)
        points = args.points.expanduser().resolve()
        if not points.is_file():
            fail(f"Tile {tile}'s proposed control file does not exist: {points}")
        packet_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        review_dir = (
            BATCH_DIR / "reviews" / f"tile-{tile:04d}" / f"packet-{packet_stamp}"
        )
        review_dir.mkdir(parents=True, exist_ok=False)
        immutable_points = review_dir / "approved-controls.points"
        shutil.copy2(points, immutable_points)
        if sha256(immutable_points) != sha256(points):
            fail(f"Tile {tile}'s immutable review control copy did not verify.")
        command = [
            sys.executable,
            str(Path(__file__).with_name("sanborn_review.py")),
            "create",
            "--source",
            str(source),
            "--points",
            str(immutable_points),
            "--review-dir",
            str(review_dir),
            # This packet folder is brand new and already holds the immutable
            # control copy above, so the generator's empty-folder guard would
            # otherwise reject the very file we just placed for it.
            "--replace",
            "--target-seed-json",
            json.dumps(seed_context, sort_keys=True),
        ]
        packet_osm = args.osm_db
        if packet_osm is None and row["proposal_evidence_json"]:
            try:
                evidence = json.loads(row["proposal_evidence_json"])
                recorded_osm = evidence.get("osm_database")
                if recorded_osm:
                    packet_osm = Path(str(recorded_osm)).expanduser().resolve()
            except json.JSONDecodeError:
                fail(f"Tile {tile}'s stored proposal evidence is not valid JSON.")
        if packet_osm is not None:
            command.extend(["--osm-db", str(packet_osm)])
        if args.kauffman_map is not None:
            command.extend(["--kauffman-map", str(args.kauffman_map)])
        historical_evidence = getattr(args, "historical_evidence", None)
        if historical_evidence:
            immutable_evidence = review_dir / "historical-street-evidence.json"
            shutil.copy2(historical_evidence, immutable_evidence)
            if sha256(immutable_evidence) != sha256(historical_evidence):
                fail("The historical street evidence copy did not verify.")
            command.extend(["--historical-evidence", str(immutable_evidence)])
        if args.allow_distortion:
            command.extend(
                ["--allow-distortion", "--distortion-note", args.distortion_note]
            )
        for label in labels:
            command.extend(["--control-label", label])
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError:
            # A half-built packet is never usable and is invisible to the app,
            # so remove it instead of leaving it to accumulate on every retry.
            shutil.rmtree(review_dir, ignore_errors=True)
            raise
        transition_tile(
            db,
            tile,
            "awaiting-approval",
            detail=str(review_dir),
            points_path=str(immutable_points),
            review_dir=str(review_dir),
            error=None,
        )
        db.commit()
    return 0


@command_tile_locked
def cmd_approve(args: argparse.Namespace) -> int:
    with connect(args.database) as db:
        row = db.execute("SELECT * FROM tiles WHERE tile=?", (args.tile,)).fetchone()
        if not row or not row["review_dir"]:
            fail(f"Tile {args.tile} has no review packet.")
        if row["status"] != "awaiting-approval":
            fail(f"Tile {args.tile} is not waiting for approval.")
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("sanborn_review.py")),
                "approve",
                "--review-dir",
                row["review_dir"],
                "--approved-by",
                args.approved_by,
                "--note",
                args.note,
                "--reference-method",
                args.reference_method,
                "--verification-note",
                args.verification_note,
            ],
            check=True,
        )
        transition_tile(
            db,
            args.tile,
            "approved",
            detail=args.note,
            error=None,
        )
        db.commit()
    return 0


def _same_float_list(actual: object, expected: list[float]) -> bool:
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    try:
        return all(
            abs(float(value) - target) <= 1e-6
            for value, target in zip(actual, expected)
        )
    except (TypeError, ValueError):
        return False


def _same_structured_value(actual: object, expected: object) -> bool:
    """Compare recomputed JSON-like evidence with stable float tolerance."""
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and set(actual) == set(expected)
            and all(_same_structured_value(actual[key], value) for key, value in expected.items())
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_same_structured_value(left, right) for left, right in zip(actual, expected))
        )
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False
        return math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-7)
    return actual == expected


def _inspect_source_raster(source: Path) -> dict:
    """Read the live source dimensions used to recompute affine safety."""
    environment = dict(os.environ)
    environment["GDAL_PAM_ENABLED"] = "NO"
    result = subprocess.run(
        [require_program("gdalinfo"), "-json", str(source)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    return json.loads(result.stdout)


def _inspect_final_geotiff(output: Path) -> dict:
    """Read current raster structure without creating GDAL sidecar files."""
    environment = dict(os.environ)
    environment["GDAL_PAM_ENABLED"] = "NO"
    result = subprocess.run(
        [require_program("gdalinfo"), "-json", "-checksum", "-stats", str(output)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    return json.loads(result.stdout)


def _verify_final_pair(
    row: sqlite3.Row,
    output: Path,
    ledger: Path,
    approved_review: dict | None = None,
) -> dict:
    """Recompute the approved affine and verify the live raster, not ledger claims."""
    if not output.is_file() or not ledger.is_file():
        fail("The final GeoTIFF and verification ledger must both exist.")
    try:
        record = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"The final verification ledger is unreadable: {error}")
    if record.get("schema_version") != 1:
        fail("The final verification ledger uses an unsupported schema.")

    source = Path(str(row["source_path"])).expanduser().resolve()
    points = Path(str(row["points_path"])).expanduser().resolve()
    if not source.is_file() or not points.is_file():
        fail("The source scan and approved control file must still exist.")
    source_digest = sha256(source)
    points_digest = sha256(points)
    if source_digest != row["source_sha256"]:
        fail(
            "The source scan changed after its printed-number identity check; "
            "reopen this tile to queued."
        )
    source_record = record.get("source", {})
    points_record = record.get("points", {})
    if Path(str(source_record.get("path", ""))).resolve() != source:
        fail("The final ledger names a different source scan.")
    if source_record.get("sha256") != source_digest:
        fail("The final output was made from a different source scan.")
    if Path(str(points_record.get("path", ""))).resolve() != points:
        fail("The final ledger names a different control file.")
    if points_record.get("sha256") != points_digest:
        fail("The final output was made from different controls.")

    source_info = _inspect_source_raster(source)
    live_source_size = source_info.get("size")
    if (
        not isinstance(live_source_size, list)
        or len(live_source_size) != 2
        or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in live_source_size)
    ):
        fail("The source scan no longer reports valid raster dimensions.")
    source_width, source_height = live_source_size
    if [source_record.get("width"), source_record.get("height")] != live_source_size:
        fail("The source dimensions differ from the final ledger.")

    target_crs, controls = read_points(points)
    require_expected_crs(target_crs, "EPSG:3857")
    for control in controls:
        x = float(control["source_x"])
        line = float(control["source_line_gdal"])
        if not (0 <= x < source_width and 0 <= line < source_height):
            fail("An approved control now lies outside the source scan.")
    recorded_controls = points_record.get("controls")
    if not isinstance(recorded_controls, list) or len(recorded_controls) != 3:
        fail("The final ledger does not record exactly three controls.")
    labels = validate_control_labels([control.get("label", "") for control in recorded_controls])
    numeric_keys = ("map_x", "map_y", "source_x", "source_y_qgis", "source_line_gdal")
    for live, stored in zip(controls, recorded_controls):
        if not isinstance(stored, dict) or not all(
            _same_structured_value(stored.get(key), live.get(key)) for key in numeric_keys
        ):
            fail("The final ledger control coordinates differ from the live points file.")
    if approved_review is not None:
        approved_controls = approved_review.get("points", {}).get("controls")
        if not isinstance(approved_controls, list) or len(approved_controls) != 3:
            fail("The approved review packet does not contain exactly three controls.")
        for stored, approved in zip(recorded_controls, approved_controls):
            if stored.get("label") != approved.get("label") or not all(
                _same_structured_value(stored.get(key), approved.get(key)) for key in numeric_keys
            ):
                fail("The final controls differ from the approved review packet.")

    if row["target_seed_ambiguous"] or row["target_seed_x"] is None or row["target_seed_y"] is None:
        fail("A final output requires one unique independent index-map location seed.")
    seed_x = float(row["target_seed_x"])
    seed_y = float(row["target_seed_y"])
    distance = float(row["target_seed_max_distance"] or 0)
    if not (0 < distance <= MAX_TARGET_SEED_DISTANCE):
        fail("The current index-map location seed has an invalid tolerance.")
    expected_bbox = [
        seed_x - distance,
        seed_y - distance,
        seed_x + distance,
        seed_y + distance,
    ]
    expected_limits = {
        "max_scale_ratio": 1.15,
        "axis_angle_degrees": [85.0, 95.0],
        "min_triangle_coverage": 0.02,
        "min_x_span_fraction": 0.20,
        "min_y_span_fraction": 0.20,
        "expected_crs": "EPSG:3857",
        "expected_target_bbox": expected_bbox,
        "expected_target_seed": [seed_x, seed_y],
        "max_target_seed_distance": distance,
    }
    transformation = record.get("transformation", {})
    if str(transformation.get("target_crs", "")).upper() != "EPSG:3857":
        fail("The final output is not in required EPSG:3857.")
    if not _same_structured_value(transformation.get("safety_limits"), expected_limits):
        fail("The final ledger's affine safety limits differ from the required index-seed gates.")

    diagnostics = affine_diagnostics(controls, source_width, source_height)
    location = target_location_diagnostics(
        controls,
        diagnostics,
        source_width,
        source_height,
        expected_target_bbox=expected_bbox,
        expected_target_seed=[seed_x, seed_y],
        max_target_seed_distance=distance,
    )
    diagnostics["target_location_check"] = location
    require_target_location(location)
    warnings = affine_safety_warnings(diagnostics)
    distortion_warnings, hard_warnings = split_affine_safety_warnings(warnings)
    if hard_warnings:
        fail("Final affine hard safety gate failed: " + "; ".join(hard_warnings))
    distortion_override = transformation.get("distortion_override") is True
    if distortion_warnings and not distortion_override:
        fail("The final affine has unapproved distortion: " + "; ".join(distortion_warnings))
    if distortion_override and not distortion_warnings:
        fail("The final ledger claims a distortion exception that the live controls do not need.")
    if transformation.get("warnings") != warnings:
        fail("The final ledger's affine warnings differ from the live controls.")
    if not _same_structured_value(transformation.get("diagnostics"), diagnostics):
        fail("The final ledger's affine diagnostics differ from the live controls.")
    affine_signature = affine_provenance_signature(
        source_digest,
        points_digest,
        target_crs,
        diagnostics,
        expected_limits,
    )
    if transformation.get("affine_provenance_signature") != affine_signature:
        fail("The final ledger is not bound to the recomputed affine transform.")

    output_record = record.get("output", {})
    if Path(str(output_record.get("path", ""))).resolve() != output.resolve():
        fail("The final ledger names a different GeoTIFF path.")
    output_digest = sha256(output)
    if output_record.get("sha256") != output_digest:
        fail("The final GeoTIFF no longer matches its verification ledger.")
    if int(output_record.get("bytes", -1)) != output.stat().st_size:
        fail("The final GeoTIFF byte length no longer matches its verification ledger.")
    if output_record.get("bands") != ["Red", "Green", "Blue", "Alpha"]:
        fail("The final output does not have the required RGBA bands.")
    checksums = output_record.get("band_checksums")
    if (
        not isinstance(checksums, list)
        or len(checksums) != 4
        or any(not isinstance(value, int) or isinstance(value, bool) for value in checksums)
        or not any(checksums[:3])
        or checksums[3] == 0
    ):
        fail("The final ledger does not contain valid four-band pixel checksums.")
    if output_record.get("alpha_max") != 255:
        fail("The final ledger does not prove fully opaque map pixels in the alpha band.")
    if output_record.get("compression") != "DEFLATE" or output_record.get("predictor") != 2:
        fail("The final output lacks the required lossless compression settings.")

    live_info = _inspect_final_geotiff(output)
    if live_info.get("driverShortName") != "GTiff":
        fail("The final output is not currently readable as a GeoTIFF.")
    live_size = live_info.get("size")
    if live_size != [output_record.get("width"), output_record.get("height")]:
        fail("The final GeoTIFF dimensions differ from its verification ledger.")
    live_bands = live_info.get("bands", [])
    live_interpretations = [band.get("colorInterpretation") for band in live_bands]
    if live_interpretations != ["Red", "Green", "Blue", "Alpha"]:
        fail("The final GeoTIFF no longer reports true RGBA bands.")
    live_checksums = [int(band.get("checksum", -1)) for band in live_bands]
    if live_checksums != checksums:
        fail("The final GeoTIFF pixel checksums differ from its verification ledger.")
    try:
        live_alpha_min = float(live_bands[3]["minimum"])
        live_alpha_max = float(live_bands[3]["maximum"])
        ledger_alpha_min = float(output_record["alpha_min"])
        ledger_alpha_max = float(output_record["alpha_max"])
    except (IndexError, KeyError, TypeError, ValueError):
        fail("The final GeoTIFF does not report live alpha-band statistics.")
    if live_alpha_max != 255 or not math.isclose(live_alpha_max, ledger_alpha_max):
        fail("The live alpha band does not contain the required fully opaque map pixels.")
    if not math.isclose(live_alpha_min, ledger_alpha_min):
        fail("The live alpha-band minimum differs from its verification ledger.")
    metadata = live_info.get("metadata", {})
    image_structure = metadata.get("IMAGE_STRUCTURE", {})
    if image_structure.get("COMPRESSION") != "DEFLATE" or image_structure.get("PREDICTOR") != "2":
        fail("The final GeoTIFF no longer uses required lossless compression.")
    embedded = metadata.get("", {})
    expected_embedded = {
        SOURCE_METADATA_KEY: source_digest,
        POINTS_METADATA_KEY: points_digest,
        AFFINE_METADATA_KEY: affine_signature,
        PIPELINE_METADATA_KEY: PIPELINE_METADATA_VALUE,
    }
    if any(embedded.get(key) != value for key, value in expected_embedded.items()):
        fail("The live GeoTIFF is not internally bound to this source, controls, and affine.")
    wkt = str(live_info.get("coordinateSystem", {}).get("wkt", ""))
    if 'ID["EPSG",3857]' not in wkt:
        fail("The final GeoTIFF does not currently report EPSG:3857.")

    transform = live_info.get("geoTransform")
    if (
        not isinstance(transform, list)
        or len(transform) != 6
        or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in transform)
        or float(transform[1]) <= 0
        or float(transform[5]) >= 0
        or abs(float(transform[2])) > 1e-12
        or abs(float(transform[4])) > 1e-12
    ):
        fail("The final GeoTIFF does not have the expected north-up affine geotransform.")
    width, height = (int(value) for value in live_size)
    raster_bbox = [
        float(transform[0]),
        float(transform[3]) + height * float(transform[5]),
        float(transform[0]) + width * float(transform[1]),
        float(transform[3]),
    ]
    footprint_bbox = diagnostics["target_location_check"]["transformed_source_footprint_bbox"]
    pixel_tolerance = max(abs(float(transform[1])), abs(float(transform[5]))) * 1.1 + 1e-6
    if any(
        abs(float(actual) - float(expected)) > pixel_tolerance
        for actual, expected in zip(raster_bbox, footprint_bbox)
    ):
        fail("The final GeoTIFF extent does not match the approved affine footprint.")

    protected = record.get("protected_project", {})
    if Path(str(protected.get("path", ""))).resolve() != PROTECTED_PROJECT.resolve():
        fail("The final ledger does not identify the protected QGIS project.")
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
        fail("The final ledger does not prove the protected QGIS project stayed unchanged.")
    return record


def _preserve_incomplete_pair(output: Path, ledger: Path) -> None:
    """Move an interrupted one-file publish aside without deleting evidence."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for path in (output, ledger):
        if not path.exists():
            continue
        preserved = path.with_name(f"{path.stem}.incomplete-{stamp}{path.suffix}")
        counter = 1
        while preserved.exists():
            preserved = path.with_name(
                f"{path.stem}.incomplete-{stamp}-{counter}{path.suffix}"
            )
            counter += 1
        path.replace(preserved)
        print(f"Preserved interrupted publish for inspection: {preserved}")


def _ledger_predates_approval(ledger: Path, points: Path, source: Path) -> bool:
    """Report whether a finished map was made from superseded inputs.

    An unreadable ledger counts as superseded: it cannot prove which controls
    produced the map beside it, so the map is rebuilt rather than trusted.
    """
    try:
        record = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(record, dict):
        return True
    recorded_points = record.get("points", {})
    recorded_source = record.get("source", {})
    if not isinstance(recorded_points, dict) or not isinstance(recorded_source, dict):
        return True
    try:
        same_points = Path(str(recorded_points.get("path", ""))).resolve() == points
        same_source = Path(str(recorded_source.get("path", ""))).resolve() == source
    except OSError:
        return True
    if not same_points or not same_source:
        return True
    return recorded_points.get("sha256") != sha256(points)


@command_tile_locked
def cmd_finish(args: argparse.Namespace) -> int:
    with connect(args.database) as db:
        row = db.execute("SELECT * FROM tiles WHERE tile=?", (args.tile,)).fetchone()
        if not row or row["status"] != "approved":
            fail(f"Tile {args.tile} must have an approved review packet before final warping.")
        review_dir = Path(row["review_dir"])
        seed_context = _require_fresh_stored_seed(row)
        review, approval = require_approval(review_dir)
        output = DOWNLOAD_DIR / output_name(args.tile)
        ledger = output.with_suffix(".georef.json")
        source = Path(str(row["source_path"])).resolve()
        points = Path(str(row["points_path"])).resolve()
        if sha256(source) != row["source_sha256"]:
            fail(
                "The source scan changed after its printed-number identity check; "
                "reopen this tile to queued before review or warping."
            )
        if Path(str(review["source"]["path"])).resolve() != source:
            fail("The approved review packet names a different source scan.")
        if Path(str(review["points"]["path"])).resolve() != points:
            fail("The approved review packet names a different control file.")
        if review["source"]["sha256"] != sha256(source):
            fail("The approved source scan changed after review.")
        if review["points"]["sha256"] != sha256(points):
            fail("The approved controls changed after review.")
        review_limits = review.get("safety_limits", {})
        if review_limits.get("target_seed_context") != seed_context:
            fail(
                "The approved review packet is not bound to the current independent "
                "index-map seed. Rebuild and approve the packet."
            )
        allow_distortion = review_limits.get("allow_distortion") is True
        distortion_note = str(review_limits.get("distortion_note", "")).strip()
        distortion_warnings = review_limits.get("distortion_warnings", [])
        if allow_distortion and (not distortion_note or not distortion_warnings):
            fail("The approved distortion exception is missing its note or warnings.")
        quality_note = args.quality_note or (
            f"{approval['geographic_verification']['reference_method']}: "
            f"{approval['geographic_verification']['note']}"
        )
        if allow_distortion:
            quality_note += f" Distortion exception: {distortion_note}"
        command = [
            sys.executable,
            str(Path(__file__).with_name("sanborn_georeference.py")),
            "--source",
            row["source_path"],
            "--points",
            row["points_path"],
            "--output",
            str(output),
            "--protected-project",
            str(PROTECTED_PROJECT),
            "--quality-note",
            quality_note,
        ]
        command.extend(_target_seed_command_args(row))
        if allow_distortion:
            command.append("--allow-distortion")
        for control in review["points"]["controls"]:
            command.extend(["--control-label", control.get("label", "Control")])
        if output.exists() != ledger.exists():
            _preserve_incomplete_pair(output, ledger)
        if output.exists() and ledger.exists() and _ledger_predates_approval(ledger, points, source):
            # An earlier finished map for this sheet was made from controls the
            # current approval has replaced. Resuming would verify the old map
            # against the new approval and stop with a confusing complaint, so
            # set the superseded pair aside and warp again from what was approved.
            print("An earlier finished map for this sheet used different controls.")
            _preserve_incomplete_pair(output, ledger)
        if output.exists() and ledger.exists():
            final_record = _verify_final_pair(row, output, ledger, approved_review=review)
            print("Final GeoTIFF and ledger already verify; resuming after the completed warp.")
        else:
            subprocess.run(command, check=True)
            final_record = _verify_final_pair(row, output, ledger, approved_review=review)
        if bool(final_record["transformation"].get("distortion_override")) != allow_distortion:
            fail("The final warp's distortion setting differs from the approved review packet.")
        if allow_distortion and distortion_note not in str(final_record.get("quality_note", "")):
            fail("The final ledger does not carry the approved distortion explanation.")
        # Close the gap between the pre-warp approval check and final publish.
        # Any source, points, reference, artifact, or token change during a long
        # warp invalidates approval before a manifest or verified state exists.
        final_review, final_approval = require_approval(review_dir)
        final_seed_context = _require_fresh_stored_seed(row)
        if final_review.get("safety_limits", {}).get("target_seed_context") != final_seed_context:
            fail("The independent index-map seed changed during final warping.")
        if final_review["approval_token"] != review["approval_token"]:
            fail("The approved review token changed during final warping.")
        if final_record["source"]["sha256"] != final_review["source"]["sha256"]:
            fail("The final ledger source hash differs from the approved review packet.")
        if final_record["points"]["sha256"] != final_review["points"]["sha256"]:
            fail("The final ledger control hash differs from the approved review packet.")
        approval = final_approval
        manifest_dir = BATCH_DIR / "qgis-import"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest = manifest_dir / f"tile-{args.tile:04d}.json"
        geographic_verification = dict(approval["geographic_verification"])
        if geographic_verification.get("reference_method") == "local-osm-and-kauffman-packet":
            geographic_verification["reference_method"] = "local-osm-and-kauffman"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "tile": args.tile,
                    "path": str(output),
                    "raster_sha256": final_record["output"]["sha256"],
                    "ledger_path": str(ledger),
                    "ledger_sha256": sha256(ledger),
                    "source_sha256": final_record["source"]["sha256"],
                    "points_sha256": final_record["points"]["sha256"],
                    "review_dir": str(review_dir.resolve()),
                    "review_sha256": sha256(review_dir / "review.json"),
                    "approval_sha256": sha256(review_dir / "approval.json"),
                    "group": "1911 ATLANTA SANBORNS",
                    "sort_key": args.tile,
                    "brightness": 0,
                    "gamma": 1.0,
                    "contrast": 0,
                    "opacity": 1.0,
                    "expanded": False,
                    "save_project": False,
                    "review_token": review["approval_token"],
                    "geographic_verification": geographic_verification,
                    "target_seed": (
                        json.loads(row["target_seed_json"])
                        if row["target_seed_json"] else None
                    ),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        validate_qgis_manifest(manifest)
        transition_tile(
            db,
            args.tile,
            "verified",
            detail=str(manifest),
            output_path=str(output),
            error=None,
        )
        db.commit()
        print(f"Verified tile {args.tile}; QGIS import manifest: {manifest}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with connect(args.database) as db:
        rows = db.execute("SELECT * FROM tiles ORDER BY tile").fetchall()
    if not rows:
        print("Queue is empty.")
        return 0
    print("TILE  STATUS             VOL  SIZE        OCR  ERROR")
    for row in rows:
        size = f"{row['width']}x{row['height']}" if row["width"] else "-"
        ocr = (
            "yes"
            if row["printed_number_seen"] == 1
            else "check"
            if row["printed_number_seen"] == 0
            else "-"
        )
        print(
            f"{row['tile']:>4}  {row['status']:<17} "
            f"{str(row['volume'] or '-'):>3}  {size:<10}  {ocr:<5} "
            f"{row['error'] or ''}"
        )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="Verify local dependencies and free space")
    doctor.add_argument("--osm-db", type=Path, default=DEFAULT_OSM_DB)
    doctor.set_defaults(function=cmd_doctor)
    catalog = sub.add_parser("catalog", help="Cache all four official LOC volume catalogs")
    catalog.set_defaults(function=cmd_catalog)
    add = sub.add_parser("add", help="Queue tile numbers or ranges")
    add.add_argument("tiles", nargs="+")
    add.add_argument("--volume", type=int, help="Known LOC volume for all supplied tiles")
    add.set_defaults(function=cmd_add)
    prepare = sub.add_parser("prepare", help="Download, verify, preview, and OCR queued tiles")
    prepare.add_argument("tiles", nargs="*")
    prepare.add_argument("--limit", type=int)
    prepare.add_argument("--delay", type=float, default=2.0)
    prepare.add_argument("--keep-going", action="store_true")
    prepare.set_defaults(function=cmd_prepare)
    propose = sub.add_parser(
        "propose", help="Match prepared tiles to exact nodes in the local OSM street index"
    )
    propose.add_argument("tiles", nargs="*")
    propose.add_argument("--limit", type=int)
    propose.add_argument("--osm-db", type=Path, default=DEFAULT_OSM_DB)
    propose.add_argument("--aliases", type=Path, default=DEFAULT_ALIASES)
    propose.add_argument("--index-json", type=Path, default=DEFAULT_INDEX_JSON)
    propose.add_argument("--keep-going", action="store_true")
    propose.set_defaults(function=cmd_propose)
    work = sub.add_parser(
        "work",
        help="Run safe local preparation and proposals for a tile range",
    )
    work.add_argument("tiles", nargs="*")
    work.add_argument("--limit", type=int)
    work.add_argument("--delay", type=float, default=2.0)
    work.add_argument("--osm-db", type=Path, default=DEFAULT_OSM_DB)
    work.add_argument("--aliases", type=Path, default=DEFAULT_ALIASES)
    work.add_argument("--index-json", type=Path, default=DEFAULT_INDEX_JSON)
    work.set_defaults(function=cmd_work)
    osm_refresh = sub.add_parser(
        "osm-refresh", help="Download a bounded OSM extract and rebuild the local street index"
    )
    osm_refresh.add_argument("--cache", type=Path, default=DEFAULT_OSM_CACHE)
    osm_refresh.add_argument("--osm-db", type=Path, default=DEFAULT_OSM_DB)
    osm_refresh.add_argument("--bbox", nargs=4, type=float)
    osm_refresh.add_argument("--endpoint")
    osm_refresh.add_argument("--replace", action="store_true")
    osm_refresh.set_defaults(function=cmd_osm_refresh)
    osm_import = sub.add_parser(
        "osm-import", help="Build the local street index from an existing OSM or PBF file"
    )
    osm_import.add_argument("source", type=Path)
    osm_import.add_argument("--osm-db", type=Path, default=DEFAULT_OSM_DB)
    osm_import.add_argument("--bbox", nargs=4, type=float)
    osm_import.add_argument("--replace", action="store_true")
    osm_import.set_defaults(function=cmd_osm_import)
    index_build = sub.add_parser(
        "index-build",
        help="Read approximate tile-location safeguards from the local georeferenced index",
    )
    index_build.add_argument("--index-raster", type=Path)
    index_build.add_argument("--index-json", type=Path, default=DEFAULT_INDEX_JSON)
    index_build.add_argument("--index-cache", type=Path, default=DEFAULT_INDEX_CACHE)
    index_build.add_argument("--preview-width", type=int, default=3000)
    index_build.add_argument("--crop-size", type=int, action="append", default=[])
    index_build.add_argument("--stride", type=int, default=0)
    index_build.add_argument("--cluster-radius", type=float, default=55.0)
    index_build.add_argument("--maximum-tile", type=int, default=549)
    index_build.set_defaults(function=cmd_index_build)
    index_lookup = sub.add_parser(
        "index-lookup",
        help="Store one tile's independent index-map location safeguard",
    )
    index_lookup.add_argument("tile", type=int)
    index_lookup.add_argument("--index-json", type=Path, default=DEFAULT_INDEX_JSON)
    index_lookup.set_defaults(function=cmd_index_lookup)
    confirm_seed = sub.add_parser(
        "confirm-seed",
        help="Steer one missing or ambiguous tile location using the georeferenced index",
    )
    confirm_seed.add_argument("tile", type=int)
    confirm_seed.add_argument("--index-json", type=Path, default=DEFAULT_INDEX_JSON)
    confirm_seed.add_argument("--index-raster", type=Path, default=DEFAULT_INDEX_RASTER)
    coordinates = confirm_seed.add_mutually_exclusive_group(required=True)
    coordinates.add_argument("--preview-pixel", nargs=2, type=float, metavar=("X", "Y"))
    coordinates.add_argument("--map-coordinate", nargs=2, type=float, metavar=("X", "Y"))
    confirm_seed.add_argument("--reviewer", required=True)
    confirm_seed.add_argument("--note", required=True)
    confirm_seed.add_argument("--tolerance", type=float, default=250.0)
    confirm_seed.add_argument("--replace-existing", action="store_true")
    confirm_seed.add_argument("--override-high-confidence", action="store_true")
    confirm_seed.set_defaults(function=cmd_confirm_seed)
    reopen = sub.add_parser(
        "reopen", help="Explicitly release a stopped tile after inspecting its failure"
    )
    reopen.add_argument("tile", type=int)
    reopen.add_argument(
        "--to", choices=("queued", "review-ready", "approved"), default="queued"
    )
    reopen.add_argument("--note", required=True)
    reopen.set_defaults(function=cmd_reopen)
    review_proposal = sub.add_parser(
        "review-proposal",
        help="Record corrections or reject a local proposal without approving it",
    )
    review_proposal.add_argument("tile", type=int)
    review_proposal.add_argument("--correction", action="append", default=[])
    review_proposal.add_argument("--reject")
    review_proposal.set_defaults(function=cmd_review_proposal)
    confirm_number = sub.add_parser(
        "confirm-number", help="Record a visual tile-number check when OCR is uncertain"
    )
    confirm_number.add_argument("tile", type=int)
    confirm_number.add_argument("--note", required=True)
    confirm_number.set_defaults(function=cmd_confirm_number)
    packet = sub.add_parser("packet", help="Build the compact approval packet")
    packet.add_argument("tile", type=int)
    packet.add_argument("--points", required=True, type=Path)
    packet.add_argument(
        "--osm-db",
        type=Path,
        help="Local OSM index; defaults to the exact index recorded by propose",
    )
    packet.add_argument("--kauffman-map", type=Path)
    packet.add_argument("--historical-evidence", type=Path)
    packet.add_argument("--index-json", type=Path, default=DEFAULT_INDEX_JSON)
    packet.add_argument("--control-label", action="append", default=[])
    packet.add_argument(
        "--allow-distortion",
        action="store_true",
        help="Allow a documented historic-sheet distortion in the hash-locked packet",
    )
    packet.add_argument(
        "--distortion-note",
        default="",
        help="Required evidence note when --allow-distortion is used",
    )
    packet.set_defaults(function=cmd_packet)
    approve = sub.add_parser("approve", help="Approve the exact review-packet hash")
    approve.add_argument("tile", type=int)
    approve.add_argument("--approved-by", default="Joel or ChatGPT")
    approve.add_argument("--note", default="")
    approve.add_argument(
        "--reference-method",
        default="local-osm-and-kauffman-packet",
        choices=(
            "local-osm",
            "local-kauffman",
            "local-osm-and-kauffman",
            "local-osm-and-kauffman-packet",
            "qgis-osm",
            "qgis-kauffman",
            "qgis-osm-and-kauffman",
        ),
    )
    approve.add_argument(
        "--verification-note",
        default="Reviewed the hash-locked local OSM and Kauffman contact sheet.",
    )
    approve.set_defaults(function=cmd_approve)
    finish = sub.add_parser("finish", help="Run the final verified local warp")
    finish.add_argument("tile", type=int)
    finish.add_argument("--quality-note", default="")
    finish.set_defaults(function=cmd_finish)
    status = sub.add_parser("status", help="Show the resumable batch queue")
    status.set_defaults(function=cmd_status)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.database = args.database.expanduser().resolve()
    for name in (
        "osm_db",
        "cache",
        "aliases",
        "source",
        "kauffman_map",
        "index_json",
        "index_cache",
        "index_raster",
    ):
        value = getattr(args, name, None)
        if isinstance(value, Path):
            setattr(args, name, value.expanduser().resolve())
    return args.function(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        RuntimeError,
        subprocess.CalledProcessError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        HTTPError,
        URLError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
