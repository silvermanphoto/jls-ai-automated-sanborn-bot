# Local Batch Engine

## The division of labor

Version 1.13 moves the heavy work onto Joel's Mac. The local worker downloads and verifies scans, proves printed-sheet identity, reads street labels with coordinates, looks up modern streets, proposes controls, renders both reference overlays, warps the full-resolution image, audits the result, and prepares its QGIS layer entry.

ChatGPT or Joel steers at the decision boundary:

1. review historical-to-modern street identities and any ambiguity;
2. select or correct three source intersections;
3. inspect one compact local OSM and Kauffman contact sheet;
4. approve or reject the exact packet.

The source scan, OSM extract, Kauffman map, and finished GeoTIFF stay on the Mac. A live QGIS connection is not needed for reference review. QGIS is used only after the tile is finished, to place and style the verified raster in the current unsaved session.

## One-time local setup

Run these commands from the `AI AUTOMATED SANBORN BOT` project folder:

    python3 tools/sanborn_batch.py doctor
    python3 tools/sanborn_batch.py catalog
    python3 tools/sanborn_batch.py osm-refresh
    python3 tools/sanborn_batch.py index-build

They perform four different jobs:

- `doctor` checks the local Python, GDAL command-line programs, Tesseract, Pillow, OpenCV, free disk space, and OSM-database status. The required GDAL programs include `gdal_edit.py`, which embeds source, controls, affine, and pipeline provenance inside each finished GeoTIFF; do not begin production if `doctor` reports it missing.
- `catalog` caches the four official 1911 Atlanta Library of Congress volumes.
- `osm-refresh` downloads a bounded Atlanta OSM extract and builds a local street-and-intersection database. Later, run it with `--replace` only when a deliberate OSM refresh is wanted.
- `index-build` uses Apple's Vision recognizer entirely on the Mac. It reads overlapping crops of the georeferenced 1911 index, supports printed tiles through 549, clusters repeated readings, and converts them to approximate EPSG:3857 location safeguards.

The index safeguard is independent of the Sanborn street-name matching. It is never used as a final control. Its purpose is to reject a mathematically coherent street match in the wrong Atlanta neighborhood. The worker clamps the seed tolerance to at most 250 meters, requires every proposed target control to remain inside the seed's 250-meter coordinate envelope, and passes the same envelope and seed-distance limit to the final warp. A missing or ambiguous seed remains visibly flagged for review; the worker never invents one.

### Recover a missing, ambiguous, or visibly wrong index seed

Open the exact preview created by `index-build` and visually locate the center of the printed tile. The preferred command is the batch wrapper because it takes the tile lock, writes the reviewed per-tile override, and refreshes the queue's stored seed when the tile is already queued:

    python3 tools/sanborn_batch.py confirm-seed 154 \
      --preview-pixel PREVIEW_X PREVIEW_Y \
      --reviewer "Joel" \
      --note "Visually confirmed the center of printed Tile 154 on the saved index preview"

Use exactly one coordinate method:

- `--preview-pixel X Y` is a pixel in the recorded index preview. It is not a pixel in the Sanborn scan, a screen or QGIS-canvas coordinate, or a full-resolution index-raster pixel. The command verifies the preview's hash and dimensions, scales the preview location to the full index, and then applies the recorded geotransform.
- `--map-coordinate X Y` is a coordinate already expressed in EPSG:3857. It is not longitude/latitude. The command maps it back into the verified index raster and rejects a point outside that raster.

The map-coordinate form is:

    python3 tools/sanborn_batch.py confirm-seed 154 \
      --map-coordinate EPSG3857_X EPSG3857_Y \
      --reviewer "Joel" \
      --note "Visually confirmed this EPSG:3857 center against the georeferenced index"

Both `--reviewer` and `--note` are required and must be nonblank. The note should say what was inspected and why the manual position is trustworthy. The wrapper defaults to a 250-meter tolerance; a smaller positive `--tolerance` is allowed, but a value above 250 meters is rejected.

The shared OCR index at `batch/index/tile-location-seeds.json` remains unchanged. The reviewed result is an atomic, per-tile record beside it, normally `batch/index/tile-location-seeds-manual-overrides/tile-0154.json`, including the original OCR evidence and the visual decision. If a manual record already exists, add `--replace-existing` to supersede it deliberately and retain the prior decision in its history. If one unique high-confidence OCR seed exists but is visibly wrong, add `--override-high-confidence`; this flag is not a convenience switch and must follow visual confirmation. Use both flags when replacing a manual decision whose underlying OCR seed was high-confidence.

The wrapper refuses to change a seed after a packet is awaiting approval, approved, or verified. Reopen that tile to `review-ready` or `queued` as appropriate, then confirm the seed and rebuild the proposal and packet. When a seed changes while a proposal exists, the proposal becomes stale; rerun:

    python3 tools/sanborn_batch.py propose 154

The underlying index command performs the same reviewed override without updating the batch queue. Use it only when operating on the index directly, then run the batch `index-lookup` or `propose` command to refresh queued evidence:

    python3 tools/sanborn_index.py confirm \
      batch/index/tile-location-seeds.json 154 \
      --index-raster "/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/Stage 1 -Orthorectified Atlanta Maps to print/1911 Sanborn Index Orthorectified_OSM_9point_finetuned_2026-07-14.tif" \
      --preview-pixel PREVIEW_X PREVIEW_Y \
      --reviewer "Joel" \
      --note "Visually confirmed the printed Tile 154 center on the recorded preview" \
      --tolerance 250

The lower-level form also accepts the mutually exclusive `--map-coordinate X Y`, plus `--override-dir`, `--replace-existing`, and `--override-high-confidence` under the same rules.

The local OSM database treats an intersection as a node actually shared by two named road ways. It does not average nearby nodes and does not treat a bridge crossing as an intersection. A divided road may correctly produce more than one candidate, which remains an explicit ambiguity for review.

## Run a sequential batch

Queue a range once, then let the Mac take each tile through download, spatial OCR, and control proposal:

    python3 tools/sanborn_batch.py add 154-160
    python3 tools/sanborn_batch.py work 154-160
    python3 tools/sanborn_batch.py status

The sequential worker contains a failure to the affected tile and continues through the requested range. Every file-changing operation holds a cross-process lock for that printed tile, so two local workers cannot own the same tile while different tiles can proceed concurrently. It stops every successful tile at `needs-chatgpt-review`; it never selects and approves controls on its own.

If a prior process stopped while a tile was `downloading` or `proposing`, resumed `work` acquires the tile lock and recovers the abandoned claim automatically. A download returns to `queued` and retains its resumable `.part` file. A proposal returns to `review-ready` when its verified source, spatial OCR, and printed-number proof remain valid; otherwise it returns to `queued`.

The printed-number gate is separate from street OCR. Apple Vision must read exactly the expected digits at sufficient confidence, title-sized, touching the top title strip, and inside the left or right title corner. A number elsewhere on the sheet cannot prove identity. Tesseract scans the whole sheet and may record matching-number suggestions, but those suggestions never pass the gate.

If Apple Vision is unavailable or the exact title-corner reading is not found, the tile stops at `number-check-required`. Inspect the saved preview, then record what was seen:

    python3 tools/sanborn_batch.py confirm-number 154 \
      --note "Printed 154 is visible in the upper-right sheet title"

Then continue that tile:

    python3 tools/sanborn_batch.py work 154

## Run three local jobs at once

The recommended M4 Max batch setting is three independent jobs:

    python3 tools/sanborn_parallel.py 154-196 --jobs 3

The parallel launcher queues the requested tiles, gives each tile its own log, and runs the ordinary resumable worker underneath. One failed tile stops safely without stopping the others. Use `--already-queued` when every requested tile is already in the queue. Its default `--delay 2` is a shared pacer between actual child-process launches: the second worker starts at least two seconds after the first and the third at least two seconds after the second. Each one-tile child receives a zero internal range delay, so the stated spacing is real rather than three simultaneous launches followed by ineffective per-child waits.

Three jobs are the production default, not a correctness shortcut. Use fewer jobs if the Library of Congress is responding slowly or the Mac is needed for other heavy work.

## What happens before review

For every tile, the local worker:

1. resumes or verifies the official JP2 without overwriting an existing valid scan;
2. records the source size and SHA-256 checksum;
3. runs Tesseract locally at four rotations for street suggestions and stores every usable word box in full-resolution source-pixel coordinates;
4. runs the independent Apple Vision title-corner gate for the exact printed tile number;
5. separates horizontal and vertical street families and uses local image geometry for slanted streets;
6. resolves clear modern names and only the historical aliases already approved in `config/street_aliases.json`;
7. finds exact OSM shared-node intersections in EPSG:3857;
8. rejects any triplet whose controls leave the 250-meter seed envelope and ranks the remaining wide, non-collinear choices;
9. stores the evidence, ambiguity, diagnostics, and rejected alternatives in `batch/proposals/`.

OCR text centers are not accepted as street intersections. That distinction matters on a diagonal road: the printed name may sit far from the crossing. The geometry helper estimates the street axis from the road image and intersects the source axes before proposing the point. The final source crosshair still requires visual confirmation.

Historical aliases are evidence, not global guesswork. Add a rename only after it has a source and review note. Fuzzy OCR can suggest a possible alias, but it cannot silently add one or hide a modern false friend.

## Review, select, or correct a proposal

The ranked choices for Tile 154 are stored in:

    batch/proposals/tile-0154.json

ChatGPT should read that small file, check every fuzzy or multiple-node match, and inspect the independent index-seed distance. The command below records review notes without approving anything:

    python3 tools/sanborn_batch.py review-proposal 154 \
      --correction "Control 3 is the right intersection, but its source center needs a small adjustment"

Select a ranked triplet and export a standard three-point QGIS file plus a permanent comparison record:

    python3 tools/sanborn_controls.py export \
      batch/proposals/tile-0154.json \
      --triplet 0 \
      --points "1911 SANBORN DOWNLOADS/Sanborn 1911 -- Tile 154_3points.points" \
      --comparison batch/proposals/tile-0154-selection.json

If one source point needs correction, name the control number and the corrected full-resolution source pixel. Repeat `--source-correction` if needed:

    python3 tools/sanborn_controls.py export \
      batch/proposals/tile-0154.json \
      --triplet 0 \
      --source-correction "3,2202,1052" \
      --correction-note "Centered on the diagonal Dobbs and Jesse Hill crossing" \
      --points "1911 SANBORN DOWNLOADS/Sanborn 1911 -- Tile 154_3points.points" \
      --comparison batch/proposals/tile-0154-selection.json

A correction changes source pixels only; it does not invent a different OSM target. The export reruns the affine safety checks before writing the points.

If the selected or corrected controls trigger only a scale-ratio or axis-angle warning and the historical sheet itself is independently proven distorted, the export must record that decision too:

    python3 tools/sanborn_controls.py export \
      batch/proposals/tile-0474.json \
      --triplet 0 \
      --allow-distortion \
      --distortion-note "The historic sheet is internally skewed; both independent overlays agree" \
      --points "1911 SANBORN DOWNLOADS/Sanborn 1911 -- Tile 474_3points.points" \
      --comparison batch/proposals/tile-0474-selection.json

This export override is deliberately narrow. It can waive only a live scale-ratio or axis-angle warning. It cannot waive mirroring, clustered or nearly collinear controls, inadequate source coverage or span, a wrong CRS, a wrong-neighborhood seed failure, or a control outside the source. It fails if the note is blank, if the note is supplied without the flag, or if no eligible distortion warning exists. Exporting the points does not approve the distortion for the review packet: repeat `--allow-distortion` and the same evidence in the `packet` command so the packet and final ledger lock the decision.

Reject a false proposal explicitly so it remains in the history:

    python3 tools/sanborn_batch.py review-proposal 154 \
      --reject "Historic Bell was matched to the wrong modern Bell in another neighborhood"

After correcting the alias, OCR interpretation, or local geometry, reopen the inspected tile and rebuild its proposal:

    python3 tools/sanborn_batch.py reopen 154 --to review-ready \
      --note "Corrected the Bell Street identity and reviewed the wrong-neighborhood match"
    python3 tools/sanborn_batch.py propose 154

## Build the fully local review packet

Once the three proposed source crosshairs are ready:

    python3 tools/sanborn_batch.py packet 154 \
      --points "1911 SANBORN DOWNLOADS/Sanborn 1911 -- Tile 154_3points.points" \
      --control-label "Dobbs (Houston) x Jesse Hill (North Butler)" \
      --control-label "Auburn x Fort" \
      --control-label "Auburn x Jesse Hill (North Butler)"

Every packet receives a new, never-reused folder such as `batch/reviews/tile-0154/packet-YYYYMMDDTHHMMSSffffffZ/`. It contains an immutable copy of the selected points named `approved-controls.points`, the full-sheet source proof, labeled crosshairs, an affine preview, locally rendered OSM roads, a reprojected Kauffman crop, both overlays, and `review-contact-sheet.png`. Corrections create another timestamped folder; do not edit an older packet in place.

Open that contact sheet at full size. Confirm all three named source crosshairs, the OSM anchors and surviving non-control streets, and the full historical grid against Kauffman. QGIS and the network are not required for this review.

The packet is approval evidence, not just a picture. Its lock includes:

- the source and points files;
- control labels, CRS, affine measurements, and safety limits;
- the local OSM database, its metadata, and the Kauffman raster;
- the renderer code, software versions, and font provenance;
- all eight required packet artifacts and their raster properties.

If anything changes, approval fails until the packet is rebuilt. If the packet reveals a bad point, do not approve it. Record the reason, reopen the tile to `review-ready`, correct or re-export the controls, and build a new packet.

Affine safety warnings reject packet creation by default. For a historical sheet whose real distortion is independently supported by both overlays, the exception must be explicit and written into the packet:

    python3 tools/sanborn_batch.py packet 474 \
      --points "1911 SANBORN DOWNLOADS/Sanborn 1911 -- Tile 474_3points.points" \
      --allow-distortion \
      --distortion-note "The historic sheet is internally skewed; both independent overlays agree"

`--allow-distortion` without a nonblank `--distortion-note` fails. The flag, note, and exact affine warnings are stored inside the packet's safety limits, included in its approval hash, rechecked before finishing, passed to the full warp, and recorded in the final ledger.

## Approve and finish

After the complete local contact sheet passes:

    python3 tools/sanborn_batch.py approve 154 \
      --approved-by "Joel and ChatGPT" \
      --note "Three controls, surviving non-control streets, and the complete Kauffman grid pass"
    python3 tools/sanborn_batch.py finish 154

The approval command has one certifiable default: the hash-locked local OSM and Kauffman packet. It recomputes the packet lock. The finish command checks it again, creates the full-resolution EPSG:3857 GeoTIFF locally, adds a real alpha band, uses tiled lossless DEFLATE compression, writes a verification ledger, and creates a schema-3 QGIS import manifest. `gdal_edit.py` writes four items into the GeoTIFF itself: the source SHA-256, immutable-control SHA-256, affine-provenance signature, and `local-first-affine-v2` pipeline identity.

If both GeoTIFF and ledger finished just before an interruption, rerunning `finish` recomputes the approved affine and validates the live raster, its embedded provenance, pixels, RGBA structure, extent, CRS, compression, seed envelope, packet, and adjacent ledger before resuming without another warp. Rewriting only the ledger cannot make a stale raster pass. If only one of the two was published, `finish` preserves the lone file under a timestamped `.incomplete-...` name before rebuilding; it does not delete interrupted evidence or accept a one-file result. A source, points, seed, packet, output, ledger, or embedded-provenance mismatch stops the tile.

Large shared evidence can make repeated SHA-256 reads expensive. The batch, warp, and QGIS helpers cache a digest only for the lifetime of the process and only while the canonical path, device, inode, byte size, modification time, and change time remain identical. That speeds repeated checks of the same Kauffman raster and other shared files without weakening validation; any identity change forces a new hash, and a mid-read change stops the operation.

## Prepare the verified tile in QGIS

First inspect the intended actions without changing QGIS:

    python3 tools/sanborn_qgis.py batch/qgis-import/tile-0154.json --dry-run

ChatGPT can then request the exact execution payload:

    python3 tools/sanborn_qgis.py batch/qgis-import/tile-0154.json --emit-payload

The payload is sent to the live QGIS code connection only after the dry run passes. A schema-3 manifest binds the finished raster, adjacent ledger, source, immutable controls, review record, approval record, eight review artifacts, OSM and Kauffman inputs, renderer and font files, and automatic or manual seed provenance. The helper validates the ledger's RGBA, CRS, compression, band checksums, embedded GeoTIFF provenance, and unchanged protected-project record, then verifies the open project path, project CRS, exact index layer, and every input before changing the in-memory layer tree.

The QGIS result must be:

- exact root-level index: `1911 Sanborn Index Orthorectified — OSM 9-point fine-tuned (2026-07-14)`;
- root-level folder `1911 ATLANTA SANBORNS` immediately below that index, with the index outside it;
- exactly one layer node for each finished raster, in ascending printed tile-number order;
- folder expanded and every raster row collapsed, hiding the Red, Green, and Blue legend rows;
- Brightness 0, Gamma 1.0, Contrast 0 (no channel stretch), global opacity 100%, and band 4 as alpha;
- protected project file unchanged and no project-save call.

The helper is safe to run again for the same manifest. Before its first mutation, it scans the whole project registry and layer tree for any same-number raster pointing to a different source path. It reuses the intended raster by canonical path and verifies that no duplicate registry or tree entry remains. When repositioning an existing group, it inserts a clone before removing the old tree node so QGIS cannot unregister the group's live raster layers. It snapshots existing nodes and renderer values; if a later step fails, it removes newly registered layers and restores prior group, node, name, and style state. The generated code never saves the project.

An earlier live Tile 236 exercise established duplicate-free reruns, numeric order `154 / 196 / 236 / 474 / 485 / 486 / 493 / 494`, exact group placement, expanded group, collapsed raster children, Brightness +50, Gamma 1.2, Contrast +20, opacity 1.0, alpha band 4, `save_project=false`, and an unchanged protected `.qgz`. That older raster predates an adjacent schema-3 ledger, so the exercise used an equivalent in-memory hash-bound plan and was not a live schema-3 manifest run. The present schema-3 code has been exercised in the installed QGIS 3.42.1 runtime with the real fine-tuned index and synthetic tiles. Because the QGIS MCP connection was unavailable for that pass, do not claim that the current schema-3 revision inspected or mutated Joel's open protected project.

## Queue states and recovery

The normal path is:

    queued -> downloading -> review-ready -> proposing -> needs-chatgpt-review
           -> awaiting-approval -> approved -> verified

There are explicit stopped or review states for `number-check-required`, `proposal-stale`, `proposal-rejected`, and `failed`. The worker permits only declared state changes. It never silently sends a failed or rejected tile back into production. The only automatic recovery is an abandoned `downloading` or `proposing` claim encountered by resumed `work` after the tile lock is acquired.

After inspecting the cause, use:

    python3 tools/sanborn_batch.py reopen 154 --to queued \
      --note "Removed the incomplete JP2 after confirming the interrupted download"

Use `--to review-ready` only when the verified source scan, spatial OCR, and printed-number confirmation remain valid. Use `--to queued` when download or source evidence must be rebuilt. The required note becomes part of the event history.

An unapproved packet that reveals a bad point may be reopened from `awaiting-approval` to `review-ready`. An approval may also be explicitly withdrawn to `review-ready`. In both cases the rejected points and packet are cleared and must be rebuilt.

A verified tile can be reopened deliberately in three ways:

    python3 tools/sanborn_batch.py reopen 154 --to approved \
      --note "Revalidate the existing approved evidence and final pair after an engine update"

    python3 tools/sanborn_batch.py reopen 154 --to review-ready \
      --note "Rebuild the controls and packet from the still-verified source and OCR"

    python3 tools/sanborn_batch.py reopen 154 --to queued \
      --note "Rebuild the tile from acquisition after inspecting the existing evidence"

Only a previously verified tile may reopen directly to `approved`, and its existing packet must still pass approval verification. The earlier destinations deliberately return the tile to more work and clear locked review evidence where appropriate.

When a verified tile returns to `review-ready` or `queued`, its fixed-name GeoTIFF, ledger, schema-3 manifest, and packet evidence are first copied into a checked relocation-aware archive. A hash-bound retirement record makes removal crash-safe. A resumed retirement may restore only the exact unfinished attempt with the same original paths and checksums; an unrelated older archive is never used as a substitute. If the current set is missing or incomplete without that exact record, reopening stops for inspection.

`batch/sanborn_batch.sqlite3` is the source of truth. It stores each legal state, attempt, failure stage, proposal evidence, corrections, rejections, seed provenance, review directory, and final output path. Keep the database, `.points` files, selection records, approvals, ledgers, and QGIS manifests. Previews and OCR can be rebuilt.

## Current regression evidence

The final staged suite passes **122 tests in 26.9 seconds**, including a real GDAL warp whose embedded GeoTIFF provenance passed batch resume verification.

- On the real Tile 154 scan, Apple Vision passed the exact printed-title gate. First-run OCR including native helper compilation was about 17 seconds; an earlier warm spatial-OCR run was 4.8 seconds. The 250-meter proposal placed Auburn × Butler, Auburn × Fort, and Houston/Dobbs × Butler at rank 0 in about 10 seconds. The tightened final warp completed in about 5 seconds at 6587 × 7845 RGBA, SHA-256 `6b3162c42e1d414b9d0ca8213352bd33b6436baecc3576f4c028fa4402aacc6c`, band checksums `23168 / 61645 / 40864 / 26750`, with the protected project unchanged. Its existing local packet was visually inspected.
- Real Tile 474 exercised packet creation and approval with the documented distortion exception. The hash-locked warnings were scale ratio `1.3393` and axis angle `98.500°`; the packet used a written note, 67 OSM ways, and the Kauffman crop. Its 250-meter-envelope warp completed in about 5.5 seconds at 7735 × 9239 RGBA, SHA-256 `f1de82562647d0ecb27b1815da9c8056329b084565e0747c6c8b82b5e7852083`, band checksums `55125 / 14515 / 65108 / 51289`, with the protected project unchanged.

2026-09-25 appearance correction: preserve the original TIFF appearance. Brightness and contrast stay at 0, gamma at 1, opacity at 100%, alpha band 4, and RGB channel stretching is disabled. This supersedes every earlier enhanced-display preset; it does not alter the source pixels.
