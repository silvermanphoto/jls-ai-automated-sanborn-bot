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
    # Earlier file names of the same master project. Ledgers written before a
    # rename still name the file that was protected at the time; they are
    # accepted as history, never used as the live project.
    former_projects: tuple[Path, ...] = ()
    # Subfolders inside the group, in the order they appear in QGIS, each with
    # the printed tile numbers it holds (first, last; last None = no limit).
    # Empty means sheet layers sit directly in the group.
    subfolders: tuple[tuple[str, int, int | None], ...] = ()

    def names_protected_project(self, path: str | Path) -> bool:
        """True when a recorded path is this master project, now or before a rename."""
        resolved = Path(path).expanduser().resolve()
        return any(
            resolved == candidate.resolve()
            for candidate in (self.project, *self.former_projects)
        )

    def subfolder_for(self, tile: int) -> str | None:
        """The subfolder of the group that holds this printed tile number."""
        if not self.subfolders:
            return None
        for name, first, last in self.subfolders:
            if tile >= first and (last is None or tile <= last):
                return name
        raise ValueError(f"Tile {tile} falls outside every {self.group_name} subfolder.")

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
        # Joel re-saved the master under this name on 2026-09-28.
        project=MAP_BOOK / "JLS Master Map File with 1911 Sanborns.qgz",
        former_projects=(MAP_BOOK / "JLS Master Map File.qgz",),
        group_name="1911 ATLANTA SANBORNS",
        # Joel's layout from 2026-09-28: one folder per volume, named by area.
        # Volume 1 holds tiles 1-90, volume 2 151-252, volume 3 301-325 and
        # volume 4 400 upward; the bounds below follow his rule by number.
        subfolders=(
            ("NORTHEAST ATL", 100, 299),
            ("NORTHWEST ATL", 0, 99),
            ("SOUTHEAST ATL", 400, None),
            ("SOUTHWEST ATL", 300, 399),
        ),
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
