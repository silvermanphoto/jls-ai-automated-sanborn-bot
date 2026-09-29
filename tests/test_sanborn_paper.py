#!/usr/bin/env python3
"""Paper-whitening recipe files: the pixel function, the recipe text and its guards."""

import ast
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import sanborn_paper  # noqa: E402


SHEET_FACTS = {
    "width": 6806,
    "height": 7925,
    "wkt": 'PROJCRS["WGS 84 / Pseudo-Mercator",BASEGEOGCRS["WGS 84"],ID["EPSG",3857]]',
    "axis_mapping": [1, 2],
    "geotransform": [-9394210.096897371, 0.0603602448553968, 0.0, 3996350.4559200294, 0.0, -0.0603602448553968],
    "bands": [{"type": "Byte", "color": color} for color in ("Red", "Green", "Blue", "Alpha")],
}
QGIS_BIN = Path("/Applications/QGIS.app/Contents/MacOS/bin")


def run_clean(rgb):
    """Output of the pixel function for one pixel, all three colour bands."""
    inputs = [np.array([[value]], dtype=np.uint8) for value in rgb]
    result = []
    for band in (1, 2, 3):
        out = np.zeros((1, 1), dtype=np.uint8)
        sanborn_paper.clean(inputs, out, 0, 0, 1, 1, 1, 1, 0, None, band=str(band))
        result.append(int(out[0, 0]))
    return tuple(result)


def look(value, i, mx):
    """The v1.26 look for one channel with literal numbers (fitted to Joel's sheet 151 example, 2026-09-29)."""
    black, white, gamma = ((28.4, 182.8, 0.793), (0.0, 163.8, 1.358), (0.0, 183.3, 1.144))[i]
    v = 255 * np.clip((value - black) / (white - black), 0, 1) ** gamma
    return v * (1 - 0.8 * np.clip((105.0 - mx) / 45.0, 0, 1))


def levels_only(rgb):
    """The look alone (paper weight 0)."""
    return tuple(int(look(np.float32(value), i, np.float32(max(rgb)))) for i, value in enumerate(rgb))


def proven_clean(in_ar, out_ar, band):
    """The v1.27 formula with literal numbers: look, paper lift, then flat fills."""
    r, g, b = (a.astype(np.float32) for a in in_ar[:3])
    mx = np.maximum(np.maximum(r, g), b); mn = np.minimum(np.minimum(r, g), b)
    paper = np.clip((30 - (mx - mn)) / 12, 0, 1) * np.clip((mx - 140) / 25, 0, 1)
    looks = [look(c, k, mx) for k, c in enumerate((r, g, b))]
    looks = [v + (255 - v) * paper for v in looks]
    c1 = r - g; c2 = (r + g) / 2 - b
    hue = np.degrees(np.arctan2(c2, c1)); light = (r + g + b) / 3
    sat_w = np.clip((np.hypot(c1, c2) - 14) / 10, 0, 1)
    i = int(band) - 1
    total = np.zeros_like(r); mixed = np.zeros_like(r)
    for centre, half, target, fl in ((0, 27, (226, 156, 172), 134), (52, 14, (218, 161, 90), 114),
                                     (82, 14, (237, 210, 77), 130), (230, 40, (137, 178, 199), 110)):
        w = np.clip((half + 2.5 - np.abs((hue - centre + 180) % 360 - 180)) / 5, 0, 1)
        a = np.clip((fl - 12 - light) / (fl - 12 - 60), 0, 1)
        total += w; mixed += w * ((1 - a) * target[i] + a * looks[i])
    fill = np.clip(total, 0, 1) * sat_w * (1 - paper)
    out_ar[:] = fill * (mixed / np.maximum(total, 1e-6)) + (1 - fill) * looks[i]


class PixelFunctionTests(unittest.TestCase):
    def test_dingy_paper_turns_white(self):
        self.assertEqual(run_clean((197, 190, 181)), (255, 255, 255))

    def test_dark_ink_reads_black(self):
        # Joel 2026-09-29: crisp deep black text. Paper weight is 0 for ink; the toe takes it near black.
        self.assertEqual(run_clean((74, 54, 49)), levels_only((74, 54, 49)))
        self.assertTrue(all(v <= 45 for v in run_clean((74, 54, 49))))
        self.assertTrue(all(v <= 12 for v in run_clean((50, 45, 45))))

    def test_fills_become_joels_target_colours(self):
        # Joel 2026-09-29: pink #e29cac, orange #daa15a, yellow #edd24d, blue #89b2c7, whatever the scan's shade.
        for rgb, target in (((152, 116, 132), (226, 156, 172)), ((140, 108, 124), (226, 156, 172)),
                            ((143, 110, 80), (218, 161, 90)), ((158, 148, 85), (237, 210, 77)),
                            ((150, 140, 70), (237, 210, 77)), ((90, 113, 129), (137, 178, 199))):
            with self.subTest(rgb=rgb):
                self.assertTrue(all(abs(o - t) <= 2 for o, t in zip(run_clean(rgb), target)), run_clean(rgb))

    def test_ink_inside_a_fill_stays_dark(self):
        self.assertTrue(all(v <= 60 for v in run_clean((70, 50, 55))))

    def test_yellowed_sheet_paper_is_balanced_to_white(self):
        # Joel 2026-09-29: every sheet's paper ends equally white. A yellow-cast sheet's own paper colour
        # (179, 171, 158) is scaled to the reference before the whitening, so its paper turns pure white.
        out = []
        for band in (1, 2, 3):
            arr = np.zeros((1, 1), dtype=np.uint8)
            sanborn_paper.clean([np.full((1, 1), v, np.uint8) for v in (179, 171, 158)], arr, 0, 0, 1, 1, 1, 1, 0, None,
                                band=str(band), paper=b"179,171,158")
            out.append(int(arr[0, 0]))
        self.assertEqual(tuple(out), (255, 255, 255))

    def test_a_pale_pink_is_still_the_full_fill_colour(self):
        # A pale printing's pink gets the same fill colour, with or without strength; paper stays white.
        def clean_with(rgb, **kw):
            out = []
            for band in (1, 2, 3):
                arr = np.zeros((1, 1), dtype=np.uint8)
                sanborn_paper.clean([np.full((1, 1), v, np.uint8) for v in rgb], arr, 0, 0, 1, 1, 1, 1, 0, None,
                                    band=str(band), **kw)
                out.append(int(arr[0, 0]))
            return tuple(out)
        pale = (160, 128, 144)
        for kw in ({}, {"strength": b"1.5"}):
            self.assertTrue(all(abs(o - t) <= 2 for o, t in zip(clean_with(pale, **kw), (226, 156, 172))))
        self.assertEqual(clean_with((178, 178, 178), strength=b"1.3"), (255, 255, 255))

    def test_shadowed_paper_turns_white_against_the_local_paper_map(self):
        # Joel 2026-09-29: no dingy white. Left half clean paper, right half a shadow 15% darker; the map knows both.
        block = np.zeros((3, 8, 8), np.uint8)
        block[:, :, :4] = np.array([190, 183, 172])[:, None, None]
        block[:, :, 4:] = np.array([160, 154, 146])[:, None, None]
        background = "2,2;190,183,172,160,154,146,190,183,172,160,154,146"
        for band in (1, 2, 3):
            out = np.zeros((8, 8), np.uint8)
            sanborn_paper.clean(list(block), out, 0, 0, 8, 8, 8, 8, 0, None, band=str(band), paper=b"190,183,172",
                                strength=b"1.5", background=background.encode())
            self.assertTrue((out == 255).all(), out)
        # Without the map the shadow stays dingy (the defect this fixes).
        out = np.zeros((8, 8), np.uint8)
        sanborn_paper.clean(list(block), out, 0, 0, 8, 8, 8, 8, 0, None, band="1", paper=b"190,183,172", strength=b"1.5")
        self.assertLess(int(out[0, 7]), 250)

    def test_without_a_paper_reading_the_formula_is_unchanged(self):
        rng = np.random.default_rng(29)
        inputs = [rng.integers(0, 256, size=(16, 16), dtype=np.uint8) for _ in range(3)]
        for band in (1, 2, 3):
            a = np.zeros((16, 16), dtype=np.uint8); b = np.zeros((16, 16), dtype=np.uint8)
            sanborn_paper.clean(inputs, a, 0, 0, 16, 16, 16, 16, 0, None, band=str(band))
            proven_clean(inputs, b, band)
            np.testing.assert_array_equal(a, b)

    def test_named_constants_reproduce_the_proven_formula_exactly(self):
        rng = np.random.default_rng(1911)
        inputs = [rng.integers(0, 256, size=(64, 64), dtype=np.uint8) for _ in range(3)]
        for band in (1, 2, 3):
            ours = np.zeros((64, 64), dtype=np.uint8)
            proven = np.zeros((64, 64), dtype=np.uint8)
            sanborn_paper.clean(inputs, ours, 0, 0, 64, 64, 64, 64, 0, None, band=str(band))
            proven_clean(inputs, proven, band)
            np.testing.assert_array_equal(ours, proven)

    def test_module_top_level_needs_only_numpy_and_the_standard_library(self):
        tree = ast.parse(Path(sanborn_paper.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        allowed = {"__future__", "argparse", "json", "os", "pathlib", "re", "shutil",
                   "subprocess", "sys", "tempfile", "xml", "numpy"}
        self.assertLessEqual(imported, allowed)


class RecipeFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.tif = self.folder / "Sanborn 1911 -- Tile 151_georeferenced.tif"
        self.tif.write_bytes(b"stand-in sheet")
        self.facts = dict(SHEET_FACTS)
        self.patch = mock.patch.object(sanborn_paper, "read_raster_facts", side_effect=lambda tif: self.facts)
        self.patch.start()
        self.paper_patch = mock.patch.object(sanborn_paper, "measure_paper", return_value=(178, 178, 178))
        self.paper_patch.start()
        self.strength_patch = mock.patch.object(sanborn_paper, "measure_strength", return_value=1.0)
        self.strength_patch.start()
        self.addCleanup(self.strength_patch.stop)
        self.background_patch = mock.patch.object(sanborn_paper, "measure_background", return_value=None)
        self.background_patch.start()
        self.addCleanup(self.background_patch.stop)

    def tearDown(self):
        self.paper_patch.stop()
        self.patch.stop()
        self.temp.cleanup()

    def test_recipe_path_sits_beside_its_tiff(self):
        vrt = sanborn_paper.vrt_path_for(self.tif)
        self.assertEqual(vrt.name, "Sanborn 1911 -- Tile 151_georeferenced.clean.vrt")
        self.assertEqual(vrt.parent, self.tif.parent)
        self.assertEqual(sanborn_paper.tif_path_for(vrt), self.tif)
        with self.assertRaises(sanborn_paper.RecipeError):
            sanborn_paper.vrt_path_for(self.folder / "sheet.jp2")

    def test_recipe_text_is_deterministic_relative_and_has_no_inline_code(self):
        xml = sanborn_paper.render_vrt_xml(self.tif)
        self.assertEqual(xml, sanborn_paper.render_vrt_xml(self.tif))
        self.assertNotIn("PixelFunctionCode", xml)
        self.assertNotIn(str(self.folder), xml)
        root = ET.fromstring(xml)
        self.assertEqual((root.get("rasterXSize"), root.get("rasterYSize")), ("6806", "7925"))
        self.assertEqual(root.findtext("SRS"), "EPSG:3857")
        self.assertEqual(
            [float(value) for value in root.findtext("GeoTransform").split(",")],
            SHEET_FACTS["geotransform"],
        )
        bands = root.findall("VRTRasterBand")
        self.assertEqual([band.get("band") for band in bands], ["1", "2", "3", "4"])
        for band, color in zip(bands[:3], ("Red", "Green", "Blue")):
            self.assertEqual(band.get("subClass"), "VRTDerivedRasterBand")
            self.assertEqual(band.get("dataType"), "Byte")
            self.assertEqual(band.findtext("ColorInterp"), color)
            self.assertEqual(band.findtext("PixelFunctionLanguage"), "Python")
            self.assertEqual(band.findtext("PixelFunctionType"), "sanborn_paper.clean")
            self.assertEqual(band.find("PixelFunctionArguments").get("band"), band.get("band"))
            sources = band.findall("SimpleSource")
            self.assertEqual([source.findtext("SourceBand") for source in sources], ["1", "2", "3"])
            for source in sources:
                self.assertEqual(source.find("SourceFilename").get("relativeToVRT"), "1")
                self.assertEqual(source.findtext("SourceFilename"), self.tif.name)
        alpha = bands[3]
        self.assertIsNone(alpha.get("subClass"))
        self.assertEqual(alpha.findtext("ColorInterp"), "Alpha")
        self.assertIsNone(alpha.find("PixelFunctionType"))
        self.assertEqual([source.findtext("SourceBand") for source in alpha.findall("SimpleSource")], ["4"])

    def test_a_crs_without_an_epsg_code_is_copied_whole(self):
        self.facts["wkt"] = 'LOCAL_CS["sheet"]'
        self.assertIn('<SRS dataAxisToSRSAxisMapping="1,2">LOCAL_CS["sheet"]</SRS>',
                      sanborn_paper.render_vrt_xml(self.tif))

    def test_only_rgba_byte_sheets_get_a_recipe(self):
        self.facts["bands"] = SHEET_FACTS["bands"][:3]
        with self.assertRaisesRegex(sanborn_paper.RecipeError, "alpha"):
            sanborn_paper.render_vrt_xml(self.tif)

    def test_write_is_atomic_idempotent_and_read_only(self):
        vrt = sanborn_paper.write_vrt(self.tif)
        self.assertEqual(vrt.read_text(encoding="utf-8"), sanborn_paper.render_vrt_xml(self.tif))
        self.assertEqual(vrt.stat().st_mode & 0o777, 0o444)
        before = vrt.stat()
        self.assertEqual(sanborn_paper.write_vrt(self.tif), vrt)
        after = vrt.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), sorted([self.tif.name, vrt.name]))
        self.assertEqual(self.tif.read_bytes(), b"stand-in sheet")

    def test_verify_rejects_edited_missing_outdated_or_mismatched_recipes(self):
        vrt = sanborn_paper.vrt_path_for(self.tif)
        with self.assertRaisesRegex(sanborn_paper.RecipeError, "missing"):
            sanborn_paper.verify_vrt(vrt, self.tif)
        sanborn_paper.write_vrt(self.tif)
        sanborn_paper.verify_vrt(vrt, self.tif)

        os.chmod(vrt, 0o644)
        vrt.write_text(vrt.read_text(encoding="utf-8").replace("sanborn_paper.clean", "other.clean"), encoding="utf-8")
        with self.assertRaisesRegex(sanborn_paper.RecipeError, "does not match"):
            sanborn_paper.verify_vrt(vrt, self.tif)
        sanborn_paper.write_vrt(self.tif)
        sanborn_paper.verify_vrt(vrt, self.tif)

        self.facts["geotransform"] = [value + 1.0 for value in SHEET_FACTS["geotransform"]]
        with self.assertRaisesRegex(sanborn_paper.RecipeError, "does not match"):
            sanborn_paper.verify_vrt(vrt, self.tif)
        sanborn_paper.write_vrt(self.tif)
        sanborn_paper.verify_vrt(vrt, self.tif)

        other = self.folder / "Sanborn 1911 -- Tile 152_georeferenced.tif"
        with self.assertRaisesRegex(sanborn_paper.RecipeError, "not the paper recipe file"):
            sanborn_paper.verify_vrt(vrt, other)

    def test_backfill_counts_and_dry_run_writes_nothing(self):
        second = self.folder / "Sanborn 1911 -- Tile 152_georeferenced.tif"
        second.write_bytes(b"second")
        (self.folder / "Sanborn 1911 -- Tile 486_georeferenced alpha.tif").write_bytes(b"not a sheet name")
        sanborn_paper.write_vrt(second)
        stale = sanborn_paper.vrt_path_for(second)
        os.chmod(stale, 0o644)
        stale.write_text("stale", encoding="utf-8")

        out = io.StringIO()
        counts = sanborn_paper.backfill(self.folder, dry_run=True, out=out)
        self.assertEqual(counts, {"sheets": 2, "current": 0, "missing": 1, "outdated": 1, "failed": 0})
        self.assertFalse(sanborn_paper.vrt_path_for(self.tif).exists())
        self.assertEqual(stale.read_text(encoding="utf-8"), "stale")
        self.assertIn("would write 1 missing and 1 outdated", out.getvalue())

        counts = sanborn_paper.backfill(self.folder, out=io.StringIO())
        self.assertEqual(counts["missing"] + counts["outdated"], 2)
        counts = sanborn_paper.backfill(self.folder, out=io.StringIO())
        self.assertEqual(counts, {"sheets": 2, "current": 2, "missing": 0, "outdated": 0, "failed": 0})

        self.facts["bands"] = SHEET_FACTS["bands"][:3]
        out = io.StringIO()
        counts = sanborn_paper.backfill(self.folder, out=out)
        self.assertEqual(counts["failed"], 2)
        self.assertIn("FAILED", out.getvalue())

    def test_install_module_copies_this_module_atomically(self):
        target = self.folder / "profile" / "python" / "sanborn_paper.py"
        self.assertEqual(sanborn_paper.install_module(target), target)
        self.assertEqual(target.read_bytes(), Path(sanborn_paper.__file__).read_bytes())
        self.assertEqual(sorted(path.name for path in target.parent.iterdir()), ["sanborn_paper.py"])
        self.assertEqual(
            sanborn_paper.QGIS_MODULE_PATH,
            Path.home() / "Library/Application Support/QGIS/QGIS3/profiles/default/python/sanborn_paper.py",
        )


@unittest.skipUnless(
    shutil.which("gdal_translate") and shutil.which("gdalinfo") and (QGIS_BIN / "python3").exists(),
    "needs GDAL on PATH and QGIS's Python",
)
class RealGdalRecipeTests(unittest.TestCase):
    """Write a recipe for a small real GeoTIFF and read it through QGIS's own GDAL."""

    def test_recipe_whitens_paper_and_passes_alpha_through_in_qgis_gdal(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            pixels = np.zeros((4, 4, 4), dtype=np.uint8)
            pixels[..., :3] = (197, 190, 181)
            pixels[..., 3] = 255
            pixels[0, 0, :3] = (162, 126, 132)
            pixels[3, 3, 3] = 0
            Image.fromarray(pixels, "RGBA").save(folder / "sheet.png")
            tif = folder / "Sanborn 1911 -- Tile 9_georeferenced.tif"
            subprocess.run(
                ["gdal_translate", "-q", "-a_srs", "EPSG:3857",
                 "-a_ullr", "-9394210", "3996350", "-9394200", "3996340",
                 "-colorinterp", "red,green,blue,alpha", str(folder / "sheet.png"), str(tif)],
                check=True,
            )
            vrt = sanborn_paper.write_vrt(tif)
            sanborn_paper.verify_vrt(vrt, tif)
            self.assertIn("<SRS dataAxisToSRSAxisMapping=\"1,2\">EPSG:3857</SRS>", vrt.read_text(encoding="utf-8"))

            script = (
                "import sys\n"
                "from osgeo import gdal\n"
                "ds = gdal.Open(sys.argv[1])\n"
                "print([ds.GetRasterBand(b).ReadAsArray().tolist() for b in (1, 2, 3, 4)])\n"
            )
            resources = QGIS_BIN.parents[1] / "Resources"
            env = dict(os.environ, PYTHONPATH=str(Path(sanborn_paper.__file__).parent),
                       GDAL_VRT_ENABLE_PYTHON="TRUSTED_MODULES",
                       GDAL_VRT_PYTHON_TRUSTED_MODULES="sanborn_paper",
                       PROJ_LIB=str(resources / "proj"), GDAL_DATA=str(resources / "gdal"))
            result = subprocess.run([str(QGIS_BIN / "python3"), "-c", script, str(vrt)],
                                    check=True, capture_output=True, text=True, env=env)
            red, green, blue, alpha = ast.literal_eval(result.stdout.strip().splitlines()[-1])
            self.assertEqual((red[1][1], green[1][1], blue[1][1]), (255, 255, 255))
            self.assertEqual((red[0][0], green[0][0], blue[0][0]), (226, 156, 172))  # pink fill #e29cac
            self.assertEqual((alpha[3][3], alpha[1][1]), (0, 255))
            self.assertEqual(vrt.read_text(encoding="utf-8"), sanborn_paper.render_vrt_xml(tif))


if __name__ == "__main__":
    unittest.main()
