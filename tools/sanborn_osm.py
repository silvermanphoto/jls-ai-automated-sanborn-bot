#!/usr/bin/env python3
"""Build and query a local OpenStreetMap street-intersection index.

The index deliberately treats only a node shared by two named highway ways as
an intersection.  It never estimates a crossing from geometry and never
averages several nearby nodes into one control point.  This matters for divided
roads, bridges, tunnels, and other places where lines can cross on a map without
meeting on the ground.

Only Python's standard library is required for OSM XML.  Importing a ``.pbf``
file additionally requires the local ``osmium`` command so it can be clipped to
the requested bounding box and converted to XML before indexing.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import gzip
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
import tempfile
import time
from typing import Iterator, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import unicodedata
import xml.etree.ElementTree as ET


EARTH_RADIUS_METERS = 6_378_137.0
MAX_MERCATOR_LATITUDE = 85.05112878
DEFAULT_ATLANTA_BBOX = (33.60, -84.55, 33.90, -84.25)
DEFAULT_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
USER_AGENT = "Joel-Silverman-Sanborn-Bot/1.0 (local historical-map research)"
SCHEMA_VERSION = 1

NAME_TAGS = (
    "name",
    "alt_name",
    "old_name",
    "official_name",
    "short_name",
    "loc_name",
    "name:en",
)

WORD_REPLACEMENTS = {
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
    "ne": "northeast",
    "nw": "northwest",
    "se": "southeast",
    "sw": "southwest",
    "st": "street",
    "str": "street",
    "ave": "avenue",
    "av": "avenue",
    "rd": "road",
    "blvd": "boulevard",
    "dr": "drive",
    "ln": "lane",
    "ct": "court",
    "cir": "circle",
    "pkwy": "parkway",
    "pl": "place",
    "ter": "terrace",
    "trl": "trail",
    "hwy": "highway",
    "first": "1st",
    "second": "2nd",
    "third": "3rd",
    "fourth": "4th",
    "fifth": "5th",
    "sixth": "6th",
    "seventh": "7th",
    "eighth": "8th",
    "ninth": "9th",
    "tenth": "10th",
    "eleventh": "11th",
    "twelfth": "12th",
    "thirteenth": "13th",
    "fourteenth": "14th",
    "fifteenth": "15th",
    "sixteenth": "16th",
    "seventeenth": "17th",
    "eighteenth": "18th",
    "nineteenth": "19th",
    "twentieth": "20th",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_name(text: str) -> str:
    """Return a stable comparison form for an OSM or historic street name.

    Normalization is intentionally modest.  It expands common direction and
    street-type abbreviations, but it does not invent historical aliases or use
    fuzzy matching.  Alternate names must come from OSM tags or a separately
    reviewed historical-alias record.
    """

    value = unicodedata.normalize("NFKD", str(text))
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    words = [WORD_REPLACEMENTS.get(word, word) for word in value.split()]
    return " ".join(words)


def web_mercator(lon: float, lat: float) -> tuple[float, float]:
    """Convert WGS84 longitude/latitude to EPSG:3857 meters."""

    latitude = max(-MAX_MERCATOR_LATITUDE, min(MAX_MERCATOR_LATITUDE, float(lat)))
    longitude = float(lon)
    x = EARTH_RADIUS_METERS * math.radians(longitude)
    y = EARTH_RADIUS_METERS * math.log(
        math.tan(math.pi / 4.0 + math.radians(latitude) / 2.0)
    )
    return x, y


def validate_bbox(bbox: Sequence[float]) -> tuple[float, float, float, float]:
    if len(bbox) != 4:
        raise ValueError("Bounding box must be south, west, north, east.")
    south, west, north, east = (float(value) for value in bbox)
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise ValueError("Invalid bounding box. Expected south, west, north, east.")
    if (north - south) * (east - west) > 0.25:
        raise ValueError(
            "Bounding box is too large for a conservative Overpass request; "
            "use a regional .osm/.pbf extract instead."
        )
    return south, west, north, east


def overpass_query(bbox: Sequence[float]) -> str:
    south, west, north, east = validate_bbox(bbox)
    bounds = f"{south:.8f},{west:.8f},{north:.8f},{east:.8f}"
    selectors = "\n".join(
        f'  way["highway"]["{tag}"]({bounds});' for tag in NAME_TAGS
    )
    return (
        "[out:xml][timeout:180];\n"
        "(\n"
        f"{selectors}\n"
        ");\n"
        "(._;>;);\n"
        "out body qt;\n"
    )


def _retry_delay(error: HTTPError | URLError, attempt: int) -> int:
    if isinstance(error, HTTPError):
        retry_after = error.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return min(60, max(1, int(retry_after)))
    return min(60, 4 * (2**attempt))


def acquire_overpass(
    destination: str | Path,
    bbox: Sequence[float] = DEFAULT_ATLANTA_BBOX,
    endpoint: str = DEFAULT_OVERPASS_ENDPOINT,
    *,
    replace: bool = False,
) -> Path:
    """Download and cache named Atlanta highway data as OSM XML.

    Existing cache files are reused unless ``replace`` is explicitly true.
    Downloads use one request, conservative retry/backoff behavior, and a
    temporary partial file so interruption cannot make an incomplete cache look
    valid.
    """

    output = Path(destination).expanduser().resolve()
    if output.exists() and not replace:
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    query = overpass_query(bbox)
    body = urlencode({"data": query}).encode("utf-8")
    last_error: Exception | None = None
    partial = output.with_suffix(output.suffix + ".part")
    if partial.exists():
        partial.unlink()

    for attempt in range(4):
        request = Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "Accept": "application/xml,text/xml",
            },
        )
        try:
            with closing(urlopen(request, timeout=240)) as response, partial.open("wb") as handle:
                content_type = response.headers.get("Content-Type", "").lower()
                first = response.read(4096)
                lowered = first.lower()
                if "text/html" in content_type or b"<html" in lowered:
                    if b"captcha" in lowered or b"prove you are human" in lowered:
                        raise RuntimeError(
                            "The OSM service requested human verification. Pause for Joel."
                        )
                    raise RuntimeError("The OSM service returned HTML instead of map data.")
                handle.write(first)
                shutil.copyfileobj(response, handle, length=1024 * 1024)
            root = ET.parse(partial).getroot()
            if root.tag.rsplit("}", 1)[-1] != "osm":
                raise RuntimeError("The OSM service response is not an OSM dataset.")
            remarks = [
                (element.text or "").strip()
                for element in root.iter()
                if element.tag.rsplit("}", 1)[-1] == "remark"
                and (element.text or "").strip()
            ]
            if remarks:
                raise RuntimeError("The OSM service reported: " + " | ".join(remarks))
            partial.replace(output)
            metadata = {
                "schema_version": SCHEMA_VERSION,
                "source_kind": "overpass",
                "source_url": endpoint,
                "bbox": list(validate_bbox(bbox)),
                "query": query,
                "downloaded_utc": utc_now(),
                "source_sha256": sha256(output),
                "source_bytes": output.stat().st_size,
            }
            output.with_suffix(output.suffix + ".metadata.json").write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return output
        except (HTTPError, URLError) as error:
            last_error = error
            if isinstance(error, HTTPError) and error.code not in {
                429,
                500,
                502,
                503,
                504,
                520,
                522,
                524,
            }:
                break
            if attempt < 3:
                delay = _retry_delay(error, attempt)
                print(
                    f"OSM request paused {delay}s after a temporary error...",
                    file=sys.stderr,
                )
                time.sleep(delay)
        except Exception:
            if partial.exists():
                partial.unlink()
            raise

    if partial.exists():
        partial.unlink()
    if last_error:
        raise RuntimeError(f"Unable to download OSM data: {last_error}") from last_error
    raise RuntimeError("Unable to download OSM data.")


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE nodes (
            osm_node_id INTEGER PRIMARY KEY,
            lon REAL NOT NULL,
            lat REAL NOT NULL,
            x_3857 REAL NOT NULL,
            y_3857 REAL NOT NULL
        );
        CREATE TABLE ways (
            osm_way_id INTEGER PRIMARY KEY,
            highway TEXT NOT NULL,
            primary_name TEXT NOT NULL,
            bridge TEXT,
            tunnel TEXT,
            layer TEXT,
            oneway TEXT
        );
        CREATE TABLE way_names (
            osm_way_id INTEGER NOT NULL,
            raw_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            source_tag TEXT NOT NULL,
            PRIMARY KEY (osm_way_id, raw_name, normalized_name, source_tag),
            FOREIGN KEY (osm_way_id) REFERENCES ways(osm_way_id) ON DELETE CASCADE
        );
        CREATE TABLE way_nodes (
            osm_way_id INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            osm_node_id INTEGER NOT NULL,
            PRIMARY KEY (osm_way_id, sequence),
            FOREIGN KEY (osm_way_id) REFERENCES ways(osm_way_id) ON DELETE CASCADE,
            FOREIGN KEY (osm_node_id) REFERENCES nodes(osm_node_id) ON DELETE CASCADE
        );
        CREATE TABLE intersections (
            osm_node_id INTEGER NOT NULL,
            way_a INTEGER NOT NULL,
            way_b INTEGER NOT NULL,
            PRIMARY KEY (osm_node_id, way_a, way_b),
            CHECK (way_a < way_b),
            FOREIGN KEY (osm_node_id) REFERENCES nodes(osm_node_id),
            FOREIGN KEY (way_a) REFERENCES ways(osm_way_id),
            FOREIGN KEY (way_b) REFERENCES ways(osm_way_id)
        );
        CREATE INDEX way_names_normalized_idx
            ON way_names(normalized_name, osm_way_id);
        CREATE INDEX way_nodes_node_idx
            ON way_nodes(osm_node_id, osm_way_id);
        CREATE INDEX intersections_node_idx
            ON intersections(osm_node_id);
        """
    )


@contextmanager
def _open_xml(path: Path):
    if path.name.lower().endswith(".gz"):
        with gzip.open(path, "rb") as handle:
            yield handle
    else:
        with path.open("rb") as handle:
            yield handle


@contextmanager
def _xml_source(source: Path, bbox: Sequence[float]):
    """Yield OSM XML, converting and clipping PBF locally when necessary."""

    if not source.name.lower().endswith(".pbf"):
        yield source
        return

    osmium = shutil.which("osmium")
    if not osmium:
        raise RuntimeError(
            "Importing .pbf requires the local osmium command. Install osmium-tool "
            "or provide an .osm/.osm.gz extract."
        )
    south, west, north, east = validate_bbox(bbox)
    with tempfile.TemporaryDirectory(prefix="sanborn-osm-") as folder:
        clipped = Path(folder) / "atlanta.osm.pbf"
        xml_path = Path(folder) / "atlanta.osm"
        subprocess.run(
            [
                osmium,
                "extract",
                "-b",
                f"{west},{south},{east},{north}",
                str(source),
                "-o",
                str(clipped),
            ],
            check=True,
        )
        subprocess.run(
            [osmium, "cat", str(clipped), "-f", "osm", "-o", str(xml_path)],
            check=True,
        )
        yield xml_path


def _tag_values(tags: Mapping[str, str]) -> list[tuple[str, str, str]]:
    values: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for source_tag in NAME_TAGS:
        raw_value = tags.get(source_tag, "")
        for raw_name in raw_value.split(";"):
            raw_name = raw_name.strip()
            normalized = normalize_name(raw_name)
            row = (raw_name, normalized, source_tag)
            if raw_name and normalized and row not in seen:
                seen.add(row)
                values.append(row)
            # Atlanta OSM names often carry a terminal address quadrant that is
            # absent from a Sanborn label (for example, "5th Street NE").  Add
            # a searchable variant while retaining the exact tagged name and
            # recording how the variant was derived.  Prefix directions such
            # as "North Highland" are never stripped.
            words = normalized.split()
            if len(words) > 2 and words[-1] in {
                "northeast",
                "northwest",
                "southeast",
                "southwest",
            }:
                without_quadrant = " ".join(words[:-1])
                variant = (
                    raw_name,
                    without_quadrant,
                    f"{source_tag}:without-quadrant",
                )
                if variant not in seen:
                    seen.add(variant)
                    values.append(variant)
    return values


def _parse_nodes(
    connection: sqlite3.Connection,
    xml_path: Path,
    bbox: Sequence[float],
) -> set[int]:
    south, west, north, east = validate_bbox(bbox)
    node_ids: set[int] = set()
    rows: list[tuple[int, float, float, float, float]] = []
    with _open_xml(xml_path) as handle:
        for _, element in ET.iterparse(handle, events=("end",)):
            if element.tag != "node":
                if element.tag == "way":
                    element.clear()
                continue
            lat = float(element.attrib["lat"])
            lon = float(element.attrib["lon"])
            if south <= lat <= north and west <= lon <= east:
                node_id = int(element.attrib["id"])
                x, y = web_mercator(lon, lat)
                rows.append((node_id, lon, lat, x, y))
                node_ids.add(node_id)
                if len(rows) >= 10_000:
                    connection.executemany(
                        "INSERT OR REPLACE INTO nodes VALUES (?, ?, ?, ?, ?)", rows
                    )
                    rows.clear()
            element.clear()
    if rows:
        connection.executemany(
            "INSERT OR REPLACE INTO nodes VALUES (?, ?, ?, ?, ?)", rows
        )
    return node_ids


def _parse_ways(
    connection: sqlite3.Connection,
    xml_path: Path,
    node_ids: set[int],
) -> int:
    way_count = 0
    with _open_xml(xml_path) as handle:
        for _, element in ET.iterparse(handle, events=("end",)):
            if element.tag != "way":
                if element.tag == "node":
                    element.clear()
                continue
            tags = {
                child.attrib["k"]: child.attrib.get("v", "")
                for child in element
                if child.tag == "tag" and "k" in child.attrib
            }
            names = _tag_values(tags)
            if tags.get("highway") and names:
                refs = [
                    int(child.attrib["ref"])
                    for child in element
                    if child.tag == "nd" and child.attrib.get("ref")
                ]
                inside_refs = [(sequence, ref) for sequence, ref in enumerate(refs) if ref in node_ids]
                if inside_refs:
                    way_id = int(element.attrib["id"])
                    primary = tags.get("name") or names[0][0]
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO ways(
                            osm_way_id, highway, primary_name, bridge,
                            tunnel, layer, oneway
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            way_id,
                            tags["highway"],
                            primary,
                            tags.get("bridge"),
                            tags.get("tunnel"),
                            tags.get("layer"),
                            tags.get("oneway"),
                        ),
                    )
                    connection.executemany(
                        "INSERT OR IGNORE INTO way_names VALUES (?, ?, ?, ?)",
                        [(way_id, raw, normalized, source_tag) for raw, normalized, source_tag in names],
                    )
                    connection.executemany(
                        "INSERT OR REPLACE INTO way_nodes VALUES (?, ?, ?)",
                        [(way_id, sequence, ref) for sequence, ref in inside_refs],
                    )
                    way_count += 1
            element.clear()
    return way_count


def _build_intersections(connection: sqlite3.Connection) -> int:
    connection.execute(
        """
        INSERT OR IGNORE INTO intersections(osm_node_id, way_a, way_b)
        SELECT left_nodes.osm_node_id, left_nodes.osm_way_id, right_nodes.osm_way_id
        FROM way_nodes AS left_nodes
        JOIN way_nodes AS right_nodes
          ON right_nodes.osm_node_id = left_nodes.osm_node_id
         AND right_nodes.osm_way_id > left_nodes.osm_way_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM way_names AS left_name
            JOIN way_names AS right_name
              ON right_name.osm_way_id = right_nodes.osm_way_id
             AND right_name.normalized_name = left_name.normalized_name
            WHERE left_name.osm_way_id = left_nodes.osm_way_id
        )
        """
    )
    return int(connection.execute("SELECT COUNT(*) FROM intersections").fetchone()[0])


def import_osm(
    source: str | Path,
    db_path: str | Path,
    bbox: Sequence[float] = DEFAULT_ATLANTA_BBOX,
    *,
    replace: bool = False,
    provenance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a fresh SQLite index from a bounded OSM XML or PBF source."""

    source_path = Path(source).expanduser().resolve()
    destination = Path(db_path).expanduser().resolve()
    if not source_path.is_file():
        raise RuntimeError(f"OSM source does not exist: {source_path}")
    if destination.exists() and not replace:
        raise RuntimeError(
            f"OSM index already exists: {destination}. Use replace=True to refresh it."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    bbox_tuple = validate_bbox(bbox)
    fd, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".building", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    temporary.unlink()

    try:
        connection = sqlite3.connect(temporary)
        try:
            _schema(connection)
            with _xml_source(source_path, bbox_tuple) as xml_path:
                node_ids = _parse_nodes(connection, xml_path, bbox_tuple)
                _parse_ways(connection, xml_path, node_ids)
            intersection_pair_count = _build_intersections(connection)
            way_count = int(connection.execute("SELECT COUNT(*) FROM ways").fetchone()[0])
            counts = {
                "node_count": int(connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]),
                "way_count": way_count,
                "named_way_name_count": int(
                    connection.execute("SELECT COUNT(*) FROM way_names").fetchone()[0]
                ),
                "intersection_pair_count": intersection_pair_count,
                "intersection_node_count": int(
                    connection.execute(
                        "SELECT COUNT(DISTINCT osm_node_id) FROM intersections"
                    ).fetchone()[0]
                ),
            }
            if counts["node_count"] == 0 or counts["way_count"] == 0:
                raise RuntimeError(
                    "The bounded OSM source contained no named highway data; "
                    "the source or bounding box is probably wrong."
                )
            metadata: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "source_kind": "local-import",
                "source_path": str(source_path),
                "source_sha256": sha256(source_path),
                "source_bytes": source_path.stat().st_size,
                "bbox": list(bbox_tuple),
                "refreshed_utc": utc_now(),
                **counts,
            }
            if provenance:
                metadata.update(dict(provenance))
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                [
                    (key, json.dumps(value, sort_keys=True))
                    for key, value in metadata.items()
                ],
            )
            connection.commit()
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"OSM index has foreign-key errors: {violations[:3]}")
            connection.execute("PRAGMA optimize")
        finally:
            connection.close()
        os.replace(temporary, destination)
        return metadata
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


@contextmanager
def _connection(db: str | Path | sqlite3.Connection):
    if isinstance(db, sqlite3.Connection):
        yield db
        return
    path = Path(db).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"OSM index does not exist: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def lookup_intersections(
    db: str | Path | sqlite3.Connection,
    street_a: str,
    street_b: str,
    *,
    near_x: float | None = None,
    near_y: float | None = None,
    limit: int | None = None,
) -> list[dict[str, object]]:
    """Return every exact shared-node candidate for two normalized names.

    One result is returned per distinct OSM node.  Multiple way-pair matches at
    that node are included as evidence within the result.  Nearby divided-road
    nodes remain separate results and are never replaced by their average.
    """

    normalized_a = normalize_name(street_a)
    normalized_b = normalize_name(street_b)
    if not normalized_a or not normalized_b:
        raise ValueError("Both street names must contain letters or numbers.")
    if normalized_a == normalized_b:
        raise ValueError("Provide two different street names.")
    if (near_x is None) != (near_y is None):
        raise ValueError("near_x and near_y must be supplied together.")

    sql = """
        SELECT
            intersections.osm_node_id,
            nodes.lon, nodes.lat, nodes.x_3857, nodes.y_3857,
            intersections.way_a, intersections.way_b,
            left_name.raw_name AS raw_a,
            left_name.source_tag AS source_tag_a,
            right_name.raw_name AS raw_b,
            right_name.source_tag AS source_tag_b
        FROM intersections
        JOIN nodes USING (osm_node_id)
        JOIN way_names AS left_name
          ON left_name.osm_way_id = intersections.way_a
        JOIN way_names AS right_name
          ON right_name.osm_way_id = intersections.way_b
        WHERE left_name.normalized_name = ?
          AND right_name.normalized_name = ?
        UNION ALL
        SELECT
            intersections.osm_node_id,
            nodes.lon, nodes.lat, nodes.x_3857, nodes.y_3857,
            intersections.way_b AS way_a,
            intersections.way_a AS way_b,
            right_name.raw_name AS raw_a,
            right_name.source_tag AS source_tag_a,
            left_name.raw_name AS raw_b,
            left_name.source_tag AS source_tag_b
        FROM intersections
        JOIN nodes USING (osm_node_id)
        JOIN way_names AS left_name
          ON left_name.osm_way_id = intersections.way_a
        JOIN way_names AS right_name
          ON right_name.osm_way_id = intersections.way_b
        WHERE right_name.normalized_name = ?
          AND left_name.normalized_name = ?
    """
    with _connection(db) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            sql,
            (normalized_a, normalized_b, normalized_a, normalized_b),
        ).fetchall()

    candidates: dict[int, dict[str, object]] = {}
    evidence_seen: dict[int, set[tuple[object, ...]]] = {}
    for row in rows:
        node_id = int(row["osm_node_id"])
        if node_id not in candidates:
            candidate: dict[str, object] = {
                "osm_node_id": node_id,
                "lon": float(row["lon"]),
                "lat": float(row["lat"]),
                "x_3857": float(row["x_3857"]),
                "y_3857": float(row["y_3857"]),
                "query": {
                    "street_a": street_a,
                    "street_b": street_b,
                    "normalized_a": normalized_a,
                    "normalized_b": normalized_b,
                },
                "matches": [],
            }
            if near_x is not None and near_y is not None:
                candidate["distance_m"] = math.hypot(
                    float(row["x_3857"]) - float(near_x),
                    float(row["y_3857"]) - float(near_y),
                )
            candidates[node_id] = candidate
            evidence_seen[node_id] = set()
        evidence_key = (
            row["way_a"],
            row["way_b"],
            row["raw_a"],
            row["source_tag_a"],
            row["raw_b"],
            row["source_tag_b"],
        )
        if evidence_key not in evidence_seen[node_id]:
            evidence_seen[node_id].add(evidence_key)
            candidates[node_id]["matches"].append(  # type: ignore[index,union-attr]
                {
                    "way_a": int(row["way_a"]),
                    "way_b": int(row["way_b"]),
                    "street_a": {
                        "raw_name": row["raw_a"],
                        "source_tag": row["source_tag_a"],
                    },
                    "street_b": {
                        "raw_name": row["raw_b"],
                        "source_tag": row["source_tag_b"],
                    },
                }
            )

    sort_key = (
        (lambda item: (float(item.get("distance_m", math.inf)), int(item["osm_node_id"])))
        if near_x is not None
        else (lambda item: int(item["osm_node_id"]))
    )
    result = sorted(candidates.values(), key=sort_key)
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be a positive integer.")
        result = result[:limit]
    return result


def iter_way_geometries(
    db: str | Path | sqlite3.Connection,
    *,
    normalized_name: str | None = None,
    bbox_3857: Sequence[float] | None = None,
) -> Iterator[dict[str, object]]:
    """Yield named ways with ordered WGS84 and EPSG:3857 vertices."""

    name = normalize_name(normalized_name) if normalized_name else None
    if bbox_3857 is not None:
        if len(bbox_3857) != 4:
            raise ValueError("bbox_3857 must be min_x, min_y, max_x, max_y.")
        min_x, min_y, max_x, max_y = (float(value) for value in bbox_3857)
        if not (min_x < max_x and min_y < max_y):
            raise ValueError("Invalid EPSG:3857 bounding box.")
    else:
        min_x = min_y = max_x = max_y = 0.0

    with _connection(db) as connection:
        connection.row_factory = sqlite3.Row
        if name:
            way_rows = connection.execute(
                """
                SELECT DISTINCT ways.*
                FROM ways JOIN way_names USING (osm_way_id)
                WHERE way_names.normalized_name=?
                ORDER BY ways.osm_way_id
                """,
                (name,),
            ).fetchall()
        else:
            way_rows = connection.execute(
                "SELECT * FROM ways ORDER BY osm_way_id"
            ).fetchall()
        for way in way_rows:
            vertices = connection.execute(
                """
                SELECT nodes.osm_node_id, nodes.lon, nodes.lat,
                       nodes.x_3857, nodes.y_3857
                FROM way_nodes JOIN nodes USING (osm_node_id)
                WHERE way_nodes.osm_way_id=?
                ORDER BY way_nodes.sequence
                """,
                (way["osm_way_id"],),
            ).fetchall()
            if bbox_3857 is not None and not any(
                min_x <= vertex["x_3857"] <= max_x
                and min_y <= vertex["y_3857"] <= max_y
                for vertex in vertices
            ):
                continue
            names = connection.execute(
                """
                SELECT raw_name, normalized_name, source_tag
                FROM way_names WHERE osm_way_id=?
                ORDER BY source_tag, raw_name
                """,
                (way["osm_way_id"],),
            ).fetchall()
            yield {
                "osm_way_id": int(way["osm_way_id"]),
                "highway": way["highway"],
                "primary_name": way["primary_name"],
                "bridge": way["bridge"],
                "tunnel": way["tunnel"],
                "layer": way["layer"],
                "oneway": way["oneway"],
                "names": [dict(row) for row in names],
                "vertices": [dict(row) for row in vertices],
            }


def read_metadata(db: str | Path | sqlite3.Connection) -> dict[str, object]:
    with _connection(db) as connection:
        rows = connection.execute("SELECT key, value FROM metadata ORDER BY key").fetchall()
    return {key: json.loads(value) for key, value in rows}


def _load_download_metadata(path: Path) -> dict[str, object]:
    metadata_path = path.with_suffix(path.suffix + ".metadata.json")
    if not metadata_path.is_file():
        return {}
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def _bbox_argument(values: Sequence[str] | None) -> tuple[float, float, float, float]:
    return validate_bbox(values or DEFAULT_ATLANTA_BBOX)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cache and query exact shared-node OSM street intersections."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    refresh = commands.add_parser("refresh", help="Download Atlanta OSM XML and rebuild the index.")
    refresh.add_argument("--cache", required=True, type=Path)
    refresh.add_argument("--db", required=True, type=Path)
    refresh.add_argument("--bbox", nargs=4, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    refresh.add_argument("--endpoint", default=DEFAULT_OVERPASS_ENDPOINT)
    refresh.add_argument("--replace", action="store_true")

    import_parser = commands.add_parser("import", help="Build the index from a local .osm/.pbf file.")
    import_parser.add_argument("source", type=Path)
    import_parser.add_argument("--db", required=True, type=Path)
    import_parser.add_argument("--bbox", nargs=4, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    import_parser.add_argument("--replace", action="store_true")

    lookup = commands.add_parser("lookup", help="Find every shared-node candidate for two streets.")
    lookup.add_argument("db", type=Path)
    lookup.add_argument("street_a")
    lookup.add_argument("street_b")
    lookup.add_argument("--near-x", type=float)
    lookup.add_argument("--near-y", type=float)
    lookup.add_argument("--limit", type=int)

    ways = commands.add_parser("ways", help="Emit indexed way geometry as JSON lines.")
    ways.add_argument("db", type=Path)
    ways.add_argument("--name")
    ways.add_argument("--bbox-3857", nargs=4, type=float)

    metadata = commands.add_parser("metadata", help="Show index provenance and refresh time.")
    metadata.add_argument("db", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "refresh":
        bbox = _bbox_argument(arguments.bbox)
        cache = acquire_overpass(
            arguments.cache,
            bbox,
            arguments.endpoint,
            replace=arguments.replace,
        )
        metadata = import_osm(
            cache,
            arguments.db,
            bbox,
            replace=arguments.replace,
            provenance=_load_download_metadata(cache),
        )
        print(json.dumps(metadata, indent=2, sort_keys=True))
        return 0
    if arguments.command == "import":
        metadata = import_osm(
            arguments.source,
            arguments.db,
            _bbox_argument(arguments.bbox),
            replace=arguments.replace,
        )
        print(json.dumps(metadata, indent=2, sort_keys=True))
        return 0
    if arguments.command == "lookup":
        candidates = lookup_intersections(
            arguments.db,
            arguments.street_a,
            arguments.street_b,
            near_x=arguments.near_x,
            near_y=arguments.near_y,
            limit=arguments.limit,
        )
        print(json.dumps(candidates, indent=2, sort_keys=True))
        return 0
    if arguments.command == "ways":
        for way in iter_way_geometries(
            arguments.db,
            normalized_name=arguments.name,
            bbox_3857=arguments.bbox_3857,
        ):
            print(json.dumps(way, sort_keys=True))
        return 0
    if arguments.command == "metadata":
        print(json.dumps(read_metadata(arguments.db), indent=2, sort_keys=True))
        return 0
    raise RuntimeError(f"Unknown command: {arguments.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2)
