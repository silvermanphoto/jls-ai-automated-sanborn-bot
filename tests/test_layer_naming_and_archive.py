#!/usr/bin/env python3
"""Cover how sheets are named and how the project is archived before an import."""

from __future__ import annotations

import datetime
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import sanborn_archive  # noqa: E402
import sanborn_collections  # noqa: E402
import sanborn_layer_names as names  # noqa: E402


class TidyStreetTests(unittest.TestCase):
    def test_drops_the_road_type_and_title_cases(self):
        self.assertEqual(names.tidy_street("S. PRYOR ST."), "S. Pryor")
        self.assertEqual(names.tidy_street("CENTRAL AVE"), "Central")
        self.assertEqual(names.tidy_street("FULTON"), "Fulton")

    def test_keeps_a_name_that_is_only_a_road_type(self):
        # "Broadway" must survive: dropping it would leave nothing.
        self.assertEqual(names.tidy_street("BROADWAY"), "Broadway")

    def test_compass_points(self):
        self.assertEqual(names.tidy_street("S PRYOR"), "S. Pryor")
        self.assertEqual(names.tidy_street("N.E. HARRIS ST"), "NE Harris")

    def test_strips_ocr_debris(self):
        self.assertEqual(names.tidy_street("MAIN |"), "Main")
        self.assertEqual(names.tidy_street(""), "")


class StillNamedTheSameTests(unittest.TestCase):
    def test_recognises_a_surviving_name(self):
        self.assertTrue(names._names_still_agree("CAPITOL", "Capitol Ave SW"))
        self.assertTrue(names._names_still_agree("S. PRYOR", "Pryor Street Southwest"))

    def test_rejects_a_renamed_street(self):
        self.assertFalse(names._names_still_agree("CAPITOL", "Memorial Drive"))
        self.assertFalse(names._names_still_agree("", "Anything"))


def _osm_fixture(path: Path, classes: dict[int, str]) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE ways (osm_way_id INTEGER PRIMARY KEY, highway TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO ways VALUES (?, ?)", sorted(classes.items())
    )
    connection.commit()
    connection.close()


def _candidate(label, a, b, modern_a, modern_b, way_a, way_b):
    return {
        "label": label,
        "historic_street_a": a,
        "historic_street_b": b,
        "modern_street_a": modern_a,
        "modern_street_b": modern_b,
        "osm_matches": [{"way_a": way_a, "way_b": way_b}],
    }


class ChoosingTheIntersectionTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.osm = Path(self._dir.name) / "streets.sqlite3"
        _osm_fixture(
            self.osm,
            {
                1: "primary",      # Central Avenue
                2: "secondary",    # Pryor Street
                3: "residential",  # a back street
                4: "residential",  # another back street
            },
        )
        self.addCleanup(self._dir.cleanup)

    def test_prefers_the_junction_of_the_bigger_roads(self):
        proposal = {
            "control_candidates": [
                _candidate("BACK x LANE", "BACK", "LANE", "Back St", "Lane St", 3, 4),
                _candidate(
                    "CENTRAL x S. PRYOR", "CENTRAL", "S. PRYOR",
                    "Central Avenue", "Pryor Street", 1, 2,
                ),
            ]
        }
        self.assertEqual(
            names.intersection_for_proposal(proposal, self.osm),
            "Central & S. Pryor",
        )

    def test_names_the_more_important_street_first(self):
        # Same junction, streets supplied the other way round.
        proposal = {
            "control_candidates": [
                _candidate(
                    "S. PRYOR x CENTRAL", "S. PRYOR", "CENTRAL",
                    "Pryor Street", "Central Avenue", 2, 1,
                )
            ]
        }
        self.assertEqual(
            names.intersection_for_proposal(proposal, self.osm),
            "Central & S. Pryor",
        )

    def test_breaks_a_tie_toward_streets_still_called_that_today(self):
        proposal = {
            "control_candidates": [
                _candidate("A x B", "ALPHA", "BETA", "Renamed Way", "Other Way", 3, 4),
                _candidate("C x D", "GAMMA", "DELTA", "Gamma St", "Delta St", 3, 4),
            ]
        }
        self.assertEqual(
            names.intersection_for_proposal(proposal, self.osm), "Gamma & Delta"
        )

    def test_a_sheet_with_no_candidates_gets_no_invented_name(self):
        self.assertIsNone(names.intersection_for_proposal({}, self.osm))
        self.assertIsNone(
            names.intersection_for_proposal({"control_candidates": []}, self.osm)
        )

    def test_survives_a_missing_street_database(self):
        proposal = {
            "control_candidates": [
                _candidate("A x B", "MAIN", "WASHINGTON", "Main", "Washington", 1, 2)
            ]
        }
        self.assertEqual(
            names.intersection_for_proposal(proposal, Path("/nope/missing.sqlite3")),
            "Main & Washington",
        )


class LayerNameTests(unittest.TestCase):
    def test_matches_the_form_joel_asked_for(self):
        collection = sanborn_collections.get("atlanta-1911")
        self.assertEqual(
            collection.layer_name(196, "Central & S. Pryor"),
            "Sanborn 1911 - tile 196 (Central & S. Pryor)",
        )

    def test_omits_the_bracket_when_no_intersection_is_known(self):
        collection = sanborn_collections.get("atlanta-1911")
        self.assertEqual(collection.layer_name(236), "Sanborn 1911 - tile 236")

    def test_no_superfluous_text_reaches_the_name(self):
        collection = sanborn_collections.get("atlanta-1911")
        produced = collection.layer_name(487, "Pryor & Fulton")
        for unwanted in ("_georeferenced", "_v2", ".tif", "Shmuel"):
            self.assertNotIn(unwanted, produced)

    def test_the_new_name_still_yields_its_tile_number(self):
        # Ordering within the group reads the number back out of the name.
        import sanborn_qgis

        self.assertEqual(
            sanborn_qgis.tile_number_from_name(
                "Sanborn 1911 - tile 196 (Central & S. Pryor)"
            ),
            196,
        )
        # A future collection must parse too, and the year is never the tile.
        self.assertEqual(
            sanborn_qgis.tile_number_from_name("Sanborn 1889 - tile 42 (A & B)"), 42
        )


class CollectionTests(unittest.TestCase):
    def test_the_default_is_the_1911_atlanta_sheets(self):
        collection = sanborn_collections.get()
        self.assertEqual(collection.key, "atlanta-1911")
        self.assertEqual(collection.group_name, "1911 ATLANTA SANBORNS")

    def test_the_fixed_rendering_formula_is_carried_by_the_collection(self):
        style = sanborn_collections.get().style
        self.assertEqual(style["brightness"], 0)
        self.assertEqual(style["gamma"], 1.0)
        self.assertEqual(style["contrast"], 0)
        self.assertEqual(style["alpha_band"], 4)

    def test_an_unknown_collection_says_what_exists(self):
        with self.assertRaises(KeyError) as caught:
            sanborn_collections.get("paris-1730")
        self.assertIn("atlanta-1911", str(caught.exception))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.project = self.root / "JLS Master Map File.qgz"
        self.project.write_bytes(b"pretend project")
        self.archive_dir = self.root / "archive"
        self.addCleanup(self._dir.cleanup)

    def test_uses_joels_dating_style(self):
        self.assertEqual(
            sanborn_archive.archive_name(self.project, datetime.date(2026, 7, 27)),
            "JLS Master Map File 07.27.26.qgz",
        )

    def test_keeps_exactly_one_copy_per_day(self):
        first, replaced_first = sanborn_archive.archive_project(
            self.project, archive_dir=self.archive_dir, commit=False
        )
        self.assertFalse(replaced_first)
        second, replaced_second = sanborn_archive.archive_project(
            self.project, archive_dir=self.archive_dir, commit=False
        )
        self.assertTrue(replaced_second)
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.archive_dir.iterdir())), 1)

    def test_separate_days_are_separate_files(self):
        sanborn_archive.archive_project(
            self.project, when=datetime.date(2026, 7, 26),
            archive_dir=self.archive_dir, commit=False,
        )
        sanborn_archive.archive_project(
            self.project, when=datetime.date(2026, 7, 27),
            archive_dir=self.archive_dir, commit=False,
        )
        self.assertEqual(len(list(self.archive_dir.iterdir())), 2)

    def test_the_copy_is_faithful(self):
        target, _ = sanborn_archive.archive_project(
            self.project, archive_dir=self.archive_dir, commit=False
        )
        self.assertEqual(target.read_bytes(), self.project.read_bytes())

    def test_the_master_project_is_never_written(self):
        before = self.project.read_bytes(), self.project.stat().st_mtime_ns
        sanborn_archive.archive_project(
            self.project, archive_dir=self.archive_dir, commit=False
        )
        after = self.project.read_bytes(), self.project.stat().st_mtime_ns
        self.assertEqual(before, after)

    def test_default_backup_is_local_and_never_launches_git(self):
        from unittest.mock import patch
        with patch.object(sanborn_archive, "ARCHIVE_DIR", self.archive_dir), \
                patch("subprocess.run", side_effect=AssertionError("No Git or network calls")):
            target, _ = sanborn_archive.archive_project(self.project)
        self.assertEqual(target.read_bytes(), self.project.read_bytes())

    def test_old_publication_option_stops_before_copying(self):
        with self.assertRaisesRegex(sanborn_archive.ArchiveError, "publication"):
            sanborn_archive.archive_project(
                self.project, archive_dir=self.archive_dir, commit=True
            )
        self.assertFalse(self.archive_dir.exists())

    def test_a_missing_project_stops_the_import(self):
        with self.assertRaises(sanborn_archive.ArchiveError):
            sanborn_archive.archive_project(
                self.root / "not-there.qgz",
                archive_dir=self.archive_dir,
                commit=False,
            )


if __name__ == "__main__":
    unittest.main()
