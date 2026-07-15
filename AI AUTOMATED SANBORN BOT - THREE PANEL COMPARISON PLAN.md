# Three-panel Sanborn comparison workspace

## Build plan for faster control-point discovery

**Plan version:** 1.1
**Created:** 2026-07-14
**Companion manual:** `MASTER_KAUFFMAN.md` version 1.6 or later

---

## 1. Outcome

Build a small local comparison workspace that places these three views side by side:

1. the newly downloaded, north-oriented 1911 Sanborn sheet;
2. the georeferenced 1921 Atlanta Kauffman map at the suspected location;
3. current OpenStreetMap at the same modern coordinates.

The workspace should make the slowest part of the job—identifying three defensible control intersections—fast, visible, and auditable. It must not attempt to replace historical judgment with automatic street matching.

Once three controls are approved, the existing `tools/sanborn_georeference.py` helper should perform the full-resolution warp, transparency, compression, and verification in seconds.

The production target is:

- **five minutes** for a clear sheet with three unmistakable surviving intersections;
- **ten minutes** for a normal sheet that contains one rejected candidate or one street-name problem;
- no artificial deadline for a heavily demolished or historically ambiguous sheet.

---

## 2. Core design decision

The first version should be a self-contained local comparison window, not a new QGIS plugin.

Reasons:

- the mission-critical QGIS project must remain unsaved;
- QGIS window management and repeated file-dialog work caused more delay than the raster processing;
- a dedicated window can preserve one compact control table instead of forcing repeated screenshot inspection;
- source-image pixel coordinates and modern map coordinates can be recorded directly from clicks;
- the tool can hand the reviewed controls to the already-tested warp helper;
- QGIS remains available for final visual verification and layer placement, where it is most useful.

The workspace may be displayed in a local browser window or a lightweight local application. The implementation choice should be made only after confirming the available local libraries. It must run locally, must not upload Joel's maps, and must not require editing the master QGIS project.

Live QGIS verification completed on 2026-07-14 established an important coordinate rule: the project and OSM use EPSG:3857, while the Kauffman and 1911 index rasters are natively EPSG:4326. The comparison workspace must explicitly reproject the historical references into the synchronized EPSG:3857 center/right view. It must not assume that all three source files share one native CRS.

---

## 3. Proposed screen

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Tile 474 | north: up | EPSG:3857 | location: Oakland / Cabbagetown          │
├────────────────────────┬────────────────────────┬────────────────────────────┤
│ 1911 SANBORN SOURCE    │ 1921 KAUFFMAN         │ CURRENT OPENSTREETMAP      │
│                        │                        │                            │
│ independent pixel view │ synchronized map view  │ synchronized map view      │
│ rotate / zoom / pan     │ zoom / pan             │ zoom / pan                 │
│ click historic crossing│ click historic target  │ click surviving target     │
├────────────────────────┴────────────────────────┴────────────────────────────┤
│ Candidate controls                                                          │
│ 1. Carroll × Tennelle | source (6452,3565) | OSM | accepted | high          │
│ 2. Carroll × Shelton  | source (6345,5592) | OSM | accepted | high          │
│ 3. Boulevard × Tennelle | source (2652,5056) | Kauffman-assisted | medium   │
├──────────────────────────────────────────────────────────────────────────────┤
│ Triangle coverage: wide / non-collinear | cardinal check: pending           │
│ [Reject candidate] [Approve three controls] [Create points] [Run preview]    │
└──────────────────────────────────────────────────────────────────────────────┘
```

The Sanborn panel is an image-coordinate view. The Kauffman and OSM panels are map-coordinate views and must remain synchronized to the same extent and scale.

The tool should never pretend that the unreferenced Sanborn source already shares map coordinates. It becomes geographically linked only through the explicit control rows.

---

## 4. One-pass working method

### Step 1 — Preflight

Record once:

- tile number and source path;
- source dimensions and checksum;
- compass direction and any display rotation needed to put north up;
- approximate EPSG:3857 bounding box from the 1911 index, current QGIS view, or Kauffman location;
- Kauffman source path;
- OpenStreetMap attribution and connection status;
- protected project path and modification time.

Never keep rediscovering these facts during the same run.

### Step 2 — Read the Sanborn sheet once

Transcribe the useful evidence into one short list:

- clearly printed street names;
- distinctive street-network shapes;
- railroad, cemetery, river, industrial, or institutional landmarks;
- compass direction;
- likely vanished streets;
- possible modern survivors.

Do not begin by chasing every modern name individually. First understand the historic network as a shape.

### Step 3 — Locate the network on Kauffman

Use the center panel to establish:

- the Atlanta neighborhood;
- approximate latitude/longitude placement;
- which streets were historically east–west or north–south;
- how vanished streets connected before highways, stadiums, or urban renewal;
- whether a similar modern street name is geographically plausible.

Kauffman is the permanent citywide locator, not modern survey ground truth.

### Step 4 — Test survival on OSM

Use the right panel to classify each promising intersection:

- **surviving OSM:** both street identities and the intersection geometry still survive;
- **Kauffman-assisted:** the historic relationship is clear, but the modern intersection no longer survives;
- **rejected:** identity, geometry, or placement is doubtful.

The interface should automatically mark a target clicked in the Kauffman panel as Kauffman-assisted. It must never label that row as surviving OSM.

### Step 5 — Build one wide triangle

Choose exactly three accepted controls by default:

- far apart on the source sheet;
- non-collinear;
- preferably forming a large triangle across three outer regions;
- using OSM wherever the intersection genuinely survives;
- using Kauffman assistance only when explicitly recorded and historically justified.

Do not add a fourth point by habit. Do not use higher-order warping to conceal uncertain identity.

### Step 6 — Preview and reject quickly

Create a low-resolution diagnostic transformation before the full warp.

The preview should show:

- the transformed Sanborn sheet over OSM;
- the transformed Sanborn sheet against Kauffman;
- the three control markers;
- at least one non-control street or corridor;
- expected north–south and east–west street families.

Reject immediately if the grid is mirrored, strongly tilted, compressed sideways, or sheared into a visibly implausible shape. A zero-residual three-point affine fit is not proof of geographic accuracy.

### Step 7 — Approve, export, and run

After the preview passes:

1. write the QGIS-compatible `.points` file;
2. write the comparison-session record;
3. run `tools/sanborn_georeference.py` for the final raster;
4. verify the generated `.georef.json` ledger;
5. load exactly one GeoTIFF into QGIS at the root level beside the other 1911 sheets;
6. blink against OSM and Kauffman at 100% opacity;
7. leave the protected QGIS project unsaved.

---

## 5. Control table: required information

Every candidate row should contain:

| Field | Purpose |
|---|---|
| Historic street names | What the 1911 sheet actually says |
| Modern street names | Current identity, if it survives |
| Source X and Y | Exact source-image pixel crossing |
| Target X and Y | Exact EPSG:3857 map coordinate |
| Evidence type | Surviving OSM, Kauffman-assisted, or rejected |
| Confidence | High, medium, or low |
| Acceptance state | Candidate, accepted, or rejected |
| Rejection reason | False name match, demolished geometry, unclear crossing, bad triangle, or other |
| Notes | Name changes, highway removal, cardinal orientation, or historical context |

The interface should retain rejected candidates. They are valuable evidence and prevent a later agent from repeating the same false lead.

Tile 474 should appear as the regression example:

- Carroll–Tennelle: surviving OSM, accepted;
- Carroll–Shelton: surviving OSM, accepted;
- Boulevard–Tennelle: Kauffman-assisted, accepted;
- Wyman–Shelton: rejected because the geometry does not survive reliably;
- Rinehardt/Reinhardt–Shelton: rejected false friend;
- Boulevard–Decatur: rejected as unclear or outside the usable sheet relationship.

---

## 6. Automatic warnings

The workspace should warn, but not silently decide, when:

- fewer or more than three controls are enabled;
- two controls are duplicates;
- source or target points are nearly collinear;
- the triangle covers only a small central portion of the sheet;
- a source point lies outside the image;
- a target lies far outside the recorded approximate location;
- a Kauffman click is being described as OSM ground truth;
- the target triangle implies a severe X/Y scale disagreement;
- the preview rotates known cardinal streets implausibly;
- an output or ledger filename already exists;
- the protected project modification time changes during processing.

Warnings should be written in plain English. The tool should show the reason and let the operator inspect it; it should not bury the judgment in a numerical score.

---

## 7. Files produced per tile

The comparison run should produce small, reusable records alongside the final raster:

```text
Sanborn 1911 -- Tile 474_comparison.json
Sanborn 1911 -- Tile 474_candidates.csv
Sanborn 1911 -- Tile 474_3points_kauffman_assisted.points
Sanborn 1911 -- Tile 474_contact-sheet.png
Sanborn 1911 -- Tile 474_preview.png
Sanborn 1911 -- Tile 474_georeferenced.tif
Sanborn 1911 -- Tile 474_georeferenced.georef.json
```

The large source, previews, and GeoTIFF remain outside Git. The small comparison record, candidate table, and final points file may be preserved in the private repository.

Minimum comparison record:

```json
{
  "tile": 474,
  "source": "Sanborn 1911 -- Tile 474.jp2",
  "source_rotation_degrees": 0,
  "target_crs": "EPSG:3857",
  "approximate_location": "Oakland Cemetery / Cabbagetown",
  "controls": [
    {
      "historic_name": "Carroll Street x Tennelle Street",
      "source_x": 6452,
      "source_y_qgis": -3565,
      "target_x": -9391764.83930054,
      "target_y": 3995400.81652537,
      "evidence": "surviving-osm",
      "confidence": "high",
      "status": "accepted"
    }
  ],
  "rejected_candidates": [],
  "cardinal_grid_check": "passed",
  "kauffman_check": "passed"
}
```

---

## 8. Safety boundaries

The comparison workspace must:

- treat original scans and Kauffman as read-only;
- write every derivative under a new filename;
- operate without saving `JLS Master Map File.qgz`;
- keep Global Opacity at 100%; comparisons use visibility blinking or separate panes;
- create destination alpha for warped edges and preserve genuine black ink;
- keep house numbers out of the control logic because Atlanta renumbered in 1927;
- never solve or evade LOC human-verification challenges;
- never upload source maps to an external service;
- never promote a suggested street match to accepted without visible evidence;
- never add a fourth control or a rubber-sheet transform automatically;
- never delete a GeoTIFF when removing an accidental QGIS layer entry.

---

## 9. Build sequence

### Milestone 0 — Live-environment discovery

Before writing QGIS integration code, verify through the live QGIS connection:

- installed QGIS version;
- project CRS;
- exact OSM, Kauffman, and 1911 index layer names and providers;
- whether the index is raster or vector and what usable fields exist;
- safe layer-tree insertion behavior;
- available rendering and processing operations.

Live discovery was completed on 2026-07-14 through the QGIS MCP connection.

Verified environment:

```text
QGIS_VERSION=3.42.1-Münster
PROJECT_FILE=/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/JLS Master Map File.qgz
PROJECT_CRS=EPSG:3857
PROJECT_LAYER_COUNT=42
PROJECT_DIRTY_IN_MEMORY=true
```

Verified reference layers:

| Exact layer name | Type/provider | Native CRS | Dimensions/bands | Root level | Opacity |
|---|---|---|---|---|---:|
| `Open Street Map` | raster, `wms` provider using an XYZ tile URL | EPSG:3857 | streaming tile layer | yes | 1.0 |
| `1921 Atlanta Kauffman Map_modified` | raster, `gdal` | EPSG:4326 | 19035 × 17464, 3 bands | yes | 1.0 |
| `1911 Sanborn Index Orthorectified` | raster, `gdal` | EPSG:4326 | 7251 × 6957, 3 bands | yes | 1.0 |
| `Sanborn 1911 -- Tile 474_georeferenced` | raster, `gdal` | EPSG:3857 | 7735 × 9239, 4 bands | yes | 1.0 |

Verified source paths:

```text
KAUFFMAN=/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/Stage 1 -Orthorectified Atlanta Maps to print/1921 Atlanta Kauffman Map_modified.tif
INDEX=/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/Stage 1 -Orthorectified Atlanta Maps to print/1911 Sanborn Index Orthorectified.tif
TILE_474=/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/AI AUTOMATED SANBORN BOT/1911 SANBORN DOWNLOADS/Sanborn 1911 -- Tile 474_georeferenced.tif
```

Tile 474 is verified with `QgsMultiBandColorRenderer`, renderer opacity `1.0`, and renderer alpha band `4`. It is a direct child of the layer-tree root and sits among the 1911 layers.

The 1911 index is a raster, not a vector layer. It therefore has no tile-number field to query. The first implementation must use visual index reading, a recorded approximate coordinate, or a separately created lookup table; it must not invent an index attribute schema.

No Processing algorithm identifier has yet been selected. Before implementation code invokes a QGIS Processing operation, verify the exact installed algorithm identifier through the live server.

### Milestone 1 — Static three-panel contact sheet

Inputs:

- north-oriented Sanborn preview;
- approximate EPSG:3857 extent;
- Kauffman crop;
- OSM crop.

Output:

- one labeled PNG with identical center/right extent and scale;
- a starter comparison JSON record.

This immediately reduces repeated window switching even before the click interface exists.

### Milestone 2 — Local interactive picker

Add:

- independent zoom and pan for the Sanborn source;
- synchronized zoom and pan for Kauffman and OSM;
- crosshair clicks and editable labels;
- evidence classification;
- accepted and rejected candidate table;
- triangle coverage warning;
- QGIS `.points` export.

### Milestone 3 — Diagnostic preview

Connect the picker to a reduced-resolution version of the existing warp helper. Show OSM and Kauffman QA previews before enabling the final-run button.

### Milestone 4 — Full-resolution handoff

Run the existing tested helper, show its verification ledger, then guide final QGIS loading. Automatic QGIS insertion should be attempted only after live API verification; otherwise use the known safe manual root-level procedure.

### Milestone 5 — Candidate assistance

Only after the deterministic workflow is stable, add optional assistance such as:

- OCR suggestions for street labels;
- likely-name search against modern and historical lists;
- visual suggestions for intersection pixels;
- warning when a modern same-name street is geographically inconsistent.

These remain suggestions. Acceptance always requires a visible source crossing and a justified target.

---

## 10. Regression and acceptance tests

### Tile 474 regression

The system must:

- reproduce the three accepted controls exactly;
- preserve the two rejected false-friend explanations;
- create a 7735 × 9239, four-band output at full resolution;
- reproduce accepted band checksums `55125`, `14515`, `65108`, and `51289` when using the same source and settings;
- show alpha minimum 0 and maximum 255;
- leave the protected project modification time unchanged;
- never label Boulevard–Tennelle as surviving OSM.

### New ordinary-sheet test

Use a sheet with three surviving OSM intersections. A first-time operator or normal-effort agent should produce the approved point file and diagnostic preview in five minutes without reopening the same evidence repeatedly.

### Difficult-sheet test

Use a sheet affected by highway construction or urban renewal. The system passes if it records uncertainty clearly and refuses to disguise a weak third point as OSM ground truth, even when the run takes longer than ten minutes.

### QGIS hierarchy test

Loading the final raster must result in exactly one root-level entry beside the other 1911 sheets. It must not land inside a selected group such as `River REM`.

---

## 11. Definition of done

The three-panel workspace is ready for production when:

- one local command or agent action opens the three views;
- center and right panels stay synchronized;
- source and target clicks populate one persistent table;
- rejected candidates are retained with reasons;
- OSM and Kauffman evidence cannot be silently confused;
- exactly three approved controls export to a valid QGIS points file;
- Tile 474 passes the checksum regression;
- the full raster helper runs without GUI repetition;
- all comparisons keep opacity at 100% and use alpha correctly;
- the protected QGIS project remains unchanged on disk;
- final QGIS placement is verified as one root-level 1911 layer;
- the complete normal-sheet run can reliably finish within ten minutes.

---

## 12. Permanent efficiency lessons

1. Raster processing is no longer the bottleneck; historical street identity is.
2. Read the Sanborn network once, locate it on Kauffman, then test survival on OSM.
3. Keep one control table instead of repeatedly re-reading screenshots.
4. Record rejected candidates so false friends are not rediscovered.
5. Three distant controls are the default; a fourth point is not an automatic improvement.
6. A zero-residual fit can still be geographically wrong.
7. Cardinal street orientation is the fastest whole-sheet rejection test.
8. Kauffman verification is cheaper than rebuilding a bad final raster.
9. Deterministic code should handle warp, alpha, compression, checksums, and file safety.
10. QGIS should handle final geographic judgment, blinking, and layer organization.
11. Click Add once, close the dialog, and verify one root-level layer before continuing.
12. Five minutes is a fast path; ten minutes is the reliable target; historical ambiguity outranks the clock.
