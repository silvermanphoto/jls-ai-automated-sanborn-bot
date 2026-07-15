#!/usr/bin/env python3
"""Run several independent local Sanborn preparation jobs at once.

Each child invokes the ordinary resumable batch worker for one tile and stops
at human/ChatGPT review.  The SQLite queue remains the source of truth; this
thin launcher only supplies bounded M4 Max concurrency and per-tile logs.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import threading
import time

from sanborn_batch import (
    DEFAULT_ALIASES,
    DEFAULT_DB,
    DEFAULT_INDEX_JSON,
    DEFAULT_OSM_DB,
    BATCH_DIR,
    parse_tiles,
)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def batch_command(
    database: Path,
    tile: int,
    delay: float,
    osm_db: Path,
    index_json: Path,
    aliases: Path,
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).with_name("sanborn_batch.py")),
        "--database",
        str(database),
        "work",
        str(tile),
        "--delay",
        str(delay),
        "--osm-db",
        str(osm_db),
        "--index-json",
        str(index_json),
        "--aliases",
        str(aliases),
    ]


class LaunchPacer:
    """Guarantee a minimum interval between actual child-process launches."""

    def __init__(self, delay: float, *, clock=time.monotonic, sleeper=time.sleep):
        self.delay = delay
        self.clock = clock
        self.sleeper = sleeper
        self._lock = threading.Lock()
        self._last_launch: float | None = None

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            if self._last_launch is not None:
                remaining = self.delay - (now - self._last_launch)
                if remaining > 0:
                    self.sleeper(remaining)
                    now = self.clock()
            self._last_launch = now


def run_tile(
    database: Path,
    tile: int,
    delay: float,
    osm_db: Path,
    index_json: Path,
    aliases: Path,
    log_dir: Path,
    pacer: LaunchPacer,
) -> dict:
    # Each child owns one tile, so its range-level delay would otherwise do
    # nothing. The shared launcher pacer controls the real process start.
    command = batch_command(database, tile, 0.0, osm_db, index_json, aliases)
    pacer.wait()
    completed = subprocess.run(command, capture_output=True, text=True)
    log = log_dir / f"tile-{tile:04d}.log"
    log.write_text(
        f"COMMAND: {' '.join(command)}\n"
        f"EXIT: {completed.returncode}\n\n"
        f"STDOUT\n{completed.stdout}\n\nSTDERR\n{completed.stderr}\n",
        encoding="utf-8",
    )
    return {
        "tile": tile,
        "returncode": completed.returncode,
        "log": str(log),
        "last_line": next(
            (
                line
                for line in reversed((completed.stdout + "\n" + completed.stderr).splitlines())
                if line.strip()
            ),
            "no output",
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tiles", nargs="+", help="Printed numbers or ranges such as 154-196")
    parser.add_argument("--jobs", type=int, default=3, help="Concurrent local tiles (default: 3)")
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Minimum seconds between tile-worker launches (default: 2)",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--osm-db", type=Path, default=DEFAULT_OSM_DB)
    parser.add_argument("--index-json", type=Path, default=DEFAULT_INDEX_JSON)
    parser.add_argument("--aliases", type=Path, default=DEFAULT_ALIASES)
    parser.add_argument("--logs", type=Path)
    parser.add_argument(
        "--already-queued",
        action="store_true",
        help="Skip the idempotent add step when all requested tiles are already in the queue",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.jobs <= 8:
        raise RuntimeError("Use between 1 and 8 concurrent local jobs.")
    if args.delay < 0:
        raise RuntimeError("The LOC request delay cannot be negative.")
    tiles = parse_tiles(args.tiles)
    database = args.database.expanduser().resolve()
    osm_db = args.osm_db.expanduser().resolve()
    index_json = args.index_json.expanduser().resolve()
    aliases = args.aliases.expanduser().resolve()
    if not osm_db.is_file():
        raise RuntimeError("Build the local OSM street index before starting parallel work.")
    if not aliases.is_file():
        raise RuntimeError("The reviewed historical street-alias file is missing.")
    if not index_json.is_file():
        print(
            "The tile-location index is not built; work can continue, but every missing "
            "location will remain flagged for review.",
            file=sys.stderr,
        )
    if not args.already_queued:
        queued = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("sanborn_batch.py")),
                "--database",
                str(database),
                "add",
                *[str(tile) for tile in tiles],
            ]
        )
        if queued.returncode:
            raise RuntimeError("The requested tiles could not be added to the local queue.")
    log_dir = (
        args.logs.expanduser().resolve()
        if args.logs
        else BATCH_DIR / "logs" / f"parallel-{utc_stamp()}"
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"Starting {len(tiles)} local tiles with {args.jobs} concurrent jobs.")
    failures = []
    pacer = LaunchPacer(args.delay)
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        pending = {
            executor.submit(
                run_tile,
                database,
                tile,
                args.delay,
                osm_db,
                index_json,
                aliases,
                log_dir,
                pacer,
            ): tile
            for tile in tiles
        }
        for future in as_completed(pending):
            result = future.result()
            state = "ready for review" if result["returncode"] == 0 else "stopped"
            print(f"Tile {result['tile']}: {state}. {result['last_line']}")
            if result["returncode"]:
                failures.append(result)
    print(f"Per-tile logs: {log_dir}")
    if failures:
        print(f"{len(failures)} tile(s) stopped safely; the remaining tiles continued.")
        return 1
    print("All requested tiles completed their local preparation stage.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
