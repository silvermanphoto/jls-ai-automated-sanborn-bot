# CLAUDE.md — Local Sanborn Batch Engine

> Open findings from the 2026-09 code review: ~/.claude/overseer/reviews/2026-09/jls-ai-automated-sanborn-bot.md and ~/.claude/overseer/reviews/2026-09/sanborn-comparison.md. Mention them to Joel at the start of each session; delete this line once none are open.

Use the local-first version 1.17 workflow. Read `AGENTS.md` for standing safety rules, `LOCAL BATCH ENGINE.md` for commands, and `AI AUTOMATED SANBORN BOT - MASTER INSTRUCTIONS.md` for the full evidence policy. Keep explanations in plain English.

## Operating boundary

- Joel's Mac performs downloads, OCR, street matching, local OSM and Kauffman rendering, affine warping, checksums, and audits. ChatGPT or Joel reviews street identity, three source centers, non-control alignment, and the final contact sheet.
- Never overwrite a source scan. Never save or close `/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/JLS Master Map File.qgz` unless Joel explicitly authorizes it in the same turn.
- Run `python3 tools/sanborn_batch.py doctor` before production. `gdal_edit.py` is required because it embeds derivation evidence inside each finished GeoTIFF.
- The normal parallel setting is three jobs. `sanborn_parallel.py` uses one shared pacer to keep actual worker launches at least two seconds apart by default; each one-tile child receives zero internal delay.

## Index-seed decisions

Every packet needs one unique independent seed from the georeferenced 1911 index, clamped to no more than 250 meters. If OCR misses the tile, returns competing locations, or is visibly wrong, prefer the queue-aware wrapper:

    python3 tools/sanborn_batch.py confirm-seed TILE \
      --preview-pixel PREVIEW_X PREVIEW_Y \
      --reviewer "Joel" \
      --note "What was visually inspected and why this center is correct"

Use exactly one coordinate method:

- `--preview-pixel X Y` is a pixel in the hash-verified preview recorded by `index-build`, not a source pixel, screenshot coordinate, QGIS-canvas coordinate, or full-resolution index pixel.
- `--map-coordinate X Y` is an existing EPSG:3857 coordinate, not longitude/latitude.

Reviewer and note are mandatory. `--replace-existing` deliberately supersedes an earlier manual seed while retaining its history. `--override-high-confidence` is required only after visual inspection proves that one unique high-confidence OCR seed is wrong. The underlying equivalent is `python3 tools/sanborn_index.py confirm ...`; it does not refresh the batch queue, so prefer the wrapper during production. A tile with locked review evidence must be reopened and rebuilt before its seed changes.

## Controls and review packets

- Use exactly three strong, distant, non-collinear controls by default. Use OSM only where street identity and geometry survive. The 1958 original topo is primary for historic streets within drawn coverage; its street-adjustment companion is supporting context only. Kauffman remains the supporting historical view in the legacy packet.
- Historical controls require at least three separate, spatially distributed check crossings. Each sheet gets its own affine fit; never transfer sheet 486 percentages. The old cardinal-v2 placements for 486/493/494 and the prior487 result are superseded, while source scans and the manual workflow remain preserved.
- A distortion exception during `sanborn_controls.py export` can waive only scale-ratio or axis-angle warnings. It cannot waive mirroring, inadequate geometry, wrong CRS, wrong neighborhood, seed-envelope failure, or an out-of-bounds control. The flag requires a nonblank note and an actual eligible warning.
- Export approval does not carry automatically into packet creation. Repeat `--allow-distortion` and the evidence note on `sanborn_batch.py packet`.
- Every review attempt gets a new `batch/reviews/tile-NNNN/packet-TIMESTAMP/` folder with an `approved-controls.points` copy. Never edit or reuse an older packet.
- 2026-07-26: `cmd_packet` seeds that folder with the control copy and then calls `sanborn_review.py create` against it, so it must pass `--replace` — the generator refuses a folder that already holds anything. Without it the packet step failed on every run since the batch engine shipped, and no sheet could reach approval or a final map. A packet whose generator fails is now removed rather than left to accumulate.
- 2026-07-26: `finish` resumes from an existing raster and ledger. When that pair came from controls a later approval replaced, resuming used to stop with a mismatched-ledger complaint and no way forward; `_ledger_predates_approval` now sets the superseded pair aside and warps again from what was approved. An unreadable ledger counts as superseded.
- 2026-07-26: A completed proposal with zero ranked triplets is a real outcome, not a transient state — two of the three currently queued sheets are in it. Anything reading the queue must present that as a dead end needing human street review, never as work still in progress.

## Final raster, resume, and QGIS

- The final GeoTIFF embeds the source hash, immutable-controls hash, recomputed affine signature, and `local-first-affine-v2` pipeline identity. Resume succeeds only when the live GeoTIFF and adjacent ledger both verify against current inputs, pixels, extent, CRS, compression, seed limits, and packet evidence. A rewritten ledger cannot certify a stale raster.
- Rebuilding a verified fixed-name tile first archives its raster, ledger, manifest, and packet evidence. Crash recovery may use only an explicitly unfinished retirement record bound to the exact paths and hashes; never restore an arbitrary older archive by matching filenames.
- Process-local, stat-aware hash caching may avoid rereading unchanged shared evidence. Any canonical-path, device, inode, size, modification-time, or change-time difference forces a new hash; a mid-read change stops the run.
- A schema-3 QGIS manifest binds the raster, ledger, source, immutable controls, review and approval records, all eight packet artifacts, OSM and Kauffman inputs, renderer/font inputs, and seed provenance.
- The exact root-level index remains outside `1911 ATLANTA SANBORNS`. Keep that group immediately below the index, expanded, with tiles in printed-number order and every raster child collapsed so RGB legend rows stay closed.
- Every completed raster uses opacity 1.0, Brightness 0, Gamma 1.0, Contrast 0 (no channel stretch), and alpha band 4. Generated QGIS code must perform duplicate preflight and rollback and must contain no project-save call.
- Earlier live Tile 236 evidence used an equivalent hash-bound plan, not a live schema-3 manifest. The present schema-3 revision was exercised in the installed QGIS 3.42.1 runtime with the real index and synthetic tiles; the unavailable QGIS MCP connection prevented a current run against Joel's open protected project. Keep those claims separate.

Version 1.17 passes all 158 engine tests with the working QGIS GDAL family, including real warping and embedded-provenance resume checks. When using QGIS GDAL, set PROJ_LIB and PROJ_DATA to its Contents/Resources/proj folder and GDAL_DATA to Contents/Resources/gdal.

2026-09-25 appearance correction: preserve the original TIFF appearance. Brightness and contrast stay at 0, gamma at 1, opacity at 100%, alpha band 4, and RGB channel stretching is disabled. This supersedes every earlier enhanced-display preset; it does not alter the source pixels.
