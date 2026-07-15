#!/usr/bin/env python3

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from sanborn_osm import (
    import_osm,
    iter_way_geometries,
    lookup_intersections,
    normalize_name,
    read_metadata,
    web_mercator,
)


SYNTHETIC_OSM = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="sanborn-test">
  <node id="1" lat="33.750000" lon="-84.390000" />
  <node id="2" lat="33.751000" lon="-84.390000" />
  <node id="3" lat="33.749000" lon="-84.390000" />
  <node id="4" lat="33.750000" lon="-84.391000" />
  <node id="5" lat="33.750000" lon="-84.389000" />

  <node id="6" lat="33.760000" lon="-84.380000" />
  <node id="7" lat="33.761000" lon="-84.380000" />
  <node id="8" lat="33.759000" lon="-84.380000" />
  <node id="9" lat="33.760000" lon="-84.381000" />
  <node id="10" lat="33.760000" lon="-84.379000" />

  <node id="11" lat="33.764332" lon="-84.355603" />
  <node id="12" lat="33.765000" lon="-84.355603" />
  <node id="13" lat="33.763700" lon="-84.355603" />
  <node id="14" lat="33.764332" lon="-84.356200" />
  <node id="15" lat="33.764332" lon="-84.355000" />

  <!-- These two ways cross geometrically, but do not share an OSM node. -->
  <node id="20" lat="33.740000" lon="-84.400000" />
  <node id="21" lat="33.740000" lon="-84.380000" />
  <node id="22" lat="33.730000" lon="-84.390000" />
  <node id="23" lat="33.750000" lon="-84.390000" />

  <way id="10">
    <nd ref="2"/><nd ref="1"/><nd ref="3"/>
    <tag k="highway" v="primary"/>
    <tag k="name" v="Peachtree St."/>
  </way>
  <way id="20">
    <nd ref="4"/><nd ref="1"/><nd ref="5"/>
    <tag k="highway" v="secondary"/>
    <tag k="name" v="Ponce de Leon Ave"/>
    <tag k="official_name" v="Ponce de Leon Avenue"/>
  </way>

  <way id="11">
    <nd ref="7"/><nd ref="6"/><nd ref="8"/>
    <tag k="highway" v="primary"/>
    <tag k="name" v="Peachtree Street"/>
  </way>
  <way id="21">
    <nd ref="9"/><nd ref="6"/><nd ref="10"/>
    <tag k="highway" v="secondary"/>
    <tag k="name" v="Ponce de Leon Avenue"/>
  </way>

  <way id="30">
    <nd ref="12"/><nd ref="11"/><nd ref="13"/>
    <tag k="highway" v="secondary"/>
    <tag k="name" v="N Highland Ave NE"/>
    <tag k="alt_name" v="North Highland Avenue;Highland Avenue"/>
  </way>
  <way id="31">
    <nd ref="14"/><nd ref="11"/><nd ref="15"/>
    <tag k="highway" v="residential"/>
    <tag k="name" v="Carmel Ave NE"/>
    <tag k="old_name" v="Carmel Avenue"/>
  </way>

  <way id="40">
    <nd ref="20"/><nd ref="21"/>
    <tag k="highway" v="primary"/>
    <tag k="name" v="Bridge Road"/>
    <tag k="bridge" v="yes"/>
    <tag k="layer" v="1"/>
  </way>
  <way id="41">
    <nd ref="22"/><nd ref="23"/>
    <tag k="highway" v="secondary"/>
    <tag k="name" v="Cross Street"/>
  </way>
</osm>
"""


class OSMIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.source = self.folder / "synthetic.osm"
        self.database = self.folder / "atlanta-osm.sqlite3"
        self.source.write_text(SYNTHETIC_OSM, encoding="utf-8")
        self.import_result = import_osm(self.source, self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def test_name_normalization_expands_common_abbreviations(self):
        self.assertEqual(normalize_name("N. Highland Ave., NE"), "north highland avenue northeast")
        self.assertEqual(normalize_name("Ponce-de-Leon St"), "ponce de leon street")
        self.assertEqual(normalize_name("Fifth St. NE"), "5th street northeast")

    def test_terminal_address_quadrant_can_be_omitted_from_query(self):
        candidates = lookup_intersections(
            self.database,
            "North Highland Avenue",
            "Carmel Avenue",
        )
        match_sources = {
            (match["street_a"]["source_tag"], match["street_b"]["source_tag"])
            for match in candidates[0]["matches"]
        }
        self.assertIn(("alt_name", "name:without-quadrant"), match_sources)

    def test_alias_tags_resolve_to_the_exact_shared_node(self):
        candidates = lookup_intersections(
            self.database,
            "North Highland Avenue",
            "Carmel Avenue",
        )
        self.assertEqual([candidate["osm_node_id"] for candidate in candidates], [11])
        self.assertAlmostEqual(candidates[0]["lon"], -84.355603)
        self.assertAlmostEqual(candidates[0]["lat"], 33.764332)
        sources = {
            (match["street_a"]["source_tag"], match["street_b"]["source_tag"])
            for match in candidates[0]["matches"]
        }
        self.assertIn(("alt_name", "old_name"), sources)

    def test_duplicate_named_intersections_remain_separate_candidates(self):
        candidates = lookup_intersections(
            self.database,
            "Peachtree Street",
            "Ponce de Leon Avenue",
        )
        self.assertEqual([candidate["osm_node_id"] for candidate in candidates], [1, 6])
        self.assertNotEqual(candidates[0]["x_3857"], candidates[1]["x_3857"])
        average_x = (candidates[0]["x_3857"] + candidates[1]["x_3857"]) / 2
        self.assertNotIn(average_x, [candidate["x_3857"] for candidate in candidates])

    def test_near_coordinate_sorts_without_collapsing_ambiguity(self):
        near_x, near_y = web_mercator(-84.380000, 33.760000)
        candidates = lookup_intersections(
            self.database,
            "Peachtree Street",
            "Ponce de Leon Avenue",
            near_x=near_x,
            near_y=near_y,
        )
        self.assertEqual([candidate["osm_node_id"] for candidate in candidates], [6, 1])
        self.assertAlmostEqual(candidates[0]["distance_m"], 0.0, places=5)

    def test_geometric_bridge_crossing_without_shared_node_is_not_an_intersection(self):
        candidates = lookup_intersections(self.database, "Bridge Road", "Cross Street")
        self.assertEqual(candidates, [])

    def test_provenance_and_refresh_metadata_are_recorded(self):
        metadata = read_metadata(self.database)
        self.assertEqual(metadata["schema_version"], 1)
        self.assertEqual(metadata["source_path"], str(self.source.resolve()))
        self.assertEqual(len(metadata["source_sha256"]), 64)
        self.assertIn("refreshed_utc", metadata)
        self.assertEqual(metadata["intersection_node_count"], 3)
        self.assertEqual(self.import_result["way_count"], 8)

    def test_way_geometry_retains_order_names_and_projected_coordinates(self):
        ways = list(iter_way_geometries(self.database, normalized_name="Highland Avenue"))
        self.assertEqual([way["osm_way_id"] for way in ways], [30])
        self.assertEqual(
            [vertex["osm_node_id"] for vertex in ways[0]["vertices"]],
            [12, 11, 13],
        )
        self.assertTrue(all("x_3857" in vertex for vertex in ways[0]["vertices"]))

    def test_database_can_be_queried_through_existing_connection(self):
        connection = sqlite3.connect(self.database)
        try:
            candidates = lookup_intersections(
                connection, "North Highland Avenue", "Carmel Avenue"
            )
        finally:
            connection.close()
        self.assertEqual(candidates[0]["osm_node_id"], 11)

    def test_existing_index_is_not_replaced_without_explicit_permission(self):
        before = self.database.read_bytes()
        with self.assertRaises(RuntimeError):
            import_osm(self.source, self.database)
        self.assertEqual(self.database.read_bytes(), before)

    def test_lookup_does_not_create_a_missing_database(self):
        missing = self.folder / "missing.sqlite3"
        with self.assertRaises(RuntimeError):
            lookup_intersections(missing, "Peachtree Street", "Ponce de Leon Avenue")
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
