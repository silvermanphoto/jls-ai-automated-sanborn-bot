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

## 4a. Speed: the fast view (2026-09-30)

- **Default view is the tile pyramid** `1911 Sanborns - fast view (tile pyramid)`, second item in the Sanborns folder:
  WEBP XYZ tiles in `1911 SANBORN BAKED/tiles/{z}/{x}/{y}.webp`, zoom 12-21 (1.2 GB, 215,199 tiles). Zooms 17-21
  are rendered from the 419 full-res sheets and insets in layer-tree order (top sheet wins), 12-16 from the Darken
  merge, so it looks the same as the layer stack. A never-seen close view draws in about 2 s, a revisited one in
  1.5 s; the per-sheet stack took 13-17 s. Built by `_local/claude/bake/build_tiles.sh` (GDAL 3.12 from
  `/Applications/QGIS-final-4_0_0.app`, 9 processes, 17 min); delete `tiles/` before rebuilding or moved sheets
  leave old tiles behind.
- **The per-sheet layers stay for editing**: the four quadrant folders, INSET MAPS and the merged layer are switched
  off, not removed. Switch a folder on to toggle or inspect single sheets.
- **Close-zoom files are JPEG** (Joel, 2026-09-30): layers read `1911 SANBORN BAKED/full_jpeg/` and `insets_jpeg/`
  (quality 90, overviews 2-32, 2.8 GB against 6.2 GB lossless; mean colour difference 0.1-1.5 of 255, masks
  identical). The lossless `full/` and `insets/` files and the RAID archive are unchanged and remain the masters.
- **After any sheet changes** (on top of section 4b): make its JPEG copy (as in the agent's recipe: gdal_translate
  JPEG q90 YCbCr 512 tiles with the mask, overviews 2-32 q85), repoint its layer, and rebuild the tile pyramid.
- **The slowest thing left is the web basemaps.** Open Street Map or Google layers add tens of seconds to a fresh
  view; switch them off when working on the Sanborns.
- QGIS now renders in parallel on all cores with a 2 GB GDAL cache (QGIS settings, effective after a restart).
- Spotlight indexes the 215,000 tile files after every rebuild and loads the disk for a while.

## 4c. Simplified layers (2026-10-02)

Joel: "I just want it simplified and to look amazing from every level of detail, and to be able to host a website
where the opacity can be clicked on/off, and I want it organized so that in qGIS I can individually turn on/off layers.
But I don't want unnecessary multiples of things." Plan picture: the georeferencer app's
`docs/diagrams/1911-layer-simplification.mmd` (approved 2026-10-02).

- **One atlas layer.** `1911 Atlanta Sanborns - atlas` = the tile pyramid, now zoom 9-21 (zooms 9-11 new, for
  city-wide and regional views) with the white paper baked in: `build_tiles.py` rasterizes the white-backing outline
  to `mosaic/1911 Sanborns white backing (raster).tif` (on the merged file's 0.3 m grid, rebuilt when the outline is
  newer) and draws it under every sheet in both passes. 215,997 tiles, 1.6 GB, 32 min. The same folder of tiles is
  what a website would serve; hosting and public or private are Joel's decision.
- **Sheets one layer each, at every zoom.** The 1:4000 limit is gone; each file's overviews (down to 1/32) do the
  level-of-detail work. Measured city-wide: one sheet instant, one quadrant (97) 2.1 s and all 419 layers 8.7-11.6 s
  when first opened, 0.7 s once QGIS has them open; the atlas under 1 s.
- **Out of the project:** the merged far-zoom layer and the white backing (files kept; both feed `build_tiles.py`).
- **Sheet index:** kept in the project but out of the Layers panel (`addMapLayer(layer, False)`), because the hover
  tool (Floating Info Tool fork) and the right-click menu find it by its name prefix. Removing its panel entry removes
  the layer from the project even with the registry bridge disabled; `simplify_master.py` removes it and adds it back.
- **Right-click menu** (Layers Under Click 1.1, source only in the QGIS profile plugins folder; 1.0 saved as
  `_local/claude/layers_under_click_v1_2026-10-02.py`): also lists every sheet and inset whose outline is under the
  click, marked "(off)" when switched off; choosing one selects it in the Layers panel. The atlas is listed only over
  the atlas.
- **Map themes** "Atlas" (atlas on, sheets off) and "Sheets" (atlas off, sheets on). A QGIS theme stores every layer's
  visibility, so it also restores the basemap that was on when it was made (Google Hybrid).
- Scripts: `simplify_master.py` (the one-time change), `check_master.py` (updated invariants; previous version
  `check_master_v1_2026-09-30.py`), `build_tiles.py` (previous version `build_tiles_v1_2026-09-30.py`). Old tiles:
  `1911 SANBORN BAKED/superseded/2026-10-02 layer simplification/tiles`.
- Open, not part of this change: `1883-1894 Cram's Map of Atlanta` points at the Ward Map's file
  (`1883-1894 Atlanta Ward Map.tif`), so `check_master.py` reports one file loaded twice.

## 4d. Floater cleanup (2026-10-02)

Joel: "Do you see the pointless and messy 'stranded' white floaters in tiles like 467? I want them cleaned up
judiciously and carefully. This *includes* 'floating rosettes', any cardinal direction rosettes that are
well-integrated into their tile obviously may stay, but an 'additional' stranded rosette must be cut out along with
the white 'floaters.' However, do NOT remove floating numbers like the '467' shown. That must be closely cut out and
left in place." He approved Tile 467 as the standard, said to leave ragged edges attached to the map alone, and to
keep neighbouring-sheet reference numbers too (closely cut, like the sheet's own number).

- **What an island is.** Each connected piece of a sheet's visible area (full resolution, 8-connected); the largest
  is the map and is never touched. Islands with no drawing (under 1 m2 of ink or fill colour) were cut without review.
- **Review.** 1,944 islands with drawing went on cards (`_local/claude/bake/align/v3_rebuild/floaters/cards_all`).
  Opus Low decided every card (32 batches); Opus Medium re-decided 250 targeted cards (16 batches) and overturned
  53 of 946 sampled Low cuts (5.6%, under the 15% re-review line). The lead settled the 79 disagreements, kept the
  "Tile N INSET MAP" label letters on insets, and cut kept map scraps that a neighbouring sheet already draws
  (`keep_cover.py`). Final: 479 islands kept or close-cut, the rest cut.
- **Close cut** (`floaters.py number_keep`): ink letters, grown 0.5 m, gaps under 1 m joined, holes filled. A ruled
  line touching a digit keeps only strokes thicker than 7 px (big digits are 14-40 px, lines 4-6). Four islands with
  a stranded rose beside the number (176, 329, 331, 96) keep only lettering inside a named box.
- **Far-zoom copies.** A file's overviews (and the 20% copy) lose a cell only when most of it was cut; the first
  write used "any part cut" and turned close-cut numbers into specks at far zoom, so every file was restored and
  written again.
- **Files.** Masks edited in place in `full/`, `full_jpeg/`, `20pct/`, `insets/`, `insets_jpeg/`; the files before
  are in `1911 SANBORN BAKED/superseded/2026-10-02 floater cuts (files before)/`. `rebuild.sh` then rebuilt the
  merged far-zoom file, the sheet index and white backing outlines, and the atlas tiles (old ones in
  `superseded/2026-10-02 floater cuts (merged layers)/`).

## 4e. Hole patching (2026-10-02)

Joel: "Please patch all the 'holes' left behind like in 467. I see that the source itself does not have these holes,
so it should be easy to restore them. Go tile by tile and do not miss any." Work in
`_local/claude/bake/align/v3_rebuild/holes/`.

- **Finding holes** (`find_holes.py`): areas no sheet draws that are enclosed by drawn sheets, on the merged file's
  mask: 799. `score_holes.py` and `hole_cards.py` found 556 with scan pixels under some sheet's mask; the other 243
  have no scan anywhere (gaps between sheets).
- **Sources.** Never an inset file (it carries its whole parent page at the inset's placement), and never a parent
  inside its own inset area (`inset_areas.py`): either would fill the hole with drawing from somewhere else.
- **Review.** Cards of each hole now and filled. Opus Low decided all 556 (16 batches), Opus Medium re-decided a
  targeted set (7 batches), the lead settled 24 disagreements and 2 unsure: 524 restore, 32 left open (gaps, or
  fills that would bring back margin junk).
- **Patch** (`patch.py`): in each source's full-resolution grid, the mask is switched on where the hole is, no sheet
  draws now and the scan has pixels. 456 holes took pixels (44,877 m2); 63 were already drawn by inset layers (the
  merged far-zoom file has no insets, so they looked like holes there) and 5 were filled by a neighbour's patch.
- **Black at far zoom** (`fix_ovr.py`): a file's overviews and its 20% copy hold black where the sheet was masked
  when they were made, so a restored area drew black at far zoom (hole 546, the strip along Rawson St. between 467,
  485 and 486). Newly drawn cells that were black now carry the average of the full-resolution scan under them.
  `dark_check.py` checks every patch for black cells and for scan edge brought back; its flagged patches were all
  real drawing on the cards (`flags_1.jpg`, `flags_2.jpg`).
- **Files.** Files before the patch are in `1911 SANBORN BAKED/superseded/2026-10-02 hole patch (files before)/`;
  `rebuild.sh` rebuilt the merged file, outlines and tiles (old ones in `superseded/2026-10-02 hole patch (merged
  layers)/`).
- **Result.** After the rebuild, 275 m2 of the 524 approved holes is still open (`cover_check.py`, counting inset
  layers): 148 m2 is the Rawson St. strip where no scan exists, the rest slivers under 20 m2. The merged file took
  2 h 14 min to rebuild (13 min after the floater cuts); the tiles 24 min. Master saved; `check_master` passes except
  the Cram/Ward Map duplicate.
- **Seen, not changed:** Tile 486 draws its scanned page edge (a brown and black strip about 3 m wide) along Rawson St.
  Cut in 4f.

## 4f. Page-edge strips (2026-10-02)

Joel approved trimming scanned page edges (tan or brown paper edge, curled paper, binding shadow, black scan border)
before one final rebuild: "please trim and reprocess. Keep asking Opus/Low to do each step of the work, but promote
the reviewer to be Opus/High". Work in `_local/claude/bake/align/v3_rebuild/edges/`.

- **Finding strips** (`edge_scan.py`): on each sheet at about 0.2 m, off-palette colour (not white or the four fills)
  and solid black touching the sheet's edge or the file border: 267 candidates on 115 sheets. `cut.py groups` joined
  them into 163 groups (pieces along the same file side join across 60 m, because a brown edge near the orange fill
  colour is detected in pieces).
- **Two cuts per group** (`cut.py`): BAND, everything the sheet draws from its edge in to the depth of the junk
  joined to the edge (99th percentile, plus 0.3 m); STRIP, only the junk-coloured pixels, grown 0.3 m. Cards show the
  atlas as drawn now and after each cut, with a red outline of the cut (`cards/`; pre-cut copies in `cards_before/`).
- **Review.** Opus Low decided all 163 (11 batches). Opus High re-decided every non-band call and every third band,
  then, because it overturned 6 of 25 sampled bands, the other 40 bands too. The lead settled 28 disagreements by
  reading the cards (`lead.json`), always toward the safer cut. Final (`decisions.json`): 53 band, 12 strip, 98 keep.
  Most keeps are false hits (big sheet numbers, the digit 1) or curled paper that carries drawing.
- **Cut** (`cut.py write`): the mask is switched off over the cut in every file of the sheet (full, full_jpeg, 20%,
  inset), overviews by majority; 65 cuts on 49 sheets (`write_log.jsonl`). Files before the cut are in
  `1911 SANBORN BAKED/superseded/2026-10-02 edge strips (files before)/`.
- **Rebuild** (`edges/rebuild.sh`): merged file, outlines, backing and tiles; old ones in `superseded/2026-10-02 edge
  strips (merged layers)/`. 38 min in all (darken 11 min, tiles 27 min). `check_master` passes except the Cram/Ward
  Map duplicate.
- **Result.** 6,341 m2 no longer drawn (`gapcheck.py`, at 1.2 m, merged file without insets). Where no other sheet
  lies under a cut, the atlas had a gap and the basemap showed, as at every other gap between sheets. 3,637 m2 of it is
  inside the atlas, nearly all the widening of gaps that were already there; the one new gap of note was Rawson St.
  between 485 and 486 (953 m2, about 3 m wide, where no scan exists). Pictures: `page edge before-after 486.jpg`,
  `page edge before-after 92.jpg`.
- **White inner gaps (Joel, 2026-10-03).** `trace_backing.py` now fills every gap enclosed by the atlas smaller than
  1,000 m2 (352 gaps, 31,239 m2, Rawson St. among them), so they show white paper in the atlas tiles. Larger enclosed
  areas Sanborn never drew (Oakland Cemetery 19.1 ha, rail yards, 28 in all) stay open and show the basemap; Joel
  chose the cap after seeing them (`largest inner gaps.jpg`). The earlier trace is `trace_backing_v1.py`; backing and
  tiles before are in `superseded/2026-10-03 white inner gaps (merged layers)/`. Tiles rebuilt in 25 min
  (`edges/rebuild_white.sh`). The Sheets view still shows the basemap in every gap.

## 4b. Safety net and records (added 2026-09-30 on a second review)

- **After any change to the master, run `_local/claude/bake/check_master.py` inside QGIS before saving.** It checks
  the counts (393 sheets, 24 insets), duplicates, missing files, alpha band, blend, the 1:4000 switch, folders, the
  merged layer, the index and the backing. It exists because the Tile 474 INSET MAP layer vanished from the project
  unnoticed between two saves; none of the earlier spot checks would have caught it.
- **`1911 SANBORN BAKED/manifest.json`** (`write_manifest.py`) lists every baked file with its geotransform, size,
  archive path, fit record and status; `_ARCHIVE README.txt` beside it (also on the RAID) names the archive files
  that are superseded (the five APPROXIMATE duplicates and the engine's Tile 485).
- **Where each sheet's fit came from:** ladder placements in `<Map Book>/_sanborn-ladder-runs/tile-NNNN/`
  (`result.json`, `resim.json`, `refit.json`) with `_ladder.points` beside the LOC scan; the deskewed 474/486/493/494 in
  `_local/claude/bake/deskew.json`; sheets the engine or the overnight team placed in `batch/reviews/tile-NNNN/`.
  Regenerating the atlas from the .jp2 scans needs those records; the baked files carry only the north-up output grid.
- **Folder names are Joel's to change.** He appends number ranges (`SOUTHEAST ATL (#451-549)`); anything that finds a
  quadrant folder must match by prefix. The ladder does now, and since engine 1.30 so does
  the importer (`tools/sanborn_qgis.py`), which also tolerates the INSET MAPS folder and the three helper layers
  beside the quadrant folders; before 1.30 it refused the current master outright.
- **Three Pythons:** QGIS's `python3` (3.9: osgeo, numpy, scipy, PIL 7) runs anything that touches GDAL in-process;
  `/Library/Frameworks/.../3.12` has cv2 and scipy but no osgeo (call `gdalinfo`/`gdal_translate` as programs);
  `/usr/local/bin/python3.12` runs the engine tests. `refit_sheet.py` needs the second, `bake_one.py` the first.
- **Speed:** the merged layer draws the city in under a second; with Google Satellite switched on the same view took
  14-68 s, all of it tile downloads. When QGIS feels slow, check the web basemaps first.
- **Open gaps (2026-09-30, from `fp_mask` of the merged layer, ground m²):** 185,000 at -9392270, 3995086 (Oakland
  Cemetery, never mapped); 70,000 at -9396081, 3997402; 64,000 at -9395065, 3997442; 50,000 at -9394958, 3996094;
  41,000 at -9396828, 3993479; 12,000 at -9393079, 3996361; 10,000 at -9395094, 3998246; 10,000 at -9396462,
  3993750; 8,000 at -9395504, 3998010. Check each the way 353 was checked (render, read the rim labels).
- **Tiles 32 and 174** were baked and archived but missing from QGIS; the previous summary counted 393 sheets and
  never asked why the LOC's 395 were not all there. Tile 32 (engine-verified, held only by the old strict QC) lines up
  with Jett, Neal, Proctor, Jones, Chestnut and Griffin and was added 2026-09-30 (394 sheets). Tile 174's ladder fit
  failed the 15 m gate; over OSM it sits plausibly along the Southern rail line at Mayson Av / 1st St / New St but is
  never point-verified; Joel chose to add it as a normal sheet (2026-09-30), so the atlas is 395 sheets, the LOC's full count. Lesson: reconcile the sheet count against the catalog, not the layer list.
- **Parked:** the single full-resolution overlay (one seamless image, no ghosting, insets removed). Draft real-map
  outlines are in the hidden QGIS group "1911 OVERLAY - sheet map areas" and `overlay/sheet map areas (edit me).gpkg`;
  the inset cuts are done. Remaining: cut seams along streets, mosaic at full resolution (about 5-10 GB), add as one
  layer.

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
- Two Trash folders hold 71 GB of superseded sheet files until Joel empties the Trash; the baked and archived files do not depend on them.
- A cleanup pass found five approximate sheets loaded twice, a hidden skewed 486 copy and a duplicate base map.
  After placing or replacing sheets, check for two layers per tile and rebuild the merged layer from the kept set.

## 6. Working with Joel on this project

- He is the expert; what he sees on screen is fact. Never argue from saved files against his observation.
- Never override a budget, time limit, model or rule he set; ask first with the cost.
- When a question can be answered through the QGIS MCP (does this panel line up with today's streets?), answer it
  with a render over OpenStreetMap. Do not hand him "check in QGIS" as advice.
- Show full sheets, and full-resolution previews, before running a change across the atlas.
- Tools he clicks in must let him click past the image edge (margins), so nothing is left uncut.
