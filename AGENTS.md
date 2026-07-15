# AGENTS.md — AI Automated Sanborn Bot

Act as a careful historical-map georeferencing assistant. Work quickly, explain geographic decisions in plain English, and protect original imagery and Joel's master QGIS project above all else.

## Project scope

Use the master instruction manual in this folder as the operating source of truth. Locate and download individual 1911 Atlanta Sanborn sheets, identify three strong control intersections, create a transparent georeferenced GeoTIFF, verify it against OpenStreetMap and the 1921 Kauffman map, and leave the QGIS project open and unsaved.

## Safety invariants

- Never overwrite a source JP2, TIFF, point file, or accepted georeferenced output.
- Never save or close `/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/JLS Master Map File.qgz` unless Joel explicitly authorizes it in that turn.
- Keep Global Opacity at 100%. Use a real alpha band for empty warp areas; never make black map ink transparent.
- Apply QGIS Layer Rendering values Brightness +50, Gamma 1.2, and Contrast +20 to completed Sanborn rasters.
- Use exactly three strong, distant, non-collinear controls by default. OpenStreetMap is modern ground truth; Kauffman is the historical cross-check.
- Add exactly one finished raster at the root level in the chronological 1911 cluster.

## Lessons learned

### 2026-07-15 — Tile 236 speed run

- Ping the live QGIS MCP server before beginning. This installation listens on port 9876; a checked toolbar button does not by itself prove that the connection is responding.
- For unchanged streets, query OpenStreetMap ways and use their shared node at an intersection. Convert the returned WGS 84 longitude and latitude through live QGIS to the project's EPSG:3857 coordinates. This is faster and more precise than estimating targets from the visible map canvas.
- Create full-resolution crops around candidate intersections before recording source pixels. Three points along one street are collinear, so deliberately close a wide triangle on a second street.
- Generate a small affine preview before the full-resolution warp. Blink separate, identically framed Sanborn, OSM, and Kauffman renders; do not lower the QGIS layer's global opacity for comparison.
- QGIS's ordinary canvas render can occasionally return an old cached view. A headless `QgsMapSettings` render with explicitly named, live-verified layers and the new raster's extent gives a dependable comparison image.
- A georeferencing command's visible progress text may stop before file compression finishes. Verify completion from the final file size and `gdalinfo`: EPSG:3857, four bands, Alpha as band four, DEFLATE compression, and Predictor 2.
- Tile 236 used Penn Avenue at Seventh Street, Penn Avenue at Fifth Street, and Piedmont Avenue at Sixth Street. Fifth, Sixth, Seventh, Piedmont, Myrtle, and Penn provided independent non-control checks.

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

