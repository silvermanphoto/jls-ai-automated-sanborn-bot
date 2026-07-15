# AI Automated Sanborn Bot

This project is the local-first system for downloading, locating, georeferencing, checking, and preparing hundreds of 1911 Atlanta Sanborn sheets. Current system version: **1.13**.

The Mac does the expensive and repetitive work. ChatGPT remains the steering layer: it reviews a small set of street matches, corrects a source point when image reading is imperfect, and approves the final local comparison. A live QGIS connection is **not** required to build or inspect the reference packet.

## What the Mac now does

- caches all four official Library of Congress catalogs and resumes interrupted JP2 downloads;
- builds a bounded local OpenStreetMap street database for Atlanta;
- proves the downloaded sheet number only from an exact Apple Vision reading in a printed title corner; whole-sheet Tesseract matches remain suggestions and require manual confirmation;
- reads approximate locations for index tiles through 549 with Apple's local Vision text recognizer and limits every seed and proposed-control envelope to 250 meters;
- records a reviewed per-tile index seed when OCR is missing, ambiguous, or visibly wrong, without changing the shared OCR index;
- runs spatial OCR at four rotations and preserves every reading's full-resolution source coordinates;
- uses reviewed historical-name aliases, source-image street geometry, and exact shared OpenStreetMap nodes to propose control intersections;
- rejects wrong-neighborhood proposals with an independent index-location safeguard;
- creates a new timestamped, never-reused local OSM and 1921 Kauffman comparison packet for each review attempt;
- performs the full-resolution affine warp, real alpha band, lossless compression, checksums, embedded derivation evidence, and file audit;
- locks each tile across local processes, safely resumes abandoned downloading or proposing work, and isolates failures between tiles;
- safely reuses stable file hashes during one run so large shared evidence is not reread unnecessarily;
- writes a schema-3 QGIS import manifest binding the raster and the complete approved evidence chain for final in-memory layer placement.

## Start here

The first setup is:

    python3 tools/sanborn_batch.py doctor
    python3 tools/sanborn_batch.py catalog
    python3 tools/sanborn_batch.py osm-refresh
    python3 tools/sanborn_batch.py index-build

Run a sequential batch:

    python3 tools/sanborn_batch.py add 154-160
    python3 tools/sanborn_batch.py work 154-160

Or use three independent local jobs, the recommended M4 Max batch setting:

    python3 tools/sanborn_parallel.py 154-160 --jobs 3

Both routes stop at the judgment boundary. They do not silently approve controls or write the protected QGIS project.

## When the index location is missing or wrong

The index seed is only a neighborhood safeguard, but every finished tile needs one unique reviewed seed. If OCR misses the printed number, returns competing locations, or produces a location that is visibly wrong, use the queue-aware wrapper after opening the saved georeferenced-index preview and identifying the center of the tile:

    python3 tools/sanborn_batch.py confirm-seed 154 \
      --preview-pixel PREVIEW_X PREVIEW_Y \
      --reviewer "Joel" \
      --note "Visually confirmed the center of printed Tile 154 on the saved index preview"

`--preview-pixel` means an X/Y pixel in the exact preview recorded by `index-build`; it is not a Sanborn source pixel, a screenshot coordinate, or a full-resolution index pixel. If an EPSG:3857 coordinate is already known, use the mutually exclusive alternative:

    python3 tools/sanborn_batch.py confirm-seed 154 \
      --map-coordinate EPSG3857_X EPSG3857_Y \
      --reviewer "Joel" \
      --note "Visually confirmed this Tile 154 center against the georeferenced index"

The reviewer and note are required because this is a human evidence decision. `--replace-existing` deliberately supersedes an earlier manual seed while preserving its history. `--override-high-confidence` is required only when correcting one unique high-confidence OCR seed and must be used only after the displayed index proves that OCR location is wrong. A locked packet, approved tile, or verified tile must first be reopened so its evidence can be rebuilt.

The complete plain-English command sequence is in `LOCAL BATCH ENGINE.md`. The deeper accuracy and recovery rules are in `AI AUTOMATED SANBORN BOT - MASTER INSTRUCTIONS.md`. `AGENTS.md` records the non-obvious lessons that future sessions must preserve.

## Review and approval boundary

The proposal file contains ranked three-control choices plus the evidence and ambiguity for each street match. ChatGPT or Joel selects or corrects one triplet, then the local packet renders:

- the complete source sheet and labeled source crosshairs;
- a low-resolution georeferenced Sanborn preview;
- local OSM roads over the same EPSG:3857 extent;
- a reprojected Kauffman crop and overlay;
- one four-panel contact sheet.

Each packet lives at a unique path such as `batch/reviews/tile-0154/packet-YYYYMMDDTHHMMSSffffffZ/`. It includes an immutable copy of the selected controls named `approved-controls.points`. The approval hash includes that copy, the source, local OSM database and metadata, Kauffman raster, index-seed evidence, rendering code, software and font provenance, affine limits, and every required review artifact. A correction creates a new timestamped packet; an earlier packet is never edited or reused.

Affine warnings still stop a packet by default. A genuinely distorted historical sheet can proceed only with `--allow-distortion` and a nonblank `--distortion-note`; the flag, written evidence, and exact warnings become part of the approval hash and final ledger.

## QGIS organization and rendering

After a tile is verified, `tools/sanborn_qgis.py` checks its schema-3 import manifest and prepares safe code for the live QGIS session. Before changing QGIS, it validates the raster, adjacent ledger, source, immutable controls, review and approval records, all eight packet artifacts, OSM and Kauffman inputs, renderer and font inputs, and automatic or manually confirmed index-seed provenance. It rejects any same-number raster pointing to a different file anywhere in the project. If a later operation fails, it rolls back newly added layers and restores prior layer-tree state. The helper requires the exact root-level index layer:

`1911 Sanborn Index Orthorectified — OSM 9-point fine-tuned (2026-07-14)`

It keeps that index outside the `1911 ATLANTA SANBORNS` folder, places the folder immediately below the index, orders tiles by printed number, keeps the folder expanded, and collapses every raster row so the Red, Green, and Blue band entries stay closed.

Every completed raster uses 100% global opacity, Brightness +50, Gamma 1.2, Contrast +20, and band 4 as the real alpha band. The emitted QGIS code contains no project-save call and verifies that the protected `.qgz` modification time did not change. Stable, stat-aware hash caching avoids repeatedly reading the same large Kauffman and other shared evidence within one run; a changed file identity forces a new hash.

The earlier Tile 236 live exercise remains useful evidence for group placement, duplicate-free reruns, numeric order, collapsed child rows, the rendering formula, and no project save. It used an equivalent in-memory hash-bound plan because that older raster predates an adjacent schema-3 ledger, so it was not a live schema-3 manifest run. The current schema-3 code has also been exercised in the installed QGIS 3.42.1 runtime with the real fine-tuned index and synthetic tiles. The QGIS MCP connection was unavailable for that current pass, so the present schema-3 revision has not yet been run against or used to mutate Joel's open protected project.

## Important safety rules

The original Sanborn scan is read-only. Every result receives a new filename. The protected project at `/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/JLS Master Map File.qgz` must never be saved or closed by automation unless Joel explicitly authorizes that action in the same turn.

The queue permits only declared state changes. Abandoned `downloading` and `proposing` claims are recovered automatically when resumed `work` acquires that tile's lock. Failed, rejected, or stale evidence requires an explicit `reopen` note. A verified tile can also be deliberately reopened to `approved` for revalidation, or to an earlier state for rebuilding. In a parallel batch, one tile's failure is isolated; the other tiles continue and each tile receives its own log.

Before an earlier verified result gives up its fixed filename for a correction, the worker copies and verifies its raster, ledger, manifest, and review evidence in a relocation-aware archive. The removal step has an exact hash-bound retirement record. If that step is interrupted, recovery may restore only that specific unfinished attempt; it will never substitute an older archived version merely because the filenames match.

The finished GeoTIFF itself stores the source hash, immutable-control hash, recomputed affine signature, and local-pipeline identity. `finish` will resume without warping only when both the GeoTIFF and adjacent ledger independently verify against the current source, controls, seed limits, pixels, extent, CRS, compression, and embedded evidence. If final publication is interrupted with only one file present, it preserves that file under a timestamped `.incomplete-...` name before rebuilding. A rewritten ledger alone cannot make an unrelated or stale raster pass.

## Current verification

The final staged suite passes **122 tests in 26.9 seconds**, including a real GDAL warp whose embedded GeoTIFF provenance passed batch resume verification. Current regression evidence also includes:

- Real Tile 154 passed the Apple Vision printed-title gate. Its first OCR run, including native-helper compilation, took about 17 seconds; a warm spatial-OCR run took 4.8 seconds. The tightened 250-meter proposal ranked Auburn × Butler, Auburn × Fort, and Houston/Dobbs × Butler first in about 10 seconds, and its existing local packet was visually inspected. The exercised final warp took about 5 seconds and produced 6587 × 7845 RGBA, SHA-256 `6b3162c42e1d414b9d0ca8213352bd33b6436baecc3576f4c028fa4402aacc6c`, with band checksums `23168 / 61645 / 40864 / 26750`; the protected project remained unchanged.
- Real Tile 474 exercised the controlled-distortion packet and approval with scale ratio `1.3393`, axis angle `98.500°`, a written evidence note, 67 local OSM ways, and the Kauffman crop. Its tightened 250-meter final warp took about 5.5 seconds and produced 7735 × 9239 RGBA, SHA-256 `f1de82562647d0ecb27b1815da9c8056329b084565e0747c6c8b82b5e7852083`, with band checksums `55125 / 14515 / 65108 / 51289`; the protected project remained unchanged.

This repository is private. Large source scans and finished GeoTIFFs remain local because they can be recovered from the Library of Congress or regenerated from the saved controls and audit records.

The preceding hands-on georeferencing workflow remains permanently available on the private branch `legacy-manual-georeferencing-v1.9`, anchored at commit `253c137`. The `main` branch carries the local-first batch system.
