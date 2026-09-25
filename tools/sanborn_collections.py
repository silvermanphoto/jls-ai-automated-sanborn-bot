#!/usr/bin/env python3
"""One record per body of Sanborn sheets, so more than one can exist.

Everything that is true of *the 1911 Atlanta sheets in particular* -- which QGIS
project they belong in, what their group is called, which index sheet sits above
them, how their layers are named, how they are drawn -- lives here as data rather
than being written into the import code.

Adding another year or another city is therefore adding a record to
``COLLECTIONS`` below, not editing the import machinery.  The default collection
is ``atlanta-1911``; every existing command keeps working untouched because that
record holds exactly the values that used to be constants in ``sanborn_qgis``.

The one thing a new collection cannot inherit is its street data: naming layers
after an intersection needs an OSM extract for that city (see
``sanborn_osm.py``), and the index sheet must already be orthorectified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


MAP_BOOK = Path(
    "/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book"
)

# Joel requires the original TIFF appearance (2026-09-25): neutral color values
# and no channel stretch. Band 4 is the georeferencer's real alpha channel.
STANDARD_STYLE: dict[str, Any] = {
    "brightness": 0,
    "gamma": 1.0,
    "contrast": 0,
    "opacity": 1.0,
    "alpha_band": 4,
}


@dataclass(frozen=True)
class Collection:
    """A single body of sheets: one city, one survey year."""

    key: str
    city: str
    year: int
    project: Path
    group_name: str
    index_layer_name: str
    index_layer_source: Path
    osm_database: Path
    crs: str = "EPSG:3857"
    style: dict[str, Any] = field(default_factory=lambda: dict(STANDARD_STYLE))

    def layer_name(self, tile: int, intersection: str | None = None) -> str:
        """The name Joel sees in the QGIS layer list.

        ``Sanborn 1911 - tile 196 (Central & S. Pryor)``, or without the
        parenthesis when no intersection could be established.  Nothing from the
        file name reaches this, which is how "_georeferenced" and the older
        working titles stop appearing in the project.
        """
        stem = f"Sanborn {self.year} - tile {tile}"
        return f"{stem} ({intersection})" if intersection else stem


COLLECTIONS: dict[str, Collection] = {
    "atlanta-1911": Collection(
        key="atlanta-1911",
        city="Atlanta",
        year=1911,
        project=MAP_BOOK / "JLS Master Map File.qgz",
        group_name="1911 ATLANTA SANBORNS",
        index_layer_name=(
            "1911 Sanborn Index Orthorectified — OSM 9-point fine-tuned (2026-07-14)"
        ),
        index_layer_source=(
            MAP_BOOK
            / "Stage 1 -Orthorectified Atlanta Maps to print"
            / "1911 Sanborn Index Orthorectified_OSM_9point_finetuned_2026-07-14.tif"
        ),
        osm_database=Path(__file__).resolve().parent.parent
        / "batch"
        / "osm"
        / "atlanta-streets.sqlite3",
    ),
}

DEFAULT_COLLECTION = "atlanta-1911"


def get(key: str | None = None) -> Collection:
    """Look up a collection, defaulting to the 1911 Atlanta sheets."""
    resolved = key or DEFAULT_COLLECTION
    try:
        return COLLECTIONS[resolved]
    except KeyError:
        known = ", ".join(sorted(COLLECTIONS))
        raise KeyError(
            f"There is no collection called {resolved!r}. Known collections: {known}."
        ) from None
