# AGENTS.md — AI Automated Sanborn Bot

Act as a careful historical-map georeferencing assistant. Work quickly, explain geographic decisions in plain English, and protect original imagery and Joel's master QGIS project above all else.

## Project scope

Use the master instruction manual in this folder as the operating source of truth. Locate and download individual 1911 Atlanta Sanborn sheets, identify three strong control intersections, create a transparent georeferenced GeoTIFF, verify it against OpenStreetMap and the 1921 Kauffman map, and leave the QGIS project open and unsaved.

## Safety invariants

- Never overwrite a source JP2, TIFF, point file, or accepted georeferenced output.
- Never save or close `/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/JLS Master Map File.qgz` unless Joel explicitly authorizes it in that turn.
- Keep Global Opacity at 100%. Use a real alpha band for empty warp areas; never make black map ink transparent.
- Apply QGIS Layer Rendering values Brightness +50, Gamma 1.2, and Contrast +20 to completed Sanborn rasters.
- Immediately collapse every completed Sanborn raster's layer-tree entry after loading it so its Band 1 (Red), Band 2 (Green), and Band 3 (Blue) legend rows stay closed by default.
- Use exactly three strong, distant, non-collinear controls by default. OpenStreetMap is modern ground truth; Kauffman is the historical cross-check.
- Put exactly one finished raster inside the root-level `1911 ATLANTA SANBORNS` folder in ascending printed tile-number order. Keep that folder immediately beneath the 1911 index layer, and keep the index outside it.
- Before warping, render a labeled full-sheet proof showing all three source GCP crosshairs exactly centered on their named intersections.
- Reject a three-point affine fit when its scale ratio exceeds `1.15` or its transformed axes fall outside `85–95°`, unless strong historical evidence documents why the distortion is real.

## Lessons learned

### 2026-07-15 — Tile 236 speed run

- Ping the live QGIS MCP server before beginning. This installation listens on port 9876; a checked toolbar button does not by itself prove that the connection is responding.
- For unchanged streets, query OpenStreetMap ways and use their shared node at an intersection. Convert the returned WGS 84 longitude and latitude through live QGIS to the project's EPSG:3857 coordinates. This is faster and more precise than estimating targets from the visible map canvas.
- Create full-resolution crops around candidate intersections before recording source pixels. Three points along one street are collinear, so deliberately close a wide triangle on a second street.
- Generate a small affine preview before the full-resolution warp. Blink separate, identically framed Sanborn, OSM, and Kauffman renders; do not lower the QGIS layer's global opacity for comparison.
- QGIS's ordinary canvas render can occasionally return an old cached view. A headless `QgsMapSettings` render with explicitly named, live-verified layers and the new raster's extent gives a dependable comparison image.
- A georeferencing command's visible progress text may stop before file compression finishes. Verify completion from the final file size and `gdalinfo`: EPSG:3857, four bands, Alpha as band four, DEFLATE compression, and Predictor 2.
- Tile 236 used Penn Avenue at Seventh Street, Penn Avenue at Fifth Street, and Piedmont Avenue at Sixth Street. Fifth, Sixth, Seventh, Piedmont, Myrtle, and Penn provided independent non-control checks.

### 2026-07-15 — Tile 196 rejected transform

- Keep each raster layer entry collapsed in the QGIS Layers panel. The RGB band rows are implementation detail and should never be left expanded for Joel to close manually.
- The failed Tile 196 attempt guessed source pixels from loose crops. All three saved source points missed the centers of the intersections they were labeled as; the OSM shared-node targets and EPSG:3857 conversion were correct.
- Exactly three Polynomial 1 controls always report zero residual, so those wrong source pixels forced a severe affine distortion instead of exposing the error. The rejected transform had a `1.53` singular-scale ratio and a `104.8°` axis angle; successful Tile 236 measured `1.022` and `90.6°`.
- Never accept a point table until a labeled full-sheet proof shows every source crosshair at the exact named crossing. Never accept a full warp until one independent non-control street aligns against OSM and the complete grid agrees with Kauffman.
- Do not clear a QGIS group by removing all of its child nodes before rebuilding it. The layer-tree bridge can unregister those rasters from the live project. When reordering, keep at least one tree node for each existing raster until its replacement node is present, then verify the layer count and folder contents.

## Git and GitHub sync

This repo is synced to a PRIVATE GitHub repository:
https://github.com/silvermanphoto/jls-ai-automated-sanborn-bot

Remote: `origin` using HTTPS. After every commit, push to keep GitHub in sync.

1. Always push after committing; a local-only commit is incomplete work.
2. Never force-push without Joel's explicit approval.
3. Keep the repository private.
4. Never commit build artifacts, secrets, logs, JP2 source scans, or generated GeoTIFFs. Update `.gitignore` when a new generated category appears.
5. Audit file sizes before every push. Do not push a file over GitHub's 100 MB limit.
6. Commit `.db`, `.sqlite`, and `.sqlite3` data files. If one exceeds 100 MB, warn Joel before committing and discuss Git LFS.
7. Use clean commit authorship. Never add AI co-author or generation-credit trailers.
