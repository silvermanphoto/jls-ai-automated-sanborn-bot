#!/usr/bin/env python3
"""Work out what a Sanborn sheet should be called in the QGIS layer list.

Joel wants a sheet identified by the crossroads it is known for -- "tile 196
(Central & S. Pryor)" -- rather than by a file name.  Two rules govern the
choice, both his:

* the two streets must genuinely cross each other inside that sheet, and
* it is better if they are still called the same thing on today's map.

Both are already settled by the time this runs.  The control proposal for each
tile lists intersections that were read off the printed sheet and then matched
to a real junction node in OpenStreetMap, carrying the historic spelling and the
modern spelling side by side.  This module only has to choose between them and
write the result out tidily.

The choice is by prominence: the junction of two major roads is what a
neighbourhood is known by, so each candidate is scored on how important OSM
considers its two streets, with a bonus when the printed name and the modern
name still agree.  The printed spelling is what gets displayed, because the
layer is a 1911 sheet.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable


# How much weight each OpenStreetMap road classification carries. A junction of
# two primary roads is a landmark; a junction of two residential streets is not.
ROAD_IMPORTANCE: dict[str, int] = {
    "motorway": 9,
    "trunk": 8,
    "primary": 7,
    "secondary": 6,
    "tertiary": 5,
    "unclassified": 3,
    "residential": 2,
    "living_street": 2,
    "pedestrian": 1,
    "service": 1,
}

# Dropped from the end of a street name: the sheet says "S. PRYOR ST." and Joel
# writes "S. Pryor". Road type is noise once the name is in a layer list.
STREET_TYPES = {
    "st", "street", "ave", "av", "avenue", "rd", "road", "pl", "place",
    "dr", "drive", "blvd", "boulevard", "ln", "lane", "way", "ter", "terrace",
    "ct", "court", "cir", "circle", "pkwy", "parkway", "hwy", "highway",
    "aly", "alley", "sq", "square", "row", "walk", "trl", "trail",
}

# Kept upper-case when the rest of the name is title-cased.
_DIRECTIONAL = re.compile(r"^(?:[NSEW]|N\.?E|N\.?W|S\.?E|S\.?W)\.?$", re.IGNORECASE)


def tidy_street(raw: str) -> str:
    """Turn a printed street name into the form Joel writes.

    ``"S. PRYOR ST."`` becomes ``"S. Pryor"``; ``"FULTON"`` becomes ``"Fulton"``.
    """
    if not raw:
        return ""
    # Strip OCR debris: pipes, stray brackets, repeated punctuation, trailing commas.
    cleaned = re.sub(r"[|\[\]{}<>_]+", " ", str(raw))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,-")
    if not cleaned:
        return ""

    words = cleaned.split()
    # Drop a trailing road type, but never the whole name ("Broadway" survives).
    while len(words) > 1 and words[-1].strip(".").lower() in STREET_TYPES:
        words.pop()

    out: list[str] = []
    for word in words:
        bare = word.strip(".")
        if _DIRECTIONAL.match(word):
            # A compass point, not a word. Single letters take a full stop the
            # way the sheets print them ("S. Pryor"); Atlanta's two-letter
            # quadrants are written without one ("NE").
            letters = bare.upper().replace(".", "")
            out.append(f"{letters}." if len(letters) == 1 else letters)
        elif bare.isupper() and len(bare) <= 3 and not bare.isalpha():
            out.append(word)
        else:
            out.append(bare.capitalize() if bare.isalpha() else word.strip("."))
    return " ".join(out)


def _way_importance(connection: sqlite3.Connection, way_ids: Iterable[int]) -> int:
    """The best road classification among the OSM ways carrying a street."""
    ids = [int(w) for w in way_ids if w is not None]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    rows = connection.execute(
        f"SELECT highway FROM ways WHERE osm_way_id IN ({placeholders})", ids
    ).fetchall()
    return max((ROAD_IMPORTANCE.get(row[0], 0) for row in rows), default=0)


def _names_still_agree(historic: str, modern: str) -> bool:
    """True when the printed street is still called that today.

    Compared on the leading word only, so "CAPITOL" and "Capitol Ave SW" agree
    while "CAPITOL" and "Memorial Drive" do not.
    """
    a = tidy_street(historic).lower().split()
    b = tidy_street(modern).lower().split()
    if not a or not b:
        return False
    a = [w for w in a if not _DIRECTIONAL.match(w)]
    b = [w for w in b if not _DIRECTIONAL.match(w)]
    return bool(a and b and a[0] == b[0])


def score_candidate(
    candidate: dict[str, Any], connection: sqlite3.Connection | None
) -> tuple[int, int, str]:
    """Rank one intersection. Higher sorts first."""
    way_a: list[int] = []
    way_b: list[int] = []
    for match in candidate.get("osm_matches") or []:
        if match.get("way_a") is not None:
            way_a.append(match["way_a"])
        if match.get("way_b") is not None:
            way_b.append(match["way_b"])

    if connection is None:
        importance = 0
    else:
        # The weaker of the two streets decides: a major road crossing an alley
        # is not the junction the area is known by.
        importance = min(
            _way_importance(connection, way_a), _way_importance(connection, way_b)
        )

    still_named = sum(
        (
            _names_still_agree(
                candidate.get("historic_street_a", ""),
                candidate.get("modern_street_a", ""),
            ),
            _names_still_agree(
                candidate.get("historic_street_b", ""),
                candidate.get("modern_street_b", ""),
            ),
        )
    )
    # The label is the final tiebreak purely so the choice is deterministic.
    return (importance, still_named, str(candidate.get("label", "")))


def format_intersection(
    candidate: dict[str, Any], connection: sqlite3.Connection | None = None
) -> str:
    """``"Central & S. Pryor"`` -- more prominent street first."""
    a = tidy_street(candidate.get("historic_street_a", ""))
    b = tidy_street(candidate.get("historic_street_b", ""))
    if not a or not b:
        return a or b

    if connection is not None:
        importance_a = _way_importance(
            connection,
            [m["way_a"] for m in candidate.get("osm_matches") or [] if m.get("way_a")],
        )
        importance_b = _way_importance(
            connection,
            [m["way_b"] for m in candidate.get("osm_matches") or [] if m.get("way_b")],
        )
        if importance_b > importance_a:
            a, b = b, a
    return f"{a} & {b}"


def intersection_for_proposal(
    proposal: dict[str, Any], osm_database: Path | None = None
) -> str | None:
    """The best-known crossroads on one sheet, or None when nothing qualifies."""
    candidates = [
        c
        for c in (proposal.get("control_candidates") or [])
        if c.get("historic_street_a") and c.get("historic_street_b")
    ]
    if not candidates:
        return None

    connection = None
    try:
        if osm_database and Path(osm_database).exists():
            connection = sqlite3.connect(
                f"file:{osm_database}?mode=ro", uri=True
            )
        best = max(candidates, key=lambda c: score_candidate(c, connection))
        return format_intersection(best, connection) or None
    finally:
        if connection is not None:
            connection.close()
