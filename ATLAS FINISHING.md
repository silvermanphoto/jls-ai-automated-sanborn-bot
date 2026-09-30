# Atlas finishing: the 1911 Atlanta Sanborns, 2026-09-28 to 2026-09-30

How the placed sheets became the finished QGIS atlas: fitting, colour, speed, the archive, inset maps and cleanup.
Each section gives the rule Joel set, why, and how it is done. Scripts under `_local/claude/` are local-only (the
folder is git-ignored); everything else named here is tracked.

## 1. Placing sheets

- **Similarity fit only** (Joel's hard rule, 2026-09-28): rotation + uniform scale + shift, least squares over every
  measured point. Never a three-point affine; it turns measuring error into skew. A sheet passes at 3+ points,
  leave-one-out error 15 m or less, and 0.035-0.075 m per source pixel. Reference: `_local/claude/ladder/simfit.py`.
- **Aesthetic gate** (2026-09-28): Tile 1 at 5 m error was "flawless". Judge gross failures only; do not chase metres.
- **Joel is the expert.** When a sheet resists, ask him to pin it (SANBORN PINS layer) or give an A/B hint (point A on
  the sheet belongs at point B). Every A/B hint: shift the sheet, then refine with a Fable Max run.
- **Railroads are controls.** Rail lines rarely move; street/rail crossings anchor industrial sheets. OSM railways,
  including abandoned lines, are in `batch/osm/atlanta-railways-3857.geojson`.
- **Look before you move.** Read the sheet's own labels and the neighbouring sheets' edge numbers before placing.
  Tile 97 was put on the Hemphill reservoirs while its labels said Swift/Marietta fertilizer works 4 miles NW; Tile 92
  was moved the wrong way along its railroad.
- **Seeds can be wrong.** Tile 353's seed was "the mean of two neighbouring sheet numbers"; the ladder then placed it
  350 m off, 21% too small and turned 73 degrees wrong, leaving a sheet-sized hole ringed by neighbours labelled
  "353". Holes whose rim labels all name one sheet mean that sheet is misplaced. Fixed 2026-09-30 from four surviving
  crossings (Whitehall/Humphries, Humphries/Wells, Whitehall/Stewart, Stewart/Wells; leave-one-out 8 m).
- **Refit from the current placement:** `_local/claude/bake/refit_sheet.py TILE '[[px,py,X,Y,label],...]'` takes
  points measured on the baked full-res file, fits current->true (similarity), SIFT-matches the LOC scan to the placed
  raster, composes the two and rebuilds from the .jp2. Then whiten edges, bake, update the QGIS layer, rebuild the
  merged layer and sheet index (section 4).
- **Skew removal** (474, 486, 493, 494, 2026-09-29): SIFT-match the LOC scan to the skewed placement, map matches
  through its geotransform, take a similarity fit; the new sheet lies within about 1 m of the old one.
- Half-scale sheets exist (163, 164: Inman Park, about 1.2 km across) and so do far-flung ones (95, 96: 8 miles NW).
  Size or distance alone is not an error.

## 2. Colour: what Joel asked for, in order

1. Paper to pure white ("I thought you agreed to make the yellow paper = pure white").
2. Every sheet's whiteness matching: each sheet's paper colour is measured and balanced to one reference.
3. "Way too bleached out": the look was refitted to Joel's own processed Sheet 151 (dense pinks and yellows, crisp
   black text) by registering the raw scan to his image and fitting per-channel levels.
4. Exact fill colours in a narrow band on every sheet: pink #e29cac, orange #daa15a, yellow #edd24d, blue #89b2c7.
   Fills are recognised by hue and saturation and replaced; ink inside a fill stays dark so lettering survives.
5. "Areas of dingy white": shadows, stains and folds. Each sheet carries a 32-column map of its local paper colour,
   and every pixel is balanced against the paper around it. The map ignores pale pink wash inside dense blocks (the
   first version whitened it into blotches).
6. Sharpening: unsharp mask 15%, radius 3.4 px, threshold 1. Joel rejected 30% as oversharpened.

The look lives in `tools/sanborn_paper.py` (engine v1.24-1.29, tested). Dark scan edges (background, binding shadow)
were whitened inside the TIFFs by `_local/claude/ladder/whiten_edges.py`.

## 3. Baked files, speed and the archive

- **Bake, don't compute at draw time.** Recipes drew through a Python pixel function on every redraw; baking the
  colour into files made reads about 30 times faster. `_local/claude/bake/bake_one.py SOURCE NAME` (batch
  `run_all.sh`) writes, per sheet:
  - `1911 SANBORN BAKED/full/<name>.tif`: full resolution, lossless, internal mask, overviews; shown closer than 1:4000.
  - `1911 SANBORN BAKED/20pct/<name> 20pct.tif`: 20% size, JPEG quality 40 (Joel's spec), mask.
  - The archive copy (below), verified by byte size, then removed locally.
- **Archive** (Joel's requirement): matching full-resolution colour-and-sharpened GeoTIFFs, uncompressed, are NOT on
  the MacBook. They are on Skychief's OWC ThunderBay RAID 5 (H:, "Zeppo") at
  `H:\2026 Files\26-032 Sanborn Georeferencer\1911 Atlanta Sanborns - archive GeoTIFF (uncompressed)\` (401 files,
  90 GB on 2026-09-29, plus the inset maps). Reach it with `ssh skychief`; push with `sftp -b`.
- **Kept on the MacBook:** the LOC .jp2 scans (any refit starts there). Pre-colour TIFFs, edge-whitening backups and
  superseded builds went to the Trash with Joel's approval (71 GB, 2026-09-29).
- **Far zoom is one layer.** 398 separate 20% layers took 18 s to draw the city; one merged file takes under 1 s.
  `_local/claude/bake/build_mosaic_darken.py` (12 min) builds `1911 SANBORN BAKED/mosaic/1911 Sanborns 20pct mosaic
  (darken).tif` from `mosaic_inputs.opt`, keeping the darkest pixel of every sheet covering a spot, so no sheet's
  white paper can hide another sheet's buildings. Rebuild it after any sheet changes.

## 4. How the QGIS master is arranged

- Master: `JLS Master Map File with 1911 Sanborns.qgz`. Saving it is authorised; a startup script keeps dated copies.
- `1911 ATLANTA SANBORNS` holds, top to bottom: the sheet index, four quadrant folders (tile < 100 NORTHWEST,
  < 300 NORTHEAST, < 400 SOUTHWEST, else SOUTHEAST), INSET MAPS, the merged far-zoom layer, the white backing.
- **Blending** (Joel, 2026-09-29): the merged far-zoom layer uses Darken. Full-resolution sheets use Normal, because
  Darken left ghosted double images where sheets overlap.
- **White backing**: one white polygon traced from the merged layer's outline, so a basemap does not show through
  the Darken paper. Rebuild it when the outline changes.
- **Sheet index** (`mosaic/1911 Sanborns sheet index.gpkg`, `build_sheet_index.py`): invisible outlines of every
  sheet and inset, so the Identify tool names the sheet at any zoom. Rebuild after a sheet moves.
- **Inset maps** (2026-09-29): Joel outlined the secondary maps on 24 sheets in the Inset Cutter
  (`_local/claude/bake/inset_tool/server.py`, http://localhost:8791; cuts in `overlay/inset_cuts.json`).
  `make_insets.py` cuts each from its parent and saves it as `Tile N INSET MAP`, labelled in black Futura Bold,
  placed 25 m outside its parent on the side it came from. Nothing is discarded: untouched parents are in
  `1911 SANBORN BAKED/_before-inset-cuts/`.
- Baked layers need `renderer.setAlphaBand(4)`; QGIS exposes the TIFF mask as band 4, and without it off-sheet
  areas draw black.
- QGIS opens on downtown Atlanta (Five Points) at every launch, new project and project open, set in
  `~/Library/Application Support/QGIS/QGIS3/startup.py`, because crash restarts kept opening at 0,0 off Africa.

## 5. Gotchas found on the way

- Never reload the `sanborn_paper` module inside a running QGIS; it crashed QGIS (SIGSEGV). Restart instead.
- `osascript 'tell application "QGIS" to quit'` is refused while QGIS is busy. Save through the MCP, then call
  `QgsApplication.instance().quit()` on a short timer.
- Long renders through the QGIS MCP time out; start the render job, have it write a result file, and wait for it.
- QGIS's MrSID JP2 driver crashes on these scans: set `GDAL_SKIP="JP2MrSID JP2ECW"`.
- A GDAL Python pixel function receives the full-resolution window (`xoff`, `xsize`) even for a reduced read.
- The ENVI driver refuses sheared geotransforms; sample with `-a_ullr 0 1 1 0`.
- The QGIS MCP connection has one slot; other clients (Codex, the Claude app) can take it. Joel clicks Start Server.
- Port 8765 is taken by another local app; the Inset Cutter uses 8791.
- A cleanup pass found five approximate sheets loaded twice, a hidden skewed 486 copy and a duplicate base map.
  After placing or replacing sheets, check for two layers per tile and rebuild the merged layer from the kept set.

## 6. Working with Joel on this project

- He is the expert; what he sees on screen is fact. Never argue from saved files against his observation.
- Never override a budget, time limit, model or rule he set; ask first with the cost.
- When a question can be answered through the QGIS MCP (does this panel line up with today's streets?), answer it
  with a render over OpenStreetMap. Do not hand him "check in QGIS" as advice.
- Show full sheets, and full-resolution previews, before running a change across the atlas.
- Tools he clicks in must let him click past the image edge (margins), so nothing is left uncut.
