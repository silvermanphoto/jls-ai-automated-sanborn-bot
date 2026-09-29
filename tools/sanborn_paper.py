#!/usr/bin/env python3
"""Whiten Sanborn sheet paper in QGIS without rewriting any TIFF.

Each georeferenced sheet ``X_georeferenced.tif`` gets a small recipe file
beside it, ``X_georeferenced.clean.vrt``. The recipe is a GDAL virtual raster
whose red, green and blue bands are computed at draw time by ``clean`` below;
band 4 passes the TIFF's alpha straight through. QGIS loads the recipe instead
of the TIFF, so the scan on disk is never changed.

QGIS must run with two environment settings, or GDAL refuses the recipe:

    GDAL_VRT_ENABLE_PYTHON=TRUSTED_MODULES
    GDAL_VRT_PYTHON_TRUSTED_MODULES=sanborn_paper

and this file must be importable inside QGIS (``install-module`` copies it into
the QGIS profile). QGIS runs Python 3.9 with numpy, so everything at module top
level uses only the standard library and numpy; GDAL is reached only inside
functions, through the ``gdalinfo`` program.

To change the look, edit the constants below, then run ``install-module`` and
``backfill`` again.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape, quoteattr

import numpy as np


# --- The look. Levels 46 / 1.56 / 205, then near-grey bright paper to white. ---
LEVELS_BLACK = 46.0  # input value that becomes pure black (levels black point)
LEVELS_RANGE = 159  # input span from black to white, so the white point is 205
LEVELS_EXPONENT = 0.641  # midtone 1.56, applied as the power 1 / 1.56
PAPER_SPREAD_LIMIT = 30  # channel spread (brightest minus dimmest) where paper weight reaches 0
PAPER_SPREAD_FADE = 12  # fade width: a spread of 18 or less counts fully as paper
PAPER_BRIGHT_START = 140  # brightest channel at or below which a pixel is never paper
PAPER_BRIGHT_FADE = 25  # fade width: a brightest channel of 165 or more counts fully as paper

# Per-sheet paper balance (Joel, 2026-09-29: 'whiteness levels of all tiles match'). Each recipe carries its sheet's
# measured paper colour; the pixel function scales every channel so that colour becomes PAPER_REFERENCE first.
PAPER_REFERENCE = 178.0  # typical bright paper in the scans
PAPER_SAMPLE_DIVISOR = 16  # measure paper on a 1/16-size read of the sheet

MODULE_NAME = "sanborn_paper"
PIXEL_FUNCTION = MODULE_NAME + ".clean"
VRT_SUFFIX = ".clean.vrt"
QGIS_MODULE_PATH = (
    Path.home()
    / "Library/Application Support/QGIS/QGIS3/profiles/default/python"
    / (MODULE_NAME + ".py")
)
QGIS_GDALINFO = Path("/Applications/QGIS.app/Contents/MacOS/bin/gdalinfo")
QGIS_ENVIRONMENT = {
    "GDAL_VRT_ENABLE_PYTHON": "TRUSTED_MODULES",
    "GDAL_VRT_PYTHON_TRUSTED_MODULES": MODULE_NAME,
}


def clean(in_ar, out_ar, xoff, yoff, xsize, ysize, raster_xsize, raster_ysize, buf_radius, gt, band, **kw):
    """GDAL pixel function: levels every channel, then lift near-grey bright paper to white."""
    src = [a.astype(np.float32) for a in in_ar[:3]]
    paper_rgb = kw.get("paper")
    if paper_rgb:
        if isinstance(paper_rgb, bytes):  # GDAL hands pixel-function arguments over as bytes
            paper_rgb = paper_rgb.decode("ascii")
        ref = [float(v) for v in str(paper_rgb).split(",")]
        src = [np.clip(c * (PAPER_REFERENCE / max(ref[i], 1.0)), 0, 255) for i, c in enumerate(src)]
    r, g, b = src
    mx = np.maximum(np.maximum(r, g), b); mn = np.minimum(np.minimum(r, g), b)
    paper = np.clip((PAPER_SPREAD_LIMIT - (mx - mn)) / PAPER_SPREAD_FADE, 0, 1) * np.clip((mx - PAPER_BRIGHT_START) / PAPER_BRIGHT_FADE, 0, 1)
    lev = (np.clip((src[int(band) - 1] - LEVELS_BLACK) / LEVELS_RANGE, 0, 1) ** LEVELS_EXPONENT) * 255
    out_ar[:] = lev + (255 - lev) * paper


class RecipeError(ValueError):
    """A recipe file cannot be made, or does not match its TIFF."""


def vrt_path_for(tif: Path | str) -> Path:
    """``X_georeferenced.tif`` -> ``X_georeferenced.clean.vrt`` in the same folder."""
    tif = Path(tif)
    if tif.suffix.lower() != ".tif":
        raise RecipeError(f"A paper recipe is made only for a .tif sheet: {tif}")
    return tif.with_name(tif.name[: -len(tif.suffix)] + VRT_SUFFIX)


def tif_path_for(vrt: Path | str) -> Path:
    """The TIFF a recipe file belongs to (the reverse of ``vrt_path_for``)."""
    vrt = Path(vrt)
    if not vrt.name.endswith(VRT_SUFFIX):
        raise RecipeError(f"Not a paper recipe file: {vrt}")
    return vrt.with_name(vrt.name[: -len(VRT_SUFFIX)] + ".tif")


def _gdalinfo() -> tuple[str, dict[str, str]]:
    found = shutil.which("gdalinfo")
    if not found and QGIS_GDALINFO.is_file():
        found = str(QGIS_GDALINFO)
    if not found:
        raise RecipeError("GDAL's gdalinfo program is unavailable; install QGIS or put gdalinfo on PATH")
    env = dict(os.environ)
    resources = Path(found).resolve().parents[2] / "Resources"
    if (resources / "proj").is_dir():
        # QGIS's own GDAL needs its bundled PROJ and GDAL data to name the CRS.
        env.setdefault("PROJ_LIB", str(resources / "proj"))
        env.setdefault("PROJ_DATA", str(resources / "proj"))
        env.setdefault("GDAL_DATA", str(resources / "gdal"))
    return found, env


def read_raster_facts(tif: Path | str) -> dict:
    """Size, CRS, geotransform and band layout of a sheet TIFF, read by gdalinfo."""
    program, env = _gdalinfo()
    try:
        result = subprocess.run(
            [program, "-json", str(tif)], check=True, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        info = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise RecipeError(f"GDAL cannot read {tif}: {exc}") from exc
    system = info.get("coordinateSystem") or {}
    return {
        "width": int(info["size"][0]),
        "height": int(info["size"][1]),
        "wkt": str(system.get("wkt", "")),
        "axis_mapping": [int(v) for v in system.get("dataAxisToSRSAxisMapping", [1, 2])],
        "geotransform": [float(v) for v in info.get("geoTransform", [])],
        "bands": [
            {"type": band.get("type"), "color": band.get("colorInterpretation")}
            for band in info.get("bands", [])
        ],
    }


_EPSG_ID = re.compile(r'(?:ID\["EPSG",\s*(\d+)\]|AUTHORITY\["EPSG",\s*"(\d+)"\])\]\s*$')


def _srs_text(wkt: str) -> str:
    """``EPSG:n`` when GDAL names the whole CRS by an EPSG code, else the WKT itself.

    The code keeps the recipe identical whichever GDAL version wrote it.
    """
    match = _EPSG_ID.search(wkt.strip())
    if match:
        return "EPSG:" + (match.group(1) or match.group(2))
    return wkt.strip()


def measure_paper(tif: Path | str, width: int | None = None, height: int | None = None) -> tuple[int, int, int]:
    """Median red, green and blue of the sheet's bright near-grey paper, from a small read by gdal_translate."""
    program, env = _gdalinfo()
    translate = str(Path(program).with_name("gdal_translate"))
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "sample"
        if width is None or height is None:
            facts = read_raster_facts(tif); width, height = facts["width"], facts["height"]
        sw = str(min(width, max(8, width // PAPER_SAMPLE_DIVISOR))); sh = str(min(height, max(8, height // PAPER_SAMPLE_DIVISOR)))
        try:
            subprocess.run([translate, "-q", "-of", "ENVI", "-outsize", sw, sh, "-r", "nearest", str(tif), str(raw)],
                           check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            header = (Path(tmp) / "sample.hdr").read_text()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RecipeError(f"GDAL cannot sample {tif}: {exc}") from exc
        w = int(re.search(r"samples\s*=\s*(\d+)", header).group(1)); h = int(re.search(r"lines\s*=\s*(\d+)", header).group(1))
        data = np.fromfile(raw, dtype=np.uint8).reshape(-1, h, w)
    r, g, b, a = (data[i].astype(np.int16) for i in range(4))
    mx = np.maximum(np.maximum(r, g), b); mn = np.minimum(np.minimum(r, g), b)
    paper = (a > 0) & (mx - mn < 35) & (mx > 120) & (mx < 250)
    if paper.sum() < 200:
        return (int(PAPER_REFERENCE),) * 3
    return tuple(int(np.median(c[paper])) for c in (r, g, b))


def render_vrt_xml(tif: Path | str) -> str:
    """The exact recipe text for one sheet TIFF. Same TIFF, same text."""
    tif = Path(tif)
    vrt_path_for(tif)
    facts = read_raster_facts(tif)
    bands = facts["bands"]
    if (
        len(bands) != 4
        or any(band["type"] != "Byte" for band in bands)
        or [band["color"] for band in bands] != ["Red", "Green", "Blue", "Alpha"]
    ):
        raise RecipeError(f"Not an 8-bit red, green, blue and alpha sheet: {tif}")
    if len(facts["geotransform"]) != 6 or not facts["wkt"]:
        raise RecipeError(f"Sheet has no map position or CRS: {tif}")
    source = escape(tif.name)
    srs = escape(_srs_text(facts["wkt"]))
    mapping = ",".join(str(value) for value in facts["axis_mapping"])
    geotransform = ", ".join(repr(value) for value in facts["geotransform"])
    paper_arg = ",".join(str(v) for v in measure_paper(tif, facts["width"], facts["height"]))

    def simple_source(source_band: int) -> str:
        return (
            "    <SimpleSource>\n"
            f'      <SourceFilename relativeToVRT="1">{source}</SourceFilename>\n'
            f"      <SourceBand>{source_band}</SourceBand>\n"
            "    </SimpleSource>\n"
        )

    lines = [
        f'<VRTDataset rasterXSize="{facts["width"]}" rasterYSize="{facts["height"]}">\n',
        f"  <SRS dataAxisToSRSAxisMapping={quoteattr(mapping)}>{srs}</SRS>\n",
        f"  <GeoTransform>{geotransform}</GeoTransform>\n",
    ]
    for band, color in ((1, "Red"), (2, "Green"), (3, "Blue")):
        lines.append(f'  <VRTRasterBand dataType="Byte" band="{band}" subClass="VRTDerivedRasterBand">\n')
        lines.append(f"    <ColorInterp>{color}</ColorInterp>\n")
        lines.extend(simple_source(source_band) for source_band in (1, 2, 3))
        lines.append("    <PixelFunctionLanguage>Python</PixelFunctionLanguage>\n")
        lines.append(f"    <PixelFunctionType>{PIXEL_FUNCTION}</PixelFunctionType>\n")
        lines.append(f'    <PixelFunctionArguments band="{band}" paper="{paper_arg}"/>\n')
        lines.append("  </VRTRasterBand>\n")
    lines.append('  <VRTRasterBand dataType="Byte" band="4">\n')
    lines.append("    <ColorInterp>Alpha</ColorInterp>\n")
    lines.append(simple_source(4))
    lines.append("  </VRTRasterBand>\n")
    lines.append("</VRTDataset>\n")
    return "".join(lines)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def write_vrt(tif: Path | str) -> Path:
    """Write (or leave alone, when already current) the recipe beside ``tif``."""
    tif = Path(tif)
    vrt = vrt_path_for(tif)
    xml = render_vrt_xml(tif)
    if _read_text(vrt) == xml:
        return vrt
    handle, temporary = tempfile.mkstemp(prefix="." + vrt.name + ".", suffix=".tmp", dir=vrt.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(xml)
            stream.flush()
            os.fsync(stream.fileno())
        # Read-only: GDAL rewrites a writable VRT whenever QGIS computes
        # statistics, which would break the exact-content check.
        os.chmod(temporary, 0o444)
        os.replace(temporary, vrt)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return vrt


def verify_vrt(vrt: Path | str, tif: Path | str) -> None:
    """Raise ``RecipeError`` unless ``vrt`` is exactly the current recipe for ``tif``."""
    vrt, tif = Path(vrt), Path(tif)
    if vrt.resolve() != vrt_path_for(tif).resolve():
        raise RecipeError(f"{vrt.name} is not the paper recipe file for {tif.name}")
    actual = _read_text(vrt)
    if actual is None:
        raise RecipeError(f"The paper recipe file is missing: {vrt}")
    if actual != render_vrt_xml(tif):
        raise RecipeError(f"The paper recipe file does not match its sheet (edited or outdated): {vrt}")


def backfill(folder: Path, *, dry_run: bool = False, out=sys.stdout) -> dict[str, int]:
    """Write missing or outdated recipes for every ``*_georeferenced.tif`` in ``folder``."""
    counts = {"sheets": 0, "current": 0, "missing": 0, "outdated": 0, "failed": 0}
    for tif in sorted(Path(folder).glob("*_georeferenced.tif")):
        counts["sheets"] += 1
        vrt = vrt_path_for(tif)
        try:
            existing = _read_text(vrt)
            status = "current" if existing == render_vrt_xml(tif) else ("missing" if existing is None else "outdated")
            if status != "current" and not dry_run:
                write_vrt(tif)
        except (RecipeError, OSError) as exc:
            counts["failed"] += 1
            print(f"FAILED {tif.name}: {exc}", file=out)
            continue
        counts[status] += 1
        if status != "current":
            print(f"{'would write' if dry_run else 'wrote'} ({status}) {vrt.name}", file=out)
    verb = "would write" if dry_run else "wrote"
    print(
        f"{counts['sheets']} sheets: {counts['current']} already current, "
        f"{verb} {counts['missing']} missing and {counts['outdated']} outdated, "
        f"{counts['failed']} failed",
        file=out,
    )
    return counts


def install_module(destination: Path = QGIS_MODULE_PATH) -> Path:
    """Copy this module where QGIS's Python can import it, atomically."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="." + destination.name + ".", suffix=".tmp", dir=destination.parent)
    os.close(handle)
    try:
        shutil.copyfile(Path(__file__).resolve(), temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper-whitening recipe files for Sanborn sheets.")
    commands = parser.add_subparsers(dest="command", required=True)
    fill = commands.add_parser("backfill", help="write missing or outdated recipe files beside every *_georeferenced.tif")
    fill.add_argument("folder", type=Path)
    fill.add_argument("--dry-run", action="store_true", help="report what would be written; write nothing")
    commands.add_parser("install-module", help="copy this module into the QGIS profile so GDAL can use it")
    args = parser.parse_args(argv)
    if args.command == "backfill":
        if not args.folder.is_dir():
            print(f"Not a folder: {args.folder}", file=sys.stderr)
            return 2
        counts = backfill(args.folder, dry_run=args.dry_run)
        return 1 if counts["failed"] else 0
    target = install_module()
    print(f"Installed the paper module for QGIS at {target}")
    print("QGIS also needs these environment settings (Settings > Options > System > Environment), then a restart:")
    for key, value in QGIS_ENVIRONMENT.items():
        print(f"  {key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
