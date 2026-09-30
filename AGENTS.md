# AGENTS.md — AI Automated Sanborn Bot

## Latest approved workflow — September 27, 2026

Read the final Approved cost reduction, QGIS crash prevention, Visible QGIS delivery, and First-pass hold audit sections of `CLAUDE.md` first. It supersedes older leadership effort, two-stage independent QC and 25-map batch instructions below. The active project handoff is in `run-state.json`; inspect actual goal and QGIS state before resuming. Live implementation and compaction checkpoints are in `run-state.json`; do not confuse agreed settings with settings already applied.

## Current team workflow — September 26, 2026

Joel approved the team workflow and later asked to switch routine leadership to Sol, with Astra Extra High independent QC. Current model settings are stated in the final cost-reduction section of CLAUDE.md; verify the app setting separately. Read `CLAUDE.md` first; its latest team and delivery sections supersede historical High-only, single-reviewer and unsaved-master wording below. Keep reusable project knowledge in that one canonical brief and its linked local records. Follow its September27 numeric-processing section: begin at Tile1 and advance by printed number, recording holds explicitly. Do not duplicate changing counts or lessons here. Any driver, including Claude, must read `_local/overnight-2026-09-25/team/PROTOCOL.md`, `run-state.json`, `team/knowledge/facts.json` and `QGIS_RECOVERY.md` before resuming. Lead owns shared state and QGIS, producers own separate drafts, QC owns independent approvals. Update this routing note if record locations or responsibilities change. The live delivery safeguard and its validation record are described in the final continuity paragraph of CLAUDE.md. Also follow the September27 budget-efficiency and aerial-fallback section; aerial research is reserved for blocked or low-certainty sheets. The geographic-review lessons section records check coverage and clipped-width corrections; keep detailed evidence in the linked local records.

Act as a careful historical-map georeferencing assistant. Work quickly, explain geographic decisions in plain English, and protect original imagery and Joel's master QGIS project above all else.

## Project scope

Use the master instruction manual in this folder as the operating source of truth. Run the local-first worker for downloads, spatial OCR, OSM matching, index safeguards, reference packets, rotate-scale-shift warps, and audit records. Joel or ChatGPT must still judge the proposed street identities and measured points. QGIS is the final in-memory display and layer-organization boundary; leave its protected project open and unsaved.

## Safety invariants

- Never overwrite a source JP2, TIFF, point file, or accepted georeferenced output.
- Saving the master `JLS Master Map File with 1911 Sanborns.qgz` is allowed under Joel's standing permission (dated copies are kept automatically); check the layer invariants first (`_local/claude/bake/check_master.py`). Never close QGIS with unsaved work.
- Keep Global Opacity at 100%. Use a real alpha band for empty warp areas; never make black map ink transparent.
- Apply QGIS Layer Rendering values Brightness 0, Gamma 1.0, and Contrast 0; disable channel stretching to completed Sanborn rasters.
- Immediately collapse every completed Sanborn raster's layer-tree entry after loading it so its Band 1 (Red), Band 2 (Green), and Band 3 (Blue) legend rows stay closed by default.
- HARD RULE (Joel, 2026-09-28): every Sanborn sheet is fitted with rotation + uniform scale + shift (a similarity fit, image y running down), least squares over all measured points, fit corners and check points alike. Never build a sheet from a three-point affine (`gdalwarp -order 1` over exactly three GCPs): it turns measuring error into skew. Skew beyond 3° is never acceptable and more than 1° is suspicious; the similarity fit has zero skew by design. A sheet passes when its leave-one-out error (each point predicted from a fit to the others) is 15 m RMS or less, it has at least three measured points, and its scale is plausible (0.035–0.075 ground metres per source pixel). The fit lives in `tools/sanborn_georeference.py` (`fit_diagnostics`, `fit_safety_warnings`, `write_similarity_vrt`): the similarity geotransform [c, a, b, d, b, −a] is written into a VRT and warped by `gdalwarp` without `-order`. Nothing waives the pass test; the old scale-ratio/axis-angle distortion exception is retired.
- Use at least three strong, distant street controls. OpenStreetMap is modern ground truth; Kauffman is the historical cross-check.
- Require the fully local, hash-locked OSM and Kauffman packet before approval. Live QGIS is not required for reference review.
- Treat the georeferenced-index location as an independent wrong-neighborhood safeguard, never as a final control point.
- Place each finished raster exactly once inside the root-level `1911 ATLANTA SANBORNS` folder, in its area subfolder, in ascending printed tile-number order within that subfolder. Since 2026-09-28 sheet layers live in four area subfolders, in this order: `NORTHEAST ATL` (volume 2, tiles 100–299), `NORTHWEST ATL` (volume 1, tiles below 100), `SOUTHEAST ATL` (volume 4, tiles 400 and up) and `SOUTHWEST ATL` (volume 3, tiles 300–399). The importer creates a missing subfolder in its fixed position and refuses a sheet sitting loose in the group or in the wrong subfolder. Keep every sheet's legend collapsed. Keep `1911 ATLANTA SANBORNS` immediately beneath the exact 1911 index layer, and keep the index outside it.
- Before warping, render a labeled full-sheet proof showing every measured point's crosshair exactly centered on its named intersection.
- Reject a sheet whose leave-one-out error exceeds 15 m RMS, that has fewer than three measured points, or whose scale falls outside 0.035–0.075 m per source pixel. No flag or note waives these gates.

## Lessons learned

### 2026-09-29 — Version 1.25, per-sheet paper balance

- `render_vrt_xml` measures each sheet's paper colour (`measure_paper`: median red, green, blue of bright near-grey pixels
  with alpha, from a gdal_translate read at 1/16 size, never below 8 px) and writes it into every colour band's
  `PixelFunctionArguments` as `paper="r,g,b"`. `clean` scales each channel by 178 / paper before the levels and
  paper lift, so every sheet's paper ends equally white (Joel: "whiteness levels of all tiles match").
- Never `importlib.reload(sanborn_paper)` inside a running QGIS: GDAL frees the old pixel function from a draw
  thread and QGIS crashes (2026-09-29). After `install-module`, quit and reopen QGIS.
- GDAL passes pixel-function arguments as bytes; `clean` decodes them. Tests that stand in fake TIFFs must stub
  `measure_paper` as well as `read_raster_facts`.

### 2026-09-29 — Version 1.24, paper whitening through recipe files

SUPERSEDED the same day: QGIS no longer draws the Sanborns through recipe files. The colour (extended through 1.29) is baked into the files under `1911 SANBORN BAKED/`; see `ATLAS FINISHING.md`. The recipe code and its tests remain in the engine and `sanborn_batch.py finish` still writes a recipe beside a finished TIFF, but the atlas path is: fit -> `_georeferenced.tif` -> bake.

- Every finished sheet gets `<name>.clean.vrt` beside its TIFF (`tools/sanborn_paper.py`):
  RGB bands are GDAL Python pixel functions `sanborn_paper.clean`, band 4 passes the
  TIFF's alpha through, the TIFF is named relative to the recipe, and no code sits
  inside the recipe. `sanborn_batch.py finish` and the ladder scripts write it; every
  importer loads it instead of the TIFF.
- Recipes are written read-only. GDAL rewrites a writable VRT (full WKT, statistics,
  source properties) whenever QGIS computes statistics, which breaks the exact-content
  check; read-only, it logs `Failed to write .vrt file` and leaves the file intact.
- The importer verifies the recipe against the hashed TIFF when it builds the plan,
  rehashes both before any change, treats a layer on the TIFF or on its recipe as the
  same sheet, switches a TIFF layer to its recipe in place (rollback switches it back),
  and stops before any change when a pixel block cannot be read through the recipe.
- One-time switch of already-loaded sheets:
  `python3 tools/sanborn_qgis.py migrate-to-recipes` prints PyQGIS code; it never saves
  and undoes every switch if one fails.

### 2026-09-28 — Version 1.23, rotate-scale-shift fit over every measured point

- Joel's hard rule: sheets are fitted by rotation + uniform scale + shift, least
  squares over all measured points. The three-point affine is gone from building,
  review previews, proposal ranking, historical corners and final verification.
- Why: three GCPs through `gdalwarp -order 1` always report zero residual, so any
  measuring error becomes skew. Tile 21 rebuilt with the similarity fit lands within
  about 1 m of its approved map.
- The pass test is leave-one-out error (each point predicted from a fit to the
  others) at most 15 m RMS, at least three points, and 0.035–0.075 ground metres per
  source pixel. On the real test triplets it rejects the bad Tile 196 pixels (47 m)
  and the Tile 474 triplet that once needed a distortion exception (58 m), and
  passes corrected 196 (9 m), 236 (5 m) and 154 (4 m).
- Historical exports write every corner, fit and check alike, into the points file.
  The separate check count, check spread and 5 m / 10 m withheld-check limits are
  retired; `independent_checks` now records the leave-one-out summary with
  `method: leave-one-out`.
- Review packets moved to schema 4 (`fit_diagnostics`). Schema-3 affine packets verify
  only as frozen evidence for rasters already imported; they cannot be approved,
  finished or resumed. Final ledgers carry `fit_model: similarity` and pipeline
  `local-first-similarity-v3`.
- Test fixtures now use about 0.05 ground metres per pixel so the scale gate is
  exercised on realistic geometry.

### 2026-09-28 — Version 1.22, area subfolders and the renamed master

- Joel re-saved the master as `JLS Master Map File with 1911 Sanborns.qgz` and split `1911 ATLANTA SANBORNS` into four
  area subfolders. The importer reads the folder names and tile ranges from the
  collection record in `tools/sanborn_collections.py`, sorts each subfolder on its own,
  and collapses every sheet legend in every subfolder.
- 46 existing georeference ledgers name the former master file. They record which
  file was protected when they were written, so ledger checks accept either name;
  only the new name is ever the live project.
- The rollback snapshot must treat a sheet inside any subfolder as inside the group.
  Checking only the group's direct children would record those nodes as outside the
  group and restore duplicates on rollback.


### 2026-09-27 — Version 1.20, `doctor` runs the tools it reports

- `doctor` now executes `--version` on `gdal_translate`, `gdalwarp`, `gdalinfo`,
  `gdal_edit.py`, and `tesseract`, and prints the version each one reported, so a
  healthy line is evidence the program actually started. A nonzero exit, a dynamic
  loader rejection, a refusal to start, or no answer within 30 seconds stops
  `doctor` with an ERROR line carrying that program's own text, the same way an
  unavailable OpenCV already stops it. This closes the 2026-09-22 gap below.
- `gdal_edit.py --version` prints the GDAL version and still exits 255 by design,
  so it is judged by what it reports rather than by its exit status. Any other
  probe would have to invent a throwaway raster to obtain a zero exit.
- Finding a program with `shutil.which` proves only that a file sits on PATH. A
  dependency check should run whatever it declares healthy.
- Measured on this date: 183 engine tests pass in 66 seconds under the python.org
  Python 3.12 with QGIS GDAL on PATH, real warps included and nothing skipped.
  Homebrew GDAL is no longer installed on this Mac, so GDAL now comes from
  `/Applications/QGIS.app/Contents/MacOS/bin`. OpenCV is absent from Homebrew
  Python 3.13, which blocks `test_batch_safety`, `test_control_proposals`, and
  `test_street_geometry` from loading there. Both are machine faults, not code
  faults, and neither touches this change.

### 2026-09-22 — Version 1.16, `doctor` does not prove a tool runs

- `doctor` reports GDAL healthy when `gdal_translate`, `gdalwarp`, `gdalinfo`, and
  `gdal_edit.py` are present on PATH. It never runs one. On this date every GDAL
  command aborted at launch because Homebrew `libheif` is linked against
  `libx265.216.dylib` while the `x265` symlink points at 4.3, which ships `.217`.
  `brew reinstall libheif` relinks it. A future `doctor` should execute one
  trivial GDAL command and report the loader error, because a toolchain that
  installs but cannot run looks identical to a working one in the current report.
- Homebrew's Python 3.14 has a broken `pyexpat` symbol and cannot parse XML, which
  fails every OSM path. Python 3.13 runs the suite. The suite is 122 tests; the 14
  failures seen on this date come from these two Homebrew faults, not from this code.
- The README is now written for readers outside this project. Keep the operating
  detail accurate there, and keep claims about the test suite attributed to the
  version and date they were measured.

### 2026-07-26 — Version 1.15, reviewed-control scan registration

- A previously reviewed `.points` file is geographic evidence, but its source
  pixels belong to one particular scan. The new reuse path registers the old
  and current scans from hundreds of local image features, transfers the three
  pixels through that measured transformation, and records both source hashes,
  the transformation, inlier count, agreement rate, and pixel residuals.
- Never copy reviewed pixel coordinates to a differently sized derivative.
  Reuse is allowed only when the two images behave like the same printed sheet
  with a border shift, slight rotation, and nearly uniform scale; perspective,
  weak feature agreement, large residuals, or a failed affine gate stop the
  transfer.
- Reused controls retain their three historic intersection names and still
  stop before a new OSM/Kauffman packet. Prior review is not current approval.

### 2026-07-26 — Version 1.14, the packet repair

- The packet step failed on every run from the day the batch engine shipped. `cmd_packet` created the packet folder, copied `approved-controls.points` into it, then called `sanborn_review.py create --review-dir` against that same folder — and that generator refuses a folder holding anything unless `--replace` is passed. Nothing was ever drawn, so no sheet reached approval or a final map. When one step both prepares a folder and hands it to another tool, check that tool's expectations about the folder being empty.
- A tool that exits non-zero inside `subprocess.run(check=True)` leaves whatever the caller already created. Every failed packet left an orphan folder holding only the control copy, invisible to the queue and to the app, accumulating on every retry. Remove what a failed step created.
- `finish` resumes from an existing raster and ledger, which is correct for crash recovery but wrong when the sheet was re-approved with different controls: verification compared the old map against the new approval and stopped with a mismatched-ledger complaint and no way forward. `_ledger_predates_approval` now sets the superseded pair aside and warps again from what was approved, keeping the safety intent without the dead end.
- Reproduce an app-reported failure by replaying the exact command with the app's sanitized environment (`env -i` with its fixed PATH) against a **copy** of `batch/sanborn_batch.sqlite3` via `--database`. Note that packet and review folders still land under the real `BATCH_DIR`, so clean up afterward.
- The warp is deterministic: two independent runs of sheet 487 from the same controls produced byte-identical rasters (`537a7de4…`). A differing fingerprint from unchanged inputs means something is genuinely wrong, not merely re-rendered.
- Downloads carry no macOS warning marking of their own, but one can attach after a scan is first opened, and Preview handles JPEG 2000 poorly regardless. `clear_macos_download_warning` strips the flag after the length check; it is best-effort and must never interrupt a download.
- A completed proposal with zero ranked triplets is a real outcome needing human street review, not a transient state. Two of the three queued sheets sit there now (four candidates on 486, one on 488). Anything reading the queue must present it as a dead end.

### 2026-07-15 — Local-first version 1.13 completion

- An OSM intersection is an exact node shared by two named road ways. Never average nearby candidates or infer a connection where bridge, tunnel, or divided-road geometry merely crosses. Preserve multiple exact nodes as an ambiguity for review.
- A printed street label's center does not locate its intersection on a diagonal road. Spatial OCR supplies names and approximate evidence; local image geometry must estimate the road axis and intersect source axes before proposing a pixel. The labeled crosshair remains the final visual test.
- Good affine measurements can still describe the wrong neighborhood. Read independent approximate sheet centers from the georeferenced 1911 index and reject otherwise coherent OSM triplets outside the seed tolerance. Missing, ambiguous, or visibly wrong seeds require a reviewed `sanborn_batch.py confirm-seed` decision; never guess or silently edit the shared OCR index.
- Prove a downloaded scan's printed number only with an exact Apple Vision reading in a title-sized left or right top corner. Tesseract searches the whole sheet and is useful for suggestions, but a matching address, building number, or adjacent-sheet label can never pass identity. If Vision is unavailable or the title-corner gate fails, require `confirm-number` with a visual note.
- Build index safeguards through tile 549 with Apple's local Vision recognizer over overlapping image crops. Cluster repeated readings of one printed number, retain the recognition provenance, and transform only a unique result through the index GeoTIFF's EPSG:3857 geotransform.
- Clamp every selected index seed to a maximum 250-meter tolerance. Require each proposed target control to remain within the seed's ±250-meter x/y envelope, require the transformed footprint to include the same seed within 250 meters, and record the envelope in the final ledger.
- For `confirm-seed`, use exactly one coordinate form. `--preview-pixel X Y` means a pixel in the hash-verified preview recorded by `index-build`; `--map-coordinate X Y` means an existing EPSG:3857 coordinate. Both require a nonblank reviewer and note. `--replace-existing` deliberately supersedes a prior manual record and preserves its history. `--override-high-confidence` is required to correct a unique high-confidence OCR seed and is permitted only after the index is visually checked. The lower-level equivalent is `sanborn_index.py confirm`; unlike the batch wrapper, it does not refresh the queue.
- Approval must hash every input that could change the meaning or appearance of the proof: source, points, labels, limits, local OSM database and metadata, Kauffman raster, renderer code, software and font provenance, and every required packet artifact. Recompute the lock at approval and before finishing.
- Every review attempt uses a new `batch/reviews/tile-NNNN/packet-TIMESTAMP/` folder and copies its selected controls to `approved-controls.points`. Never edit or reuse an old packet; corrections produce a new folder and approval lineage.
- A distortion exception during `sanborn_controls.py export` can waive only scale-ratio or axis-angle warnings. It cannot waive mirroring, weak geometry, wrong CRS, wrong neighborhood, or out-of-bounds controls; it requires a nonblank note and a live eligible warning. The exception must be declared again when building the packet because exporting points does not approve the packet or final warp.
- Queue states are legal transitions, not suggestions. Every file-changing operation holds a cross-process lock for its printed tile; the same tile serializes while different tiles can proceed. Resumed `work` may automatically recover only abandoned `downloading` or `proposing` claims after acquiring that lock. Failed, rejected, and stale evidence still requires an explicit `reopen` note. Parallel jobs isolate one tile's failure and keep the remaining tiles moving, with one log per tile.
- The parallel launcher's default two-second delay is enforced between actual child-process starts by one shared pacer. One-tile children receive zero internal range delay. Do not describe it as simultaneous launching or as a sleep that happens only inside each child.
- If a packet in `awaiting-approval` reveals a bad point, reopen it to `review-ready` with the reason. This clears the rejected points and packet so they cannot be approved accidentally; export corrected controls and rebuild the packet.
- Reposition a populated QGIS group clone-first: insert a cloned replacement node before removing the old node. Never clear the group's children and rebuild afterward, because QGIS's layer-tree bridge can unregister the live raster layers.
- Live QGIS verification on 2026-07-15 found QGIS `3.42.1-Münster`, project CRS `EPSG:3857`, 45 loaded map layers, and an already-dirty in-memory project. The exact root-level index is `1911 Sanborn Index Orthorectified — OSM 9-point fine-tuned (2026-07-14)`, sourced from `/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/Stage 1 -Orthorectified Atlanta Maps to print/1911 Sanborn Index Orthorectified_OSM_9point_finetuned_2026-07-14.tif`; that raster is EPSG:3857 and uses band 4 as alpha.
- A schema-3 QGIS manifest must bind the raster, adjacent ledger, source, immutable controls, review and approval records, all eight packet artifacts, OSM and Kauffman inputs, renderer/font inputs, and seed provenance. Before mutation, the helper must validate the entire chain, scan the whole project for same-number/different-path conflicts, and verify the exact project, index, CRS, group, numeric order, rendering formula, collapsed rows, and project-file modification time. Snapshot prior tree and renderer state and roll back on failure. Generated code must contain no project-save call.
- Publish the final ledger before the GeoTIFF so an interruption cannot leave an apparently valid raster without evidence. If only one member of the pair exists on resumed `finish`, preserve it under a timestamped `.incomplete-...` name, then rebuild; never delete or silently accept the lone artifact.
- `gdal_edit.py` is a required dependency. It embeds the source hash, immutable-control hash, recomputed affine signature, and pipeline identity inside the finished GeoTIFF. Resume must verify both the live raster's embedded evidence and the adjacent ledger; a rewritten ledger alone cannot certify a stale raster.
- SHA-256 caches are process-local and keyed by canonical path plus device, inode, byte size, modification time, and change time. They may avoid rereading unchanged shared files such as the large Kauffman raster, but any identity change forces rehashing and any mid-read change is a hard stop.
- A verified tile is reopenable only by an explicit noted command. `--to approved` is available only for a verified tile whose existing packet still validates, allowing final-pair and manifest revalidation. `--to review-ready` or `--to queued` deliberately returns it to earlier work and clears locked review evidence where appropriate.
- Before a verified fixed-name result is rebuilt, archive its raster, ledger, manifest, and review evidence as a checked relocation-aware bundle. Retirement is a two-phase operation with a hash-bound `retirement-state.json`: after a crash, restore only the one explicitly `retiring` attempt whose exact original paths and hashes match. Never recover by filename from an arbitrary older archive; missing current files without a matching interrupted-retirement record are a hard stop.
- The final staged suite passes 122 tests in 26.9 seconds, including a real GDAL warp whose embedded GeoTIFF provenance passed batch resume verification. Schema-3 QGIS dry-run, generated-code, whole-project identity, and rollback audits pass.
- Generated v1.12 QGIS code was exercised twice in the open protected project using existing Tile 236. Both runs reused the raster and group without duplication; preserved numeric order `154 / 196 / 236 / 474 / 485 / 486 / 493 / 494`; kept the group immediately below the exact index, expanded, with every child collapsed; confirmed Brightness +50, Gamma 1.2, Contrast +20, opacity 1.0, and alpha band 4; left the project dirty and unsaved; returned `save_project=false`; and left the protected `.qgz` modification time at `1784010514439060200`. Because this older raster predates an adjacent schema-3 ledger, the live exercise used an equivalent in-memory hash-bound plan. Schema-3 manifests are validated offline by the test suite; never mislabel this as a live schema-3 manifest run.
- The present schema-3 code has also been exercised in the installed QGIS 3.42.1 runtime using the real fine-tuned index and synthetic tiles. The QGIS MCP connection was unavailable, so that current pass did not inspect or mutate Joel's open protected project. Keep the older Tile 236 live evidence and the current isolated schema-3 evidence separate.
- The completed one-time local setup on 2026-07-15 contains 396 official Library of Congress catalog records across four volumes, 31,918 indexed OSM ways with 21,434 exact shared intersection nodes, and 345 index-location seeds: 227 unique high-confidence and 37 explicitly ambiguous. Reuse these local stores; refresh them only deliberately because their hashes are approval provenance.
- Before replacing the established manual workflow on `main`, preserve the last published manual state as branch `legacy-manual-georeferencing-v1.9` at commit `253c137`. Git history already retains it, but the named branch makes recovery visible and prevents future sessions from mistaking the local-first rewrite for deletion of the earlier method.
- Real Tile 154 passed the Apple Vision title gate. Its 250-meter proposal ranked Auburn × Butler, Auburn × Fort, and Houston/Dobbs × Butler first, and its existing local packet was visually inspected. The exercised final output took about 5 seconds and is 6587 × 7845 RGBA with SHA-256 `6b3162c42e1d414b9d0ca8213352bd33b6436baecc3576f4c028fa4402aacc6c` and band checksums `23168 / 61645 / 40864 / 26750`; the protected project remained unchanged.
- Real Tile 474 exercised a hash-locked distortion exception with scale ratio `1.3393`, axis angle `98.500°`, a written note, 67 OSM ways, and the Kauffman crop. Its final output took about 5.5 seconds and is 7735 × 9239 RGBA with SHA-256 `f1de82562647d0ecb27b1815da9c8056329b084565e0747c6c8b82b5e7852083` and band checksums `55125 / 14515 / 65108 / 51289`; the protected project remained unchanged.

### 2026-07-15 — Local-first batch engine

- The deterministic helper described in the manual existed only in an older project and never reached this repository. Keep all executable workflow code beside the current manual so instructions cannot refer to missing tools.
- Native Apple-silicon GDAL and Python are much faster and less fragile for heavy processing than mixing them with QGIS 3.42's Intel Python libraries. Keep QGIS/MCP as the thin display and layer-organization boundary.
- Batch acquisition must remain conservative: cache all four official LOC catalogs once, download sequentially with resume and backoff, and stop for human-verification challenges.
- Approval must be locked to the complete local packet, including its references, provenance, and rendered artifacts. Recompute that lock at approval and again immediately before the full warp; comparing two old stored hashes does not protect the current files.
- Plausible affine geometry cannot prove geographic location. Require the local OSM and Kauffman packet, the independent index-location safeguard, and a surviving non-control street check before approval.
- Reject controls that are clustered or nearly collinear even when their scale ratio and axis angle look normal. Require useful horizontal and vertical span, at least 2% source-triangle coverage, and EPSG:3857 by default.
- Treat an uncertain Apple Vision title-corner reading as a blocked visual check. Tesseract number hits are suggestion-only. Never silently advance a valid-looking JP2 under the wrong printed tile number.
- The semantic bottleneck is street identity, not computation. OCR, image geometry, approved aliases, and exact OSM nodes should propose evidence; Joel or ChatGPT must still confirm the three intersections and the independent OSM/Kauffman check.

### 2026-07-15 — Tile 236 speed run

- During the original interactive speed run, the live QGIS MCP server listened on port 9876. Version 1.13 does not need that connection for download, control proposal, reference review, approval, or warping; connect only for final in-memory QGIS layer preparation.
- For unchanged streets, use the local OSM database's exact shared node at an intersection and its stored EPSG:3857 coordinate. This is faster and more precise than estimating targets from the visible map canvas.
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
- Do not clear a QGIS group by removing all of its child nodes before rebuilding it. The layer-tree bridge can unregister those rasters from the live project. Insert a cloned replacement group first, remove the old node only after the replacement exists, then verify the registry, layer count, folder contents, numeric order, and collapsed rows.

## Git and GitHub sync

Joel explicitly designated this engine as PUBLIC on 2026-09-25 so historians and academics can use its software:
https://github.com/silvermanphoto/jls-ai-automated-sanborn-bot

Remote: `origin` using HTTPS. After every commit, push to keep GitHub in sync.

1. Always push after committing; a local-only commit is incomplete work.
2. Never force-push without Joel's explicit approval.
3. Keep this engine repository public. This explicit project exception supersedes the global private-repository default. Other repositories retain their existing visibility unless Joel authorizes a change.
4. Never commit build artifacts, secrets, logs, JP2 source scans, or generated GeoTIFFs. Update `.gitignore` when a new generated category appears.
5. Audit file sizes before every push. Do not push a file over GitHub's 100 MB limit.
6. Keep production databases and working map records local in this public repository. This scoped privacy exception overrides the general database-backup rule. Never delete local data when untracking it. Separately reviewed, deliberately published datasets need their own authorization.
7. Use clean commit authorship. Never add AI co-author or generation-credit trailers.

2026-09-28 to 2026-09-30: how the atlas was finished (colour, baked files, merged far-zoom layer, RAID archive, inset maps, QGIS layout) is in `ATLAS FINISHING.md`.

## Public publication boundary — 2026-09-25

Share reviewed software and public historical-map evidence. Never publish passwords, API or access tokens, private keys, authentication databases, connection secrets, private service URLs or unreviewed personal notes. Before each push, audit the actual staged changes; filename exclusions do not protect files already tracked. QGIS master files and dated backups stay local under ignored storage. Importing a map must not commit or push any project backup. Preserve local originals when removing a file from public tracking. Review database contents before publishing a database change; credentials and private data override the general database-backup rule. Suspected exposure in existing history requires a redacted report and a separate remediation decision; never silently rewrite history or force-push. Public source code is not the same as an open-source license; do not invent a license on Joel's behalf.

Public release scope: `batch/` and `1911 SANBORN DOWNLOADS/` contain runtime databases, review packets, source paths and provenance. They stay local and ignored. Publish code, tests, documentation and deliberately reviewed public assets; do not sweep runtime records into a software release.
