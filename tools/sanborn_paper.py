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


# --- The look (v1.26, Joel 2026-09-29: "dense pinks and yellows, crisp deep black text"), fitted to Joel's example of
# sheet 151: per-channel levels (black point, white point, gamma), dark ink pulled to neutral black, then near-grey
# bright paper to white. The v1.24 look (levels 46 / 1.56 / 205 on every channel) washed the pinks out.
LOOK_BLACK = (28.4, 0.0, 0.0)  # red, green, blue input values that become 0
LOOK_WHITE = (182.8, 163.8, 183.3)  # input values that become 255
LOOK_GAMMA = (0.793, 1.358, 1.144)  # output = 255 * ((in - black) / (white - black)) ** gamma
INK_FULL = 60.0  # a pixel whose brightest balanced channel is at or below this is ink: darkened to 20% (near black)
INK_NONE = 105.0  # at or above this nothing is darkened; the ink weight fades linearly between the two
INK_KEEP = 0.2  # share of the look's value that full ink keeps
PAPER_SPREAD_LIMIT = 30  # channel spread (brightest minus dimmest) where paper weight reaches 0
PAPER_SPREAD_FADE = 12  # fade width: a spread of 18 or less counts fully as paper
PAPER_BRIGHT_START = 140  # brightest channel at or below which a pixel is never paper
PAPER_BRIGHT_FADE = 25  # fade width: a brightest channel of 165 or more counts fully as paper

# Flat fills (v1.27, Joel 2026-09-29): every pink, orange, yellow and blue fill is one even colour on every sheet.
# A fill pixel is recognised by its hue (angle of r-g, (r+g)/2-b after paper balance and strength) and saturation, and
# replaced by Joel's target colour; ink mixed into it (darker than the fill) keeps its share of the look, so lines stay.
FILLS = (  # hue centre (deg), half width (deg), target RGB, fill lightness (mean of balanced channels)
    (0.0, 27.0, (226, 156, 172), 134.0),   # pink   #e29cac
    (52.0, 14.0, (218, 161, 90), 114.0),   # orange #daa15a
    (82.0, 14.0, (237, 210, 77), 130.0),   # yellow #edd24d
    (230.0, 40.0, (137, 178, 199), 110.0),  # blue   #89b2c7
)
FILL_HUE_FADE = 5.0  # degrees over which a fill class fades at its hue edge
FILL_SAT_LOW, FILL_SAT_HIGH = 14.0, 24.0  # saturation at which fill weight starts, and reaches full
# Pixels counted as paper (the paper lift weight) are never fill, so stains and yellowed paper are not tinted.
FILL_INK_GAP = 12.0  # a fill pixel up to this much darker than its fill lightness is pure fill
FILL_INK_L = 60.0  # lightness of pure ink (balanced scan ink is about 50-60)

# Per-sheet colour strength (v1.26): pale printings are deepened to sheet 151's density. Strength scales every
# channel's distance below paper white; it is measured from the sheet's pink (green channel) and yellow (blue channel).
STRENGTH_PINK_GREEN = 178.0 - 116.4  # sheet 151's pink: green channel distance below balanced paper
STRENGTH_YELLOW_BLUE = 178.0 - 85.9  # sheet 151's yellow: blue channel distance below balanced paper
STRENGTH_LIMITS = (0.85, 1.5)
STRENGTH_SPREAD = (10.0, 30.0)  # strength applies fully only to coloured pixels (channel spread 30+), never to grey paper

# Local paper (v1.28, Joel 2026-09-29: "still areas of dingy white"): shadows, stains and fold lines leave paper darker
# than the sheet's overall paper. Each recipe carries a coarse map of the paper colour across the sheet (median of
# near-grey bright pixels per cell, gaps filled from neighbours, smoothed); every pixel is balanced against the paper
# around it. The map is indexed by position on the sheet, so it works at every zoom level.
BACKGROUND_COLUMNS = 32  # cells across the sheet; rows follow the sheet's shape
BACKGROUND_FLOOR = 0.8  # local paper is never taken as darker than this share of the sheet's overall paper
BACKGROUND_MIN_SHARE = 0.3  # a cell counts only when paper covers this share of it (dense blocks borrow from neighbours)
BACKGROUND_MAX_TINT = 8.0  # paper candidates must be this neutral after balancing to the sheet's paper (pale pink wash is not)

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


_BACKGROUNDS: dict = {}


def _parse_background(text):
    """'rows,cols;r,g,b,r,g,b,...' -> float array rows x cols x 3 (cached: GDAL calls once per block)."""
    grid = _BACKGROUNDS.get(text)
    if grid is None:
        head, values = text.split(";", 1)
        rows, cols = (int(v) for v in head.split(","))
        grid = np.array([float(v) for v in values.split(",")], np.float32).reshape(rows, cols, 3)
        _BACKGROUNDS[text] = grid
    return grid


def _local_paper(text, xoff, yoff, xsize, ysize, shape):
    """Paper colour around every pixel of the block, bilinear from the background map; None without a map.
    GDAL passes the full-resolution window (xoff, yoff, xsize, ysize) even when it reads a reduced buffer."""
    if not text or xsize is None or not xsize or not ysize:
        return None
    try:
        raster = _local_paper.raster  # set by clean() from raster_xsize, raster_ysize
    except AttributeError:
        return None
    grid = _parse_background(text)
    rows, cols = grid.shape[:2]
    h, w = shape
    fx = (xoff + (np.arange(w, dtype=np.float32) + 0.5) * (xsize / w)) / raster[0] * cols - 0.5
    fy = (yoff + (np.arange(h, dtype=np.float32) + 0.5) * (ysize / h)) / raster[1] * rows - 0.5
    fx = np.clip(fx, 0, cols - 1); fy = np.clip(fy, 0, rows - 1)
    x0 = np.minimum(fx.astype(int), cols - 2 if cols > 1 else 0); y0 = np.minimum(fy.astype(int), rows - 2 if rows > 1 else 0)
    x1 = np.minimum(x0 + 1, cols - 1); y1 = np.minimum(y0 + 1, rows - 1)
    tx = (fx - x0)[None, :]; ty = (fy - y0)[:, None]
    out = []
    for c in range(3):
        g = grid[..., c]
        top = g[y0][:, x0] * (1 - tx) + g[y0][:, x1] * tx
        bottom = g[y1][:, x0] * (1 - tx) + g[y1][:, x1] * tx
        out.append(top * (1 - ty) + bottom * ty)
    return out


def clean(in_ar, out_ar, xoff, yoff, xsize, ysize, raster_xsize, raster_ysize, buf_radius, gt, band, **kw):
    """GDAL pixel function: balance paper (locally), deepen colour, apply the look, paper to white, flatten fills."""
    def arg(name):
        v = kw.get(name)
        return v.decode("ascii") if isinstance(v, bytes) else v  # GDAL hands arguments over as bytes
    _local_paper.raster = (raster_xsize, raster_ysize)
    src = [a.astype(np.float32) for a in in_ar[:3]]
    paper_rgb = arg("paper")
    local = _local_paper(arg("background"), xoff, yoff, xsize, ysize, src[0].shape)
    if local is not None:
        src = [np.clip(c * (PAPER_REFERENCE / np.maximum(local[i], 1.0)), 0, 255) for i, c in enumerate(src)]
    elif paper_rgb:
        ref = [float(v) for v in str(paper_rgb).split(",")]
        src = [np.clip(c * (PAPER_REFERENCE / max(ref[i], 1.0)), 0, 255) for i, c in enumerate(src)]
    strength = float(arg("strength") or 1.0)
    if strength != 1.0:
        spread = np.maximum(np.maximum(src[0], src[1]), src[2]) - np.minimum(np.minimum(src[0], src[1]), src[2])
        s_eff = 1 + (strength - 1) * np.clip((spread - STRENGTH_SPREAD[0]) / (STRENGTH_SPREAD[1] - STRENGTH_SPREAD[0]), 0, 1)
        src = [np.clip(PAPER_REFERENCE - s_eff * (PAPER_REFERENCE - c), 0, 255) for c in src]
    r, g, b = src
    mx = np.maximum(np.maximum(r, g), b); mn = np.minimum(np.minimum(r, g), b)
    paper = np.clip((PAPER_SPREAD_LIMIT - (mx - mn)) / PAPER_SPREAD_FADE, 0, 1) * np.clip((mx - PAPER_BRIGHT_START) / PAPER_BRIGHT_FADE, 0, 1)
    ink = np.clip((INK_NONE - mx) / (INK_NONE - INK_FULL), 0, 1)
    looks = []
    for i in range(3):
        lev = 255 * np.clip((src[i] - LOOK_BLACK[i]) / (LOOK_WHITE[i] - LOOK_BLACK[i]), 0, 1) ** LOOK_GAMMA[i]
        lev = lev * (1 - (1 - INK_KEEP) * ink)
        looks.append(lev + (255 - lev) * paper)
    c1 = r - g; c2 = (r + g) / 2 - b
    hue = np.degrees(np.arctan2(c2, c1)); light = (r + g + b) / 3
    sat_w = np.clip((np.hypot(c1, c2) - FILL_SAT_LOW) / (FILL_SAT_HIGH - FILL_SAT_LOW), 0, 1)
    i = int(band) - 1
    total = np.zeros_like(r); mixed = np.zeros_like(r)
    for centre, half, target, fill_light in FILLS:
        d = np.abs((hue - centre + 180) % 360 - 180)
        w = np.clip((half + FILL_HUE_FADE / 2 - d) / FILL_HUE_FADE, 0, 1)
        a = np.clip((fill_light - FILL_INK_GAP - light) / (fill_light - FILL_INK_GAP - FILL_INK_L), 0, 1)
        total += w; mixed += w * ((1 - a) * target[i] + a * looks[i])
    fill = np.clip(total, 0, 1) * sat_w * (1 - paper)
    out_ar[:] = fill * (mixed / np.maximum(total, 1e-6)) + (1 - fill) * looks[i]


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


_SAMPLES: dict = {}


def _sample(tif: Path | str, width: int | None, height: int | None) -> np.ndarray:
    key = (str(tif), os.path.getmtime(tif) if os.path.exists(tif) else None, width, height)
    if key not in _SAMPLES:
        _SAMPLES.clear()
        _SAMPLES[key] = _sample_uncached(tif, width, height)
    return _SAMPLES[key]


def _sample_uncached(tif: Path | str, width: int | None, height: int | None) -> np.ndarray:
    """A 1/16-size red, green, blue, alpha read of the sheet, via gdal_translate. The map position is dropped
    (-a_ullr) because the ENVI format refuses sheared geotransforms, such as the 1958-topo fits of 486, 493 and 494."""
    program, env = _gdalinfo()
    translate = str(Path(program).with_name("gdal_translate"))
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "sample"
        if width is None or height is None:
            facts = read_raster_facts(tif); width, height = facts["width"], facts["height"]
        sw = str(min(width, max(8, width // PAPER_SAMPLE_DIVISOR))); sh = str(min(height, max(8, height // PAPER_SAMPLE_DIVISOR)))
        try:
            subprocess.run([translate, "-q", "-of", "ENVI", "-outsize", sw, sh, "-r", "nearest", "-a_ullr", "0", "1", "1", "0", str(tif), str(raw)],
                           check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            header = (Path(tmp) / "sample.hdr").read_text()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RecipeError(f"GDAL cannot sample {tif}: {exc}") from exc
        w = int(re.search(r"samples\s*=\s*(\d+)", header).group(1)); h = int(re.search(r"lines\s*=\s*(\d+)", header).group(1))
        return np.fromfile(raw, dtype=np.uint8).reshape(-1, h, w)


def measure_paper(tif: Path | str, width: int | None = None, height: int | None = None) -> tuple[int, int, int]:
    """Median red, green and blue of the sheet's bright near-grey paper, from a small read by gdal_translate."""
    data = _sample(tif, width, height)
    r, g, b, a = (data[i].astype(np.int16) for i in range(4))
    mx = np.maximum(np.maximum(r, g), b); mn = np.minimum(np.minimum(r, g), b)
    paper = (a > 0) & (mx - mn < 35) & (mx > 120) & (mx < 250)
    if paper.sum() < 200:
        return (int(PAPER_REFERENCE),) * 3
    return tuple(int(np.median(c[paper])) for c in (r, g, b))


def measure_strength(tif: Path | str, paper_rgb, width: int | None = None, height: int | None = None) -> float:
    """How much to deepen this sheet's colour so its pinks and yellows match sheet 151 (1.0 = unchanged)."""
    data = _sample(tif, width, height)
    a = data[3] > 0
    r, g, b = (data[i].astype(np.float32)[a] * (PAPER_REFERENCE / max(float(paper_rgb[i]), 1.0)) for i in range(3))
    pink = (r - g > 25) & (b - g > 3) & (r > b + 8) & (r > 110) & (r < 200)
    yellow = (r > 120) & (g > 105) & (r - b > 45) & (g - b > 35) & (r - g < 40)
    votes = []
    if pink.sum() >= 50:
        votes.append((int(pink.sum()), STRENGTH_PINK_GREEN / max(PAPER_REFERENCE - float(np.median(g[pink])), 1.0)))
    if yellow.sum() >= 50:
        votes.append((int(yellow.sum()), STRENGTH_YELLOW_BLUE / max(PAPER_REFERENCE - float(np.median(b[yellow])), 1.0)))
    if not votes:
        return 1.0
    s = float(np.exp(sum(n * np.log(v) for n, v in votes) / sum(n for n, _ in votes)))
    return round(min(max(s, STRENGTH_LIMITS[0]), STRENGTH_LIMITS[1]), 3)


def measure_background(tif: Path | str, paper_rgb, width: int | None = None, height: int | None = None) -> str | None:
    """The local-paper map as recipe text 'rows,cols;r,g,b,...' (integers), or None when the sheet has too little paper."""
    data = _sample(tif, width, height)
    h, w = data.shape[1:]
    cols = BACKGROUND_COLUMNS; rows = max(2, int(round(cols * h / max(w, 1))))
    r, g, b, a = (data[i].astype(np.float32) for i in range(4))
    mx = np.maximum(np.maximum(r, g), b); mn = np.minimum(np.minimum(r, g), b)
    br, bg, bb = (c * (PAPER_REFERENCE / max(float(paper_rgb[i]), 1.0)) for i, c in enumerate((r, g, b)))
    tint = np.hypot(br - bg, (br + bg) / 2 - bb)
    paper = (a > 0) & (tint < BACKGROUND_MAX_TINT) & (mx > 100) & (mx < 252)
    if paper.sum() < 200:
        return None
    grid = np.full((rows, cols, 3), np.nan, np.float32)
    ys = np.minimum((np.arange(h) * rows) // h, rows - 1); xs = np.minimum((np.arange(w) * cols) // w, cols - 1)
    cell = ys[:, None] * cols + xs[None, :]
    for k in range(rows * cols):
        inside = (cell == k) & (a > 0)
        m = paper & inside
        if m.sum() >= 12 and m.sum() >= BACKGROUND_MIN_SHARE * max(int(inside.sum()), 1):
            grid[k // cols, k % cols] = [np.median(ch[m]) for ch in (r, g, b)]
    floor = np.array(paper_rgb, np.float32) * BACKGROUND_FLOOR
    for _ in range(rows + cols):  # fill cells without paper from their neighbours
        empty = np.isnan(grid[..., 0])
        if not empty.any():
            break
        padded = np.pad(grid, ((1, 1), (1, 1), (0, 0)), constant_values=np.nan)
        neigh = np.stack([padded[1 + dy:1 + dy + rows, 1 + dx:1 + dx + cols] for dy in (-1, 0, 1) for dx in (-1, 0, 1)])
        import warnings
        with warnings.catch_warnings(), np.errstate(all="ignore"):
            warnings.simplefilter("ignore", RuntimeWarning)  # cells whose neighbours are all empty stay empty this round
            mean = np.nanmean(neigh, axis=0)
        grid[empty] = mean[empty]
    grid = np.where(np.isnan(grid), np.array(paper_rgb, np.float32), grid)
    padded = np.pad(grid, ((1, 1), (1, 1), (0, 0)), mode="edge")  # smooth: 3x3 median, then 3x3 mean
    grid = np.median(np.stack([padded[1 + dy:1 + dy + rows, 1 + dx:1 + dx + cols] for dy in (-1, 0, 1) for dx in (-1, 0, 1)]), axis=0)
    padded = np.pad(grid, ((1, 1), (1, 1), (0, 0)), mode="edge")
    grid = np.mean(np.stack([padded[1 + dy:1 + dy + rows, 1 + dx:1 + dx + cols] for dy in (-1, 0, 1) for dx in (-1, 0, 1)]), axis=0)
    grid = np.clip(np.maximum(grid, floor), 1, 255)
    return f"{rows},{cols};" + ",".join(str(int(round(v))) for v in grid.ravel())


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
    paper_rgb = measure_paper(tif, facts["width"], facts["height"])
    paper_arg = ",".join(str(v) for v in paper_rgb)
    strength_arg = measure_strength(tif, paper_rgb, facts["width"], facts["height"])
    background = measure_background(tif, paper_rgb, facts["width"], facts["height"])
    background_arg = f' background="{background}"' if background else ""

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
        lines.append(f'    <PixelFunctionArguments band="{band}" paper="{paper_arg}" strength="{strength_arg}"{background_arg}/>\n')
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
