# AI AUTOMATED SANBORN BOT

## Master operating instructions for georeferencing historic Sanborn sheets in QGIS

**Version:** 1.7
**Created:** 2026-07-13  
**Primary environment:** QGIS on macOS, controlled by Sol/Codex through Computer Use  
**Default effort target:** Normal effort  
**Primary objective:** Locate, download, and georeference a historic Sanborn raster against a current OpenStreetMap layer using a very small number of high-confidence, widely separated, surviving street-centerline intersections.

---

## 0. Normal-effort execution summary

This is the shortest safe route. Use the detailed sections only when a step fails or a judgment is uncertain.

If the requested 1911 sheet is not already local, complete the LOC discovery and download workflow in Section 19 before beginning the steps below.

1. Record the source TIFF, output path, GCP path, project CRS, and protected `.qgz` modification time.
2. Confirm the source and output filenames are different and the proposed output does not already exist.
3. Open the source in QGIS Georeferencer and fit the whole sheet in view.
4. If the sheet is one of the four completed 1911 sheets listed in Section 18, load its final `_cardinal_v2.points` file and skip control-point reconstruction.
5. Otherwise locate the sheet citywide against **1921 Atlanta Kauffman Map_modified**; never assume the next sheet is in Summerhill or any other previously visited neighborhood.
6. Identify exactly three distant, non-collinear, surviving street-centerline intersections against OSM and record them in one table.
7. Capture modern coordinates from the QGIS canvas in the project CRS; manually enter them into the Georeferencer coordinate dialogs.
8. Use Helmert only as a rigid-scale diagnostic. If three verified points show a global X/Y scale mismatch, use `Polynomial 1` with exactly those three points.
9. Explicitly save `<source stem>_3points.points` by clicking **Save**.
10. Prefer `tools/sanborn_georeference.py` for the deterministic warp and file audit. Otherwise set cubic resampling, lossless DEFLATE compression, a new `_georeferenced.tif` output, **Save GCP points**, and **Load in project when done**.
11. Add the completed raster exactly once at the root level beside the other 1911 sheets; never leave it inside the currently selected group.
12. Visually check the three anchors and at least one surviving non-control street against OSM.
13. Blink the complete sheet against Kauffman to verify historical placement, orientation, and vanished streets.
14. Verify the output and GCP files exist and the protected `.qgz` modification time is unchanged.
15. Leave QGIS open. Never save or close the dirty master project unless the user separately authorizes it.

Decision tree:

```text
Same source sheet and final Section 18 .points file exists?
├─ Yes → Load source → Load final cardinal_v2.points → Verify settings → New output name → Run → OSM + Kauffman QA
└─ No  → Locate neighborhood and street grid on Kauffman → Discover 3 surviving OSM intersections → Capture targets → Add GCPs
         ├─ Helmert third-point residual is small → Keep Helmert → Run → QA
         └─ Helmert mismatch is large but all 3 controls verify → Polynomial 1 → Run → QA
```

Hard stops:

- credential prompt → user takes over;
- overwrite prompt → choose a new filename;
- large unexplained residual → do not generate output;
- project-save or project-close prompt → do not proceed without the user;
- reset/clear-GCP prompt → cancel the reset;
- Save GCP dialog → click **Save**.

---

## 1. What this manual is designed to accomplish

This is a self-contained runbook for repeating the successful georeferencing of a historic Sanborn fire-insurance sheet in QGIS. It is written so that a future Sol/Codex session can execute the job efficiently without rediscovering the workflow, re-researching already-known QGIS behavior, or endangering the master QGIS project.

The process has two modes:

1. **Same-sheet fast path:** Reopen the exact 1911 sheet already completed and load its saved GCP file. This should take very little reasoning.
2. **New-sheet general path:** Discover three surviving intersections, collect modern coordinates, fit the appropriate global transform, save the GCPs, generate a new GeoTIFF, and visually verify it.

This runbook deliberately separates:

- permanent output files from temporary QGIS session state;
- historical street identification from coordinate capture;
- diagnostic transformations from the final transformation;
- control-point fit statistics from genuine visual validation;
- safe local file creation from saving the mission-critical master project.

---

## 2. Non-negotiable safety rules

These rules override convenience, speed, and every optional instruction in this file.

### 2.1 Never modify the source raster

- Treat the original TIFF as read-only.
- Never use the original filename as the output filename.
- Never allow an overwrite prompt to replace the source.
- Always create a clearly named derivative such as:

  ```text
  <original stem>_georeferenced.tif
  ```

- If that output already exists, create a new version:

  ```text
  <original stem>_georeferenced_v2.tif
  <original stem>_georeferenced_YYYYMMDD_HHMM.tif
  ```

### 2.2 Never save the mission-critical master QGIS project unless the user explicitly authorizes it in that turn

The protected project is:

```text
/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/JLS Master Map File.qgz
```

Rules:

- Reading and opening the project is allowed.
- Panning, zooming, opening panels, loading a temporary layer, and using the Python console are allowed.
- Do **not** click QGIS **Save Project**.
- Do **not** press Command-S.
- Do **not** save the loaded georeferenced layer into the master project.
- A leading `*` in the QGIS title means the in-memory project is dirty. That is expected after adding a layer or changing the view; it does **not** mean the disk file has been changed.
- Do not close QGIS at the end of the job. Closing a dirty project can create a save/discard prompt and risks the master file. Leave the session open for the user.
- Verify the `.qgz` modification time before and after the job. It must remain unchanged.

If permanent project integration is later desired, ask the user to authorize either:

- saving a duplicate `.qgz` under a new name; or
- intentionally saving the master project.

Never infer that authorization from the georeferencing request itself.

### 2.3 Credentials and authentication belong to the user

- If QGIS requests a master password, database password, token, or other secret, pause and hand control to the user.
- Do not type, read, capture, narrate, or store the credential.
- After the user confirms authentication is complete, resume from a fresh QGIS state inspection.
- Never cancel an authentication dialog merely because it is unfamiliar.

### 2.4 Save GCPs means click **Save**, not **Cancel**

This is a critical learned rule.

- In the macOS **Save GCP Points** dialog, explicitly name the file and click **Save**.
- Do not confuse the Save dialog with a reset/clear confirmation.
- If a later dialog says **Reset georeferencer and clear all GCP points?**, cancel the reset unless clearing was explicitly intended.
- A safe GCP filename is:

  ```text
  <original stem>_3points.points
  ```

- After saving, verify that the `.points` file exists and is nonempty.

### 2.5 Default to exactly three strong points

- Use three non-collinear, widely separated, high-confidence street-centerline intersections.
- Prefer a large triangle spanning three corners or outer regions of the historic map.
- Do not add a fourth point by habit.
- A fourth point can force a least-squares compromise or reveal local historical/map distortion, but it must not be added to the final fit without a reason.
- Never use high-order polynomial or thin-plate-spline warping merely to make uncertain streets appear to fit.

### 2.6 Use street centerlines, not buildings or house numbers

- Atlanta house numbers changed in 1927; ignore house-number correspondence.
- Buildings may have been demolished, rebuilt, shifted, renumbered, or replaced.
- Interstate construction removed or altered much of the historic fabric.
- Use the geometric center of surviving road intersections.
- The preferred evidence is a surviving, unchanged intersection of streets whose identity and geometry remain trustworthy.

### 2.7 Transparency and layer rendering defaults

This is a permanent output and display rule.

- Every georeferenced raster must have a real alpha band.
- Empty areas created when the sheet is rotated, stretched, or warped must have alpha value `0` (fully transparent), not black RGB pixels.
- Valid map pixels must remain fully opaque (normally alpha value `255`).
- In QGIS, **Layer Properties > Transparency > Global Opacity** must always be set to **100%**.
- In QGIS, **Layer Properties > Symbology > Layer Rendering > Color rendering**, apply these defaults to every completed Sanborn raster: **Brightness +50**, **Gamma 1.2**, and **Contrast +20**.
- Never lower Global Opacity to reveal the basemap. For comparison, blink the historic layer off and on instead.
- Never make RGB value `0,0,0` globally transparent. Sanborn linework and text contain genuine black ink that must remain visible.
- When using GDAL directly, include `-dstalpha` in `gdalwarp`. This creates the destination alpha band and masks the otherwise black triangular or irregular areas outside the warped source footprint.
- After loading or reloading the layer, explicitly set renderer opacity to `1.0` and verify that QGIS recognizes the fourth band as Alpha.

### 2.8 OpenStreetMap is ground truth; the 1921 Kauffman map is the required historical cross-check

Use the reference layers in this order:

1. **OpenStreetMap has priority** wherever a street centerline or intersection unquestionably survives.
2. **1921 Atlanta Kauffman Map_modified** is the canonical near-contemporary reference for streets that vanished, were severed by interstate construction, or cannot be located directly on OSM.
3. The Kauffman map was itself georeferenced by hand. Treat it as a strong historical constraint, not as higher-accuracy ground truth than OSM.

Before finalizing any 1911 Sanborn sheet:

- compare it against both OSM and the Kauffman layer;
- confirm latitude/longitude placement is plausible in the Kauffman street grid;
- confirm streets historically running east–west remain east–west, and streets historically running north–south remain north–south;
- reject an affine fit that forces an obviously diagonal, sheared, or rubber-banded grid merely because three mathematical controls have zero residual;
- if an OSM-derived control conflicts with the Kauffman grid, investigate whether the supposed intersection actually survives, whether it was estimated across a demolished area, or whether the wrong modern street was selected;
- prefer a shape-preserving similarity/cardinal-grid solution over an affine shear when the surviving ground truth and historical orientation support it.

Never use Kauffman building footprints or labels as controls. Use its street centerlines and intersections only.

### 2.9 Never assume the neighborhood; Kauffman is the permanent citywide locator

Summerhill was the first proving ground, not the geographic default. Future Sanborn sheets may come from any part of Atlanta.

At the beginning of every new-sheet run:

1. Read the historic street names and the shape of the street network from the Sanborn sheet.
2. Turn on **1921 Atlanta Kauffman Map_modified** and locate the matching network anywhere in the city.
3. Use Kauffman to establish the approximate neighborhood, rotation, latitude/longitude placement, vanished-street relationships, and cardinal orientation.
4. Then move to OpenStreetMap and identify which intersections genuinely survive as modern ground-truth controls.

Kauffman is always part of the workflow, including neighborhoods where modern redevelopment, expressways, stadiums, urban renewal, or street renaming removed most of the 1911 grid. It is the historical bridge between the unidentified Sanborn sheet and the modern city.

Do not promote Kauffman to modern survey accuracy. If fewer than three trustworthy surviving OSM intersections can be found, do not invent modern control points or silently treat Kauffman as equal to OSM. Record the shortage, identify any overlapping already-georeferenced historic sheets, and ask Joel whether to proceed with a clearly labeled lower-confidence Kauffman-assisted fit.

### 2.10 New raster layers belong at the root level in the chronological 1911 cluster

QGIS often inserts a new layer relative to the currently selected layer-tree item. If a folder is selected, the new raster can silently land inside that folder even when the folder is hidden or disabled.

Before adding a finished Sanborn raster:

1. In the Layers panel, click a known **root-level 1911 raster**, not a group heading and not a child layer.
2. In the Data Source Manager, select the one intended GeoTIFF and click **Add exactly once**. The Add button may give no visible acknowledgement and the dialog may remain open; do not click repeatedly.
3. Close the Data Source Manager and verify that exactly one new layer exists.
4. Confirm that its indentation matches the other root-level 1911 sheets and that it sits in the chronological 1911 cluster.
5. If it landed inside a group, move one copy to the root-level 1911 cluster and remove only the accidental duplicate layer entries. Do not delete the GeoTIFF from disk.

The approved ordering pattern is:

```text
...1906 map
Sanborn 1911 -- Tile <number>_georeferenced
other 1911 Sanborn sheets
1911 index and related 1911 layers
1921 Atlanta Kauffman Map_modified
...
```

These are in-memory layer-tree changes. Never save the protected master project merely to preserve them.

---

## 3. Required inputs and session ledger

At the start of every run, create a compact ledger in reasoning or commentary. Do not repeatedly rediscover these values.

```text
SOURCE_RASTER=
PROTECTED_PROJECT=
OUTPUT_DIRECTORY=
OUTPUT_RASTER=
GCP_FILE=
BASEMAP=
HISTORICAL_REFERENCE=1921 Atlanta Kauffman Map_modified
PROJECT_CRS=
QGIS_APP=/Applications/QGIS.app
PROJECT_DISK_MTIME_BEFORE=
SOURCE_SIZE_OR_HASH_BEFORE=
```

For the completed 1911 example:

```text
SOURCE_RASTER=/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/1911 Sanborn Map/! 1911 Sanborn 486 Shmuel Yankel B.tif
PROTECTED_PROJECT=/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/JLS Master Map File.qgz
OUTPUT_DIRECTORY=/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/1911 Sanborn Map
OUTPUT_RASTER=/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/1911 Sanborn Map/! 1911 Sanborn 486 Shmuel Yankel B_georeferenced_v2.tif
GCP_FILE=/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/1911 Sanborn Map/! 1911 Sanborn 486 Shmuel Yankel B_cardinal_v2.points
BASEMAP=Open Street Map
HISTORICAL_REFERENCE=1921 Atlanta Kauffman Map_modified
PROJECT_CRS=EPSG:3857 - WGS 84 / Pseudo-Mercator
QGIS_APP=/Applications/QGIS.app
```

Record the following before taking actions:

- source dimensions and file size;
- whether the intended output already exists;
- project CRS;
- project file modification time;
- whether OpenStreetMap is enabled;
- whether 1921 Atlanta Kauffman Map_modified is available and enabled for historical verification;
- whether QGIS is already centered on the correct neighborhood;
- whether the project is already dirty before the run.

Use read-only filesystem inspection for these checks. Do not open Finder merely to verify a filename.

---

## 4. Token-efficient operating strategy

The following practices allow a normal-effort Sol run to succeed without excessive exploration.

### 4.1 Use the right information source

- Use QGIS accessibility text for buttons, dialogs, checkboxes, text fields, and tables.
- Use screenshots only for maps, street labels, intersection placement, visual alignment, or when the accessibility tree is incomplete.
- Use read-only shell inspection for filenames, sizes, timestamps, and file existence.
- Use the QGIS Python console for exact map-coordinate conversion.
- Browse the web only when street identity cannot be resolved from the historic sheet, OSM, the master map stack, and authoritative local sources already available.

### 4.2 Refresh UI state after every meaningful action

- QGIS element indexes are not stable.
- Obtain a fresh app state after opening a dialog, saving a file, minimizing/restoring a window, or changing a setting.
- Never reuse a stale element index from an earlier dialog.
- Use the exact application path `/Applications/QGIS.app` because multiple QGIS-like windows or bundles may exist.

### 4.3 Avoid repeated full screenshots

- Request a full state when entering a new window or when the view is visually ambiguous.
- After that, use state diffs when sufficient.
- Do not capture a new screenshot merely to confirm that text was typed if the accessibility tree already exposes the value.

### 4.4 Maintain one intersection table

Do not reason about street candidates in scattered prose. Maintain a compact table:

| ID | Historic intersection | Survives? | Name unchanged? | Geometry trustworthy? | Historic source point | Modern target coordinate | Decision |
|---:|---|---|---|---|---|---|---|
| 0 |  |  |  |  |  |  |  |
| 1 |  |  |  |  |  |  |  |
| 2 |  |  |  |  |  |  |  |

This table becomes the recovery record if QGIS loses the current GCP session.

### 4.5 Narrate only decision-relevant street discovery

The user wants to follow the historical reasoning. Narration should be concise and useful:

- identify the historic street being read;
- state its likely modern counterpart;
- explain why an intersection is accepted or rejected;
- state how the next point expands the triangle;
- report residuals and what they imply.

Avoid narrating every mouse movement or dialog click.

---

## 5. Same-sheet fast path

Use this when the source is an exact byte-for-byte copy of completed sheet 485, 486, 493, or 494. Section 18 is the authoritative reconstruction record.

### 5.1 Match the exact source to its final control file

| Source sheet | Load this control file |
|---|---|
| `1911 Sanborn 485 Shmuel Yankel A.tif` | `1911 Sanborn 485 Shmuel Yankel A_cardinal_v2.points` |
| `! 1911 Sanborn 486 Shmuel Yankel B.tif` | `! 1911 Sanborn 486 Shmuel Yankel B_cardinal_v2.points` |
| `1911 Sanborn 493 Shmuel Yankel D.tif` | `1911 Sanborn 493 Shmuel Yankel D_cardinal_v2.points` |
| `1911 Sanborn 494 Shmuel Yankel C.tif` | `1911 Sanborn 494 Shmuel Yankel C_cardinal_v2.points` |

Do not use the earlier `_3points.points` files for exact reconstruction of these four corrected sheets. Those records are retained only as process history.

### 5.2 Fast procedure

1. Open QGIS Georeferencer and load the exact source TIFF.
2. Click **Load GCP Points** and select the matching `_cardinal_v2.points` file.
3. Confirm that exactly three enabled rows appear and the target CRS is `EPSG:3857`.
4. Confirm `Polynomial 1`, cubic resampling, DEFLATE compression, and destination alpha.
5. Choose a new output name if the accepted `_georeferenced_v2.tif` already exists.
6. Start georeferencing and load the result in the current QGIS session.
7. Keep Global Opacity at 100%.
8. Blink the result against OSM and the complete 1921 Kauffman grid.
9. Confirm shared streets continue plausibly into neighboring 1911 sheets.
10. Do not save the master `.qgz`; leave QGIS open.

Do not manually re-find these controls unless the saved final file is missing, corrupt, or visibly incompatible with the exact source raster. Use the complete numerical tables and rationale in Section 18 for recovery.

---

## 6. New-sheet general workflow

### Phase A — Preflight and protection

1. Confirm QGIS is open with the intended project.
2. Confirm OpenStreetMap is available as the ground-truth reference.
3. Confirm **1921 Atlanta Kauffman Map_modified** is available as the historical cross-check.
4. Confirm the project CRS and record its EPSG code.
5. Record the disk modification time of the protected `.qgz`.
6. Record the source TIFF size and dimensions.
7. Confirm the intended output does not already exist.
8. If the user has already zoomed to the correct neighborhood, preserve that view.
9. If credentials appear, hand off to the user.
10. Open Georeferencer and load the source raster.
11. Click **Zoom to Layer** so the whole sheet is visible.

### Phase B — Read the historic sheet as a street grid

Before clicking points, transcribe the visible street framework.

1. Read every clear street name along the sheet edges and internal blocks.
2. Separate streets into the two parallel families visible on the paper.
3. Determine how the historic sheet is rotated relative to modern north.
4. Do not assume “horizontal on the TIFF” means east–west in Atlanta.
5. Build a simple orientation table:

   | Historic sheet direction | Historic streets | Modern map direction |
   |---|---|---|
   | Horizontal on image |  |  |
   | Vertical on image |  |  |

6. Note renamed streets separately.
7. Note streets destroyed or severed by interstate construction.
8. Ignore parcel numbers, building footprints, and house numbers during initial control selection.

For the completed sheet, the historic page read as:

```text
Image-horizontal streets: Washington, Pulliam, Central, South Pryor
Image-vertical streets: Rawson, Clarke, Fulton
```

The page was effectively rotated relative to the current OSM view.

### Phase C — Select three control intersections

Candidate priority, highest first:

1. Same two street names survive unchanged and intersect at the same centerline geometry.
2. One name changed, but the identity is proven by authoritative historical/current mapping and the physical intersection survives.
3. Street centerline is preserved although surrounding buildings vanished.
4. Intersection lies near an outer corner of the historic sheet.

Reject a candidate when:

- either street no longer exists at the crossing;
- an interstate ramp or realignment moved the centerline;
- a modern road only approximately follows the old road;
- the identity depends on post-1927 house numbers;
- the location is inferred only from a building footprint;
- the street label is ambiguous;
- the candidate would cluster near another GCP instead of enlarging the triangle.

Triangle design:

- Point 1: trustworthy intersection near one outer region.
- Point 2: far away along another side, preferably at the opposite end of a surviving street.
- Point 3: far from the line through points 1 and 2, creating a large triangle.
- Avoid three nearly collinear points.

### Phase D — Capture exact modern coordinates

The modern target coordinate must come from the current QGIS canvas in the project CRS.

#### Preferred method: QGIS Python console conversion

Open the QGIS Python console and initialize:

```python
from qgis.PyQt.QtCore import QPoint
c = iface.mapCanvas()
```

To inspect canvas geometry:

```python
print(c.size())
print(c.mapTo(iface.mainWindow(), QPoint(0, 0)))
```

To convert a canvas-local pixel to a map coordinate:

```python
print(c.getCoordinateTransform().toMapCoordinates(QPoint(CANVAS_X, CANVAS_Y)))
```

On this macOS/QGIS setup, the Python console occasionally requires Return twice before a line executes and a new `>>>` prompt appears.

#### Converting a screenshot point to canvas-local pixels

Do not reuse hardcoded window offsets from a prior run. Derive them from the current screenshot and current canvas size.

Let:

```text
(sx0, sy0) = top-left of the visible main map canvas in the screenshot
(sw, sh)   = width and height of that canvas in the screenshot
(sx, sy)   = the chosen intersection in the screenshot
(cw, ch)   = QGIS canvas width and height reported by c.size()
```

Then:

```text
CANVAS_X = round((sx - sx0) * cw / sw)
CANVAS_Y = round((sy - sy0) * ch / sh)
```

Use those values in `toMapCoordinates(QPoint(...))`.

Sanity checks:

- Intersections on the same modern east–west road should have nearly identical northings.
- Intersections on the same modern north–south road should have nearly identical eastings.
- If that is not true, the screenshot point or canvas conversion is wrong.
- Store at least six decimal places even if the visible table rounds them.

#### Alternative method: coordinate dialog “From Map Canvas”

Use only if it behaves reliably in the current QGIS session. During the successful run, this mode unexpectedly activated a pan-like interaction and was less reliable than direct coordinate entry. The robust default is:

1. compute the modern target coordinate first;
2. click the historic point in Georeferencer;
3. type X and Y manually into **Enter Map Coordinates**.

### Phase E — Add each GCP in Georeferencer

For each point:

1. Ensure **Add GCP Point** is active.
2. Click the exact center of the historic street intersection.
3. In **Enter Map Coordinates**:
   - type target X/East;
   - type target Y/North;
   - confirm the CRS matches the project CRS;
   - uncheck **Automatically hide georeferencer window**;
   - click **OK**.
4. Confirm a new enabled row appears in the GCP table.
5. Confirm the displayed source point is near the intended intersection.
6. Copy the source and target values into the session ledger.

Narration template:

```text
Point 1 is <intersection>. I am accepting it because <survival evidence>. It anchors <sheet region>.
Point 2 is <intersection>. Its distance from point 1 establishes <baseline/rotation>.
Point 3 is <intersection>. It is far from that baseline and closes the control triangle.
```

---

## 7. Choosing the transformation correctly

Transformation choice is the most important technical judgment after street identification.

### 7.1 Helmert: rigid-shape diagnostic

Helmert provides:

- translation;
- rotation;
- one uniform scale.

It does not allow separate horizontal and vertical scales or shear.

Use Helmert when:

- the scan has correct pixel aspect ratio;
- the original map is metrically consistent;
- three-point residuals are small;
- preserving shape with one scale is more important than exact three-point fit.

Important:

- Two points always fit Helmert exactly. Zero residual with only two points proves nothing.
- The third point is the real diagnostic.

### 7.2 Polynomial 1: the classic three-point affine fit

Polynomial 1 is a global affine transformation. It can provide:

- translation;
- rotation;
- separate X/Y scale correction;
- one global shear component.

It keeps every straight line straight. It does not create local bends or rubber-sheet ripples.

Use Polynomial 1 when:

- exactly three strong non-collinear controls are available;
- the accepted triangle cannot be fit with a uniform scale;
- the scan or paper has a global aspect-ratio difference;
- the user wants the classic three-point triangulation method.

With exactly three enabled GCPs, Polynomial 1 will normally report zero residual because those three points mathematically define the affine plane. This is expected and is not independent proof of accuracy. Visual overlay inspection remains mandatory.

### 7.3 What happened in the successful 1911 run

The three street choices were accepted, but Helmert produced large residuals:

| Point | Helmert residual, pixels |
|---:|---:|
| 0 | 119.212447 |
| 1 | 198.764394 |
| 2 | 159.046146 |

The two triangle legs implied materially different image scales:

- Rawson–Fulton baseline: approximately `0.06362 m/source-pixel`;
- South Pryor–Pulliam baseline: approximately `0.05628 m/source-pixel`.

That roughly 13% difference could not be represented by a single uniform Helmert scale. After verifying that the source clicks and modern coordinates were not mistaken, the correct recovery was:

```text
Transformation type = Polynomial 1
Enabled GCP count = exactly 3
```

This implemented the user's intended three-corner triangulation without adding a fourth control or invoking a locally deforming transform.

### 7.4 Avoid these transforms by default

- **Polynomial 2 or 3:** requires more points and can introduce curvature or overfitting.
- **Thin Plate Spline:** true rubber-sheeting; inappropriate for a three-point street-grid fit.
- **Projective:** reserved for clear photographic perspective distortion.
- **Linear/world-file-only:** useful in specialized cases, but not the default for producing a resampled GeoTIFF in this workflow.

### 7.5 Residual decision rules

Use these as practical prompts, not absolute survey standards:

- `0–3 px`: excellent for a redundant rigid fit.
- `3–10 px`: often acceptable for a scanned historical sheet; inspect visually.
- `10–25 px`: investigate click precision, street identity, scan distortion, and CRS.
- `>25 px`: do not generate final output until the cause is understood.
- `>100 px`: almost certainly a wrong control, wrong target coordinate, wrong CRS, or a transformation model that cannot represent the scan's global distortion.

For exactly three points under Polynomial 1, zero residual is mathematically guaranteed; rely on the visual QA checklist instead.

---

## 8. Required QGIS transformation settings

Open **Transformation Settings** and verify every field.

### Recommended settings

```text
Transformation type: Polynomial 1
Target CRS: Project CRS, normally EPSG:3857 for the current master project
Output file: a new *_georeferenced.tif
Resampling method: Cubic (4x4 Kernel)
Create world file only: unchecked
Destination alpha band: required; empty warp areas must be transparent
Global Opacity after loading: 100%
Layer Rendering Brightness: +50
Layer Rendering Gamma: 1.2
Layer Rendering Contrast: +20
Set target resolution: unchecked unless the user specifies a resolution
Save GCP points: checked
Load in project when done: checked
```

If the QGIS Georeferencer interface cannot reliably create a destination alpha band, generate the final raster with GDAL from the saved GCP/VRT data:

```text
gdalwarp -order 1 -t_srs EPSG:3857 -r cubic -dstalpha \
  -co COMPRESS=DEFLATE -co PREDICTOR=2 -co ZLEVEL=9 \
  <input-with-GCPs.vrt> <output_georeferenced.tif>
```

Do not substitute `-srcnodata 0` or a black-color transparency rule for `-dstalpha`; those approaches can erase authentic black cartographic content.

### Raster creation options

Use high, lossless compression:

| Name | Value |
|---|---|
| COMPRESS | DEFLATE |
| PREDICTOR | 2 |
| ZLEVEL | 9 |

Why:

- DEFLATE is lossless.
- Predictor 2 is appropriate for continuous byte/integer raster values and improves compression.
- ZLEVEL 9 minimizes storage without JPEG artifacts in labels and linework.

Before clicking **OK**, re-read the complete output path. The filename must not be the source filename and must not collide with an existing output.

---

## 9. Saving GCPs and running the transformation

### 9.1 Save GCPs explicitly before raster generation

1. Click **Save GCP Points as…**.
2. Confirm the directory is the intended Sanborn source directory.
3. Enter:

   ```text
   <original stem>_3points.points
   ```

4. Click **Save**.
5. If the file already exists, do not overwrite it silently. Use `_v2.points` or a timestamp unless the user explicitly authorizes replacement.
6. Verify the saved file exists and is nonempty.

The save dialog may return focus to the main QGIS window. That does not mean the Georeferencer was closed or reset.

### 9.2 Restore a hidden or minimized Georeferencer safely

Do **not** search for every QAction whose text contains “Georeferencer” and trigger them. That previously caused multiple actions, duplicate dialogs, save prompts, and a reset risk.

Use the QGIS Python console:

```python
from qgis.PyQt.QtWidgets import QApplication
[w.showNormal() or w.raise_() or w.activateWindow()
 for w in QApplication.topLevelWidgets()
 if w.windowTitle().startswith('Georeferencer -')]
```

Press Return again if QGIS has not executed the line yet.

### 9.3 Start georeferencing

1. Confirm all three GCP rows are enabled.
2. Confirm the transformation type in the Georeferencer status line.
3. Confirm the output path one last time.
4. Click **Start Georeferencing**.
5. If a GCP save dialog appears, click **Save**, not Cancel.
6. Monitor the progress dialog.
7. Do not click **Abort** unless QGIS is genuinely stuck and the user agrees.
8. Wait for the progress dialog to close.

High-compression processing can take tens of seconds. Progress may advance unevenly.

---

## 10. Visual verification against OpenStreetMap and the 1921 Kauffman map

Do not declare success merely because QGIS generated a file.

### 10.1 Confirm the output layer loaded

The QGIS Layers panel should contain a checked raster layer named from the output, for example:

```text
! 1911 Sanborn 486 Shmuel Yankel B_georeferenced
```

If it is missing:

- verify the GeoTIFF exists;
- add that GeoTIFF manually as a raster layer;
- do not save the master project.

Then verify the layer-tree structure, not only visibility:

- exactly one copy of the new raster is present;
- it is at the root level, not nested inside a folder such as `River REM`;
- it is beside the other 1911 sheets in chronological order;
- its Global Opacity is 100%.

### 10.2 Inspect all three anchors

At each control point, compare the historic and modern centerlines:

- Rawson × South Pryor;
- Fulton × South Pryor;
- Fulton × Pulliam;
- or the three intersections selected for a new sheet.

Check that:

- road centerlines cross at the same point;
- historic street directions match the modern grid;
- no point landed on a curb, building corner, label, or highway ramp;
- the map is not mirrored;
- rotation is correct.

### 10.3 Inspect non-control streets

Because exactly three affine points fit exactly, the strongest validation is away from the controls.

Inspect:

- surviving street segments between the controls;
- a surviving intersection not used as a GCP, if one is trustworthy;
- parallelism of the historic and modern street grids;
- the sheet edge near the interstate, understanding that roads may have been physically removed.

Do not mistake genuine century-scale urban change for georeferencing error.

### 10.4 Verify the complete street grid against 1921 Kauffman

Turn on **1921 Atlanta Kauffman Map_modified** and blink the Sanborn layer against it.

Check that:

- vanished streets occupy the same historical corridors;
- east–west and north–south street families retain their correct orientation;
- the sheet is not sheared into a parallelogram or rubber-banded to satisfy a doubtful point;
- surviving OSM anchors remain primary even if Kauffman differs slightly because of its own hand-georeferencing error.

If OSM and Kauffman disagree materially, do not average them blindly. Identify which control is genuinely surviving ground truth and which placement is historical inference.

### 10.5 Blink the layer; never adjust Global Opacity

For visual QA, toggle the new raster layer off and on so OSM is visible beneath it. Keep the raster's Global Opacity at 100% throughout the run.

Layer visibility changes are in-memory display changes. Do not save them into the protected master project.

### 10.6 Interpretation rule

A good result means:

- the chosen surviving intersections align;
- other surviving street centerlines generally agree;
- destroyed or rerouted areas are allowed to differ;
- historic buildings need not match modern buildings;
- historical house numbers are irrelevant.

---

## 11. Filesystem validation and protection audit

After QGIS finishes, verify with read-only checks:

1. Output GeoTIFF exists and has nonzero size.
2. GCP file exists and has nonzero size.
3. Source raster still exists and has the original size or hash.
4. Protected `.qgz` modification time is unchanged.
5. Output modification time matches the current run.

Example macOS command:

```bash
stat -f '%N | %z bytes | modified %Sm' \
  '/path/to/output_georeferenced.tif' \
  '/path/to/control_points.points' \
  '/path/to/JLS Master Map File.qgz'
```

If GDAL tools are available, also inspect:

```bash
gdalinfo '/path/to/output_georeferenced.tif'
```

Confirm:

- raster opens successfully;
- CRS is correct;
- raster dimensions are plausible;
- corner coordinates fall in the expected Atlanta area;
- compression metadata is present.

Do not use a command that rewrites, optimizes, or updates the TIFF during validation.

---

## 12. Recovery playbook

### Problem: the Georeferencer raster pane is blank

Recovery:

1. Confirm the source path still appears in the status line.
2. Click **Zoom to Layer**.
3. Toggle the Georeferencer window between normal and zoomed size to force repaint.
4. If still blank and the GCP table is empty, reload the exact source TIFF through **Open Raster**.
5. If GCPs already exist, save them before any reload attempt.

### Problem: the file picker will not select a file by accessibility click

Recovery:

1. Focus the file list.
2. Use Home and arrow keys to move to the exact filename.
3. Verify the selected row text.
4. Click **Open**.

Never open a nearby TIFF based on truncated visible text alone.

### Problem: Georeferencer disappeared after saving GCPs

Recovery:

- Use the safe `QApplication.topLevelWidgets()` restoration command in Section 9.2.
- Do not trigger all QGIS actions containing “Georeferencer.”

### Problem: GCP coordinates were entered but the point is missing

Check:

- Was **OK** clicked in Enter Map Coordinates?
- Did the row appear in the GCP table?
- Was **Cancel** clicked accidentally?
- Did a reset prompt clear the session?

Recovery:

- If the table still contains the points, save them immediately.
- If a `.points` file exists, load it.
- Otherwise reconstruct from the session ledger.

### Problem: GCP Save dialog is open

Correct action:

- set a unique `.points` filename;
- click **Save**.

Incorrect action:

- clicking Cancel and assuming QGIS saved automatically.

### Problem: “Reset georeferencer and clear all GCP points?” appears

Correct default:

- click **Cancel** to preserve the points.

This is the one place where Cancel is normally protective. Read the dialog title and message before acting.

### Problem: “From Map Canvas” pans or changes tools unexpectedly

Recovery:

1. Return to Georeferencer.
2. Ensure **Add GCP Point** is active.
3. Compute the modern target with the Python console.
4. Enter X and Y manually in the coordinate dialog.

### Problem: two points show zero residual

Explanation:

- This is expected and not a validation.
- Add the third independent point before judging the transform.

### Problem: the third Helmert point creates large residuals

Recovery sequence:

1. Do not generate output yet.
2. Recheck historic click locations.
3. Recheck target X/Y order.
4. Recheck target CRS.
5. Confirm the modern intersection identity.
6. Confirm the three points are non-collinear.
7. Compare scale implied by each triangle leg.
8. If the controls are correct and the mismatch is global, switch to Polynomial 1 with exactly three points.
9. Do not escalate to Polynomial 2/3 or TPS merely to erase residuals.

### Problem: output filename already exists

Recovery:

- choose `_v2`, `_v3`, or a timestamped output;
- never overwrite by default;
- never point output at the source raster.

### Problem: output generated but does not appear in the stack

Recovery:

1. Verify the file exists.
2. Add it as a raster layer.
3. Confirm it is checked and above OSM.
4. Do not save the master project.

### Problem: several duplicate layers appeared, or the raster landed inside a hidden group

Cause:

- QGIS inserted the raster relative to the selected group; and/or
- the Data Source Manager stayed open with no visible Add acknowledgement, so Add was clicked more than once.

Recovery:

1. Identify every layer entry with the exact new raster name.
2. Keep one entry and remove the accidental duplicate **layer entries only**. Do not delete the GeoTIFF.
3. Drag the survivor out to the root level beside the other 1911 sheets.
4. Verify its indentation and chronological position before continuing.
5. Leave the protected project unsaved.

### Problem: QGIS project title has an asterisk

Explanation:

- The in-memory project is dirty because of session changes.
- This is expected.
- Do not click Save.
- Do not close QGIS.
- Verify the disk `.qgz` timestamp remains unchanged.

### Problem: the user moves or zooms QGIS during automation

Recovery:

- stop sending actions;
- obtain a fresh application state;
- respect the user's new view;
- recompute all screen-to-canvas relationships;
- never reuse prior screenshot coordinates.

---

## 13. Efficient street-discovery reasoning

For a new Sanborn sheet, use this ordered process.

### Step 1: Transcribe before searching

Read all clear names from the historic sheet and place them into two parallel street families. This normally reveals the page rotation immediately.

### Step 2: Start with a user-suggested survivor

If the user names a likely unchanged intersection—such as Fulton and Pulliam—treat it as the first high-value candidate, but still verify it visually.

### Step 3: Expand along the same historic street

Find another surviving intersection far away along one of the two streets. This establishes a long baseline and reduces rotation sensitivity.

### Step 4: Close a large triangle

Choose a third intersection far from the first baseline. A large triangle is more stable than three central points.

### Step 5: Reject deceptive evidence

Common traps:

- the same street name applied to a shifted alignment;
- a historic street surviving only as a driveway or ramp;
- a modern service road occupying a demolished street's approximate path;
- confusing Central Avenue with its renamed modern counterpart;
- assuming a building corner proves a street centerline;
- using 1911 house numbers as modern locators.

### Step 6: Use web research only when necessary

If identity remains uncertain, prefer:

1. official City of Atlanta GIS or transportation records;
2. authoritative street-renaming ordinances or historical directories;
3. the user's already-georeferenced historic map layers;
4. OpenStreetMap for current geometry;
5. reputable historical map archives.

Record the conclusion once; do not repeat the same search for each point.

---

## 14. Completion criteria

The job is complete only when every item below is true.

### Output integrity

- [ ] A new georeferenced TIFF exists.
- [ ] The output filename differs from the source filename.
- [ ] The output TIFF has nonzero size and opens in QGIS.
- [ ] The output CRS is correct.
- [ ] Lossless compression settings were used unless the user requested otherwise.
- [ ] The output has a fourth band whose color interpretation is Alpha.
- [ ] Empty areas outside the warped sheet footprint are transparent, with no added black wedges or borders.
- [ ] Genuine black ink within the sheet remains opaque and visible.
- [ ] QGIS Global Opacity is exactly 100% (`renderer().opacity() == 1.0`).
- [ ] QGIS Layer Rendering uses Brightness `+50`, Gamma `1.2`, and Contrast `+20`.

### GCP integrity

- [ ] Exactly three intended GCP rows are enabled for the default workflow.
- [ ] The `.points` file exists and is nonempty.
- [ ] The street names and target coordinates are recorded.
- [ ] The GCP Save dialog was completed with **Save**.

### Alignment quality

- [ ] All three control intersections align visually.
- [ ] Surviving non-control street segments are plausible.
- [ ] The complete street grid was blink-checked against **1921 Atlanta Kauffman Map_modified**.
- [ ] Historically east–west streets remain east–west and historically north–south streets remain north–south.
- [ ] No affine shear or rubber-banding was accepted merely because three controls fit exactly.
- [ ] No mistaken mirroring or 90-degree orientation error remains.
- [ ] Interstate-altered areas are interpreted as historical change, not automatically as bad control.

### Project safety

- [ ] Source raster was not overwritten.
- [ ] Protected `.qgz` modification time is unchanged.
- [ ] QGIS project Save was not used.
- [ ] QGIS remains open if the in-memory project is dirty.
- [ ] Exactly one new raster layer is present at the root level in the chronological 1911 cluster.

---

## 15. Required final handoff to the user

The final report should be concise and include:

1. output TIFF filename and size;
2. GCP filename;
3. three intersections used;
4. transformation type and target CRS;
5. visual verification result;
6. explicit statement that the source was not overwritten;
7. explicit statement that the protected `.qgz` was not saved or modified on disk;
8. confirmation that exactly one new layer is at the root level beside the other 1911 sheets;
9. note that the new layer is loaded only in the current QGIS session unless project saving is later authorized.

Suggested handoff:

```text
Completed. I created <output>, saved the three controls in <points>, and loaded exactly one new raster over OSM at the root level beside the other 1911 sheets. The controls were <A>, <B>, and <C>, using Polynomial 1 in <CRS>. The anchor intersections and surviving street grid align visually. The original TIFF was not overwritten, and the protected master .qgz was not saved or modified on disk. The layer is present in the current QGIS session only.
```

---

## 16. Copy-paste launch prompt for a future Sol run

Use this prompt with the relevant source raster attached or named:

```text
Act as the AI AUTOMATED SANBORN BOT. Read and follow the master instruction file in:

/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/AI AUTOMATED SANBORN BOT/AI AUTOMATED SANBORN BOT - MASTER INSTRUCTIONS.md

Georeference the supplied Sanborn TIFF in the currently open QGIS project against the active OpenStreetMap layer. Treat the source TIFF and JLS Master Map File.qgz as protected: never overwrite the TIFF and never save or modify the .qgz on disk. Use exactly three strong, distant, non-collinear surviving street-centerline intersections by default. Narrate the street-identification decisions, not every UI click. Save a new *_georeferenced.tif and a *_3points.points file, verify the overlay visually, verify the protected project timestamp is unchanged, and leave QGIS open.

Before finalizing, verify the entire 1911 street grid against **1921 Atlanta Kauffman Map_modified**. OSM remains ground truth wherever an intersection survives; Kauffman is the canonical historical cross-check for vanished streets, latitude/longitude plausibility, and east–west/north–south orientation. Reject rubber-banding or affine shear caused by a doubtful control.

Every output must include a real destination alpha band so rotation/warp-created empty areas are transparent rather than black. Never treat RGB black as nodata. In QGIS, keep Global Opacity at 100% at all times, apply Layer Rendering values Brightness +50, Gamma 1.2, and Contrast +20, and blink layer visibility for comparisons.

The sheet may be anywhere in Atlanta. Begin every new sheet by locating its street network against **1921 Atlanta Kauffman Map_modified**; do not assume Summerhill. If this is one of the already-completed 1911 sheets 485, 486, 493, or 494, use the Section 18 same-sheet fast path and load its final `_cardinal_v2.points` file instead of rediscovering the intersections.
```

---

## 17. Historical record of the initial successful reference run

This section preserves the first workable 486 result for historical context. It is not the current exact-reconstruction recipe. Section 18 and the `_cardinal_v2.points` files supersede this initial record for reproducing sheets 485, 486, 493, and 494.

Reference run results:

```text
Source: ! 1911 Sanborn 486 Shmuel Yankel B.tif
Source dimensions: 6473 x 7656
Output: ! 1911 Sanborn 486 Shmuel Yankel B_georeferenced.tif
Output size: 80,313,374 bytes
GCP file: ! 1911 Sanborn 486 Shmuel Yankel B_3points.points
GCP file size: 1,700 bytes
Transformation: Polynomial 1
CRS: EPSG:3857
Resampling: Cubic (4x4 Kernel)
Compression: DEFLATE / PREDICTOR 2 / ZLEVEL 9
Points: Rawson–South Pryor; Fulton–South Pryor; Fulton–Pulliam
Protected project disk modification time after run: Jul 12 2026 17:12:08
Protected project was not saved during the run.
```

The central lesson from the run is simple:

> Three excellent historical controls are more valuable than many questionable ones. Verify their identities, save them explicitly, use the least flexible global transform that can accurately represent the scan, and protect the master project at every step.

---

## 18. Exact four-sheet reconstruction record and the reasoning behind the correction

This section is the definitive reconstruction appendix for the four neighboring 1911 sheets completed on 2026-07-14. It records the final numerical controls, the failure that exposed the need for the correction, and the general reasoning to carry into other Atlanta neighborhoods.

### 18.1 Authority and file precedence

For sheets 485, 486, 493, and 494, use the files ending in `_cardinal_v2.points`. They supersede the earlier `_3points.points` files for exact reconstruction.

Never overwrite an earlier raster or point file. The accepted corrected outputs are:

| Sheet | Source | Final control file | Accepted corrected output | Size in bytes |
|---:|---|---|---|---:|
| 485 | `1911 Sanborn 485 Shmuel Yankel A.tif` | `1911 Sanborn 485 Shmuel Yankel A_cardinal_v2.points` | `1911 Sanborn 485 Shmuel Yankel A_georeferenced_v2.tif` | 92,732,057 |
| 486 | `! 1911 Sanborn 486 Shmuel Yankel B.tif` | `! 1911 Sanborn 486 Shmuel Yankel B_cardinal_v2.points` | `! 1911 Sanborn 486 Shmuel Yankel B_georeferenced_v2.tif` | 95,369,651 |
| 493 | `1911 Sanborn 493 Shmuel Yankel D.tif` | `1911 Sanborn 493 Shmuel Yankel D_cardinal_v2.points` | `1911 Sanborn 493 Shmuel Yankel D_georeferenced_v2.tif` | 98,300,681 |
| 494 | `1911 Sanborn 494 Shmuel Yankel C.tif` | `1911 Sanborn 494 Shmuel Yankel C_cardinal_v2.points` | `1911 Sanborn 494 Shmuel Yankel C_georeferenced_v2.tif` | 101,369,439 |

The source scans, final point files, and accepted corrected rasters are stored in:

```text
/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/1911 Sanborn Map/
```

The private GitHub repository stores the small point files and this manual, but deliberately excludes the large TIFFs.

### 18.2 The diagnostic failure that changed the method

The first transformation of sheet 494 C looked close at its three chosen controls, but the sheet was visibly sheared. Fulton Street did not run east–west, and Clarke Street—although it no longer survives—also failed the east–west orientation shown by the 1921 Kauffman map.

The failure had four parts:

1. Exactly three points mathematically define a first-order affine transformation.
2. Because the transform passes through all three points, QGIS can report zero residual even when one target is historically doubtful or the resulting grid is badly sheared.
3. A zero-residual number therefore measures internal mathematical fit, not geographic truth.
4. Adding a fourth point or a more flexible rubber-sheet transform would have hidden the diagnosis by spreading the error through the sheet.

The correction was to return to the street-grid evidence:

- retain modern OSM coordinates wherever the intersection unquestionably survives;
- use Kauffman to verify the vanished streets, whole-sheet position, and cardinal orientation;
- make two controls on a verified east–west street share one target `mapY` value;
- make two controls on a verified north–south street share one target `mapX` value;
- use the shared corner as the third vertex of the control triangle;
- run a first-order affine transformation from that cardinal L-shaped control set;
- preserve the earlier output and write the correction as `_v2`;
- verify the result away from all three controls against both OSM and Kauffman.

This is not permission to straighten every street. Only share `mapX` or `mapY` when OSM, Kauffman, and the historical sheet establish that the street is genuinely north–south or east–west. Curving, angled, and irregular streets must retain their real geometry.

### 18.3 Exact final controls

All four final control files use `EPSG:3857`. Source Y coordinates are negative because that is how QGIS stores raster pixel coordinates in its `.points` format. Copy every digit; do not round the numerical record.

#### Sheet 485 — Shmuel Yankel A

The control triangle uses East Fair Street, Washington Street, South Pryor Street, and Rawson Street. East Fair supplies the shared east–west target northing; South Pryor supplies the shared north–south target easting.

| Point | Historic intersection | mapX | mapY | sourceX | sourceY |
|---:|---|---:|---:|---:|---:|
| 1 | East Fair × Washington | -9394353.685378434 | 3994871.812093691 | 474 | -350 |
| 2 | East Fair × South Pryor | -9394745.763756957 | 3994871.812093691 | 489 | -7153 |
| 3 | Rawson × South Pryor | -9394745.763756957 | 3994577.224317947 | 6000 | -7148 |

Authoritative record:

```text
#CRS: EPSG:3857
mapX,mapY,sourceX,sourceY,enable,dX,dY,residual
-9394353.685378434,3994871.812093691,474,-350,1,0,0,0
-9394745.763756957,3994871.812093691,489,-7153,1,0,0,0
-9394745.763756957,3994577.224317947,6000,-7148,1,0,0,0
```

#### Sheet 486 — Shmuel Yankel B

The control triangle uses Rawson Street, South Pryor Street, Fulton Street, and Pulliam Street. South Pryor supplies the shared north–south target easting; Fulton supplies the shared east–west target northing.

| Point | Historic intersection | mapX | mapY | sourceX | sourceY |
|---:|---|---:|---:|---:|---:|
| 1 | Rawson × South Pryor | -9394749.788794577 | 3994577.224317947 | 478.7470741222369 | -7168.166449934981 |
| 2 | Fulton × South Pryor | -9394749.788794577 | 3994228.236111111 | 5964.385565669702 | -7168.166449934981 |
| 3 | Fulton × Pulliam | -9394520.436111111 | 3994228.236111111 | 5964.385565669702 | -3056.426527958388 |

Authoritative record:

```text
#CRS: EPSG:3857
mapX,mapY,sourceX,sourceY,enable,dX,dY,residual
-9394749.788794577,3994577.224317947,478.7470741222369,-7168.166449934981,1,0,0,0
-9394749.788794577,3994228.236111111,5964.385565669702,-7168.166449934981,1,0,0,0
-9394520.436111111,3994228.236111111,5964.385565669702,-3056.426527958388,1,0,0,0
```

#### Sheet 493 — Shmuel Yankel D

The control triangle uses East Fair Street, Washington Street, Capitol Avenue, and Rawson Street. East Fair supplies the shared east–west target northing; Washington supplies the shared north–south target easting.

| Point | Historic intersection | mapX | mapY | sourceX | sourceY |
|---:|---|---:|---:|---:|---:|
| 1 | East Fair × Washington | -9394353.685378434 | 3994871.812093691 | 441 | -399 |
| 2 | East Fair × Capitol | -9393998.988084918 | 3994871.812093691 | 6054 | -351 |
| 3 | Rawson × Washington | -9394353.685378434 | 3994577.041174500 | 413 | -5872 |

Authoritative record:

```text
#CRS: EPSG:3857
mapX,mapY,sourceX,sourceY,enable,dX,dY,residual
-9394353.685378434,3994871.812093691,441,-399,1,0,0,0
-9393998.988084918,3994871.812093691,6054,-351,1,0,0,0
-9394353.685378434,3994577.041174500,413,-5872,1,0,0,0
```

#### Sheet 494 — Shmuel Yankel C

The control triangle uses Washington Street, Clarke Street, Fulton Street, and Capitol Avenue. Washington supplies the shared north–south target easting; Fulton supplies the shared east–west target northing. Clarke is an essential Kauffman verification street even though it no longer survives in the modern grid.

| Point | Historic intersection | mapX | mapY | sourceX | sourceY |
|---:|---|---:|---:|---:|---:|
| 1 | Clarke × Washington | -9394367.209008012 | 3994370.7134899595 | 394 | -1742 |
| 2 | Fulton × Washington | -9394367.209008012 | 3994196.444091066 | 394 | -4475 |
| 3 | Fulton × Capitol | -9394005.789705805 | 3994196.444091066 | 6062 | -4475 |

Authoritative record:

```text
#CRS: EPSG:3857
mapX,mapY,sourceX,sourceY,enable,dX,dY,residual
-9394367.209008012,3994370.7134899595,394,-1742,1,0,0,0
-9394367.209008012,3994196.444091066,394,-4475,1,0,0,0
-9394005.789705805,3994196.444091066,6062,-4475,1,0,0,0
```

### 18.4 Exact same-sheet reconstruction procedure

For any of these four sheets:

1. Record the protected QGIS project modification time and confirm that the source TIFF will not be overwritten.
2. Open the original source TIFF in QGIS Georeferencer.
3. Load the matching `_cardinal_v2.points` file from the 1911 Sanborn Map folder.
4. Confirm that the CRS declaration is `EPSG:3857` and exactly three points are enabled.
5. Use `Polynomial 1`, cubic resampling, and lossless DEFLATE compression.
6. Choose a new output filename. If the accepted `_georeferenced_v2.tif` already exists, create `_georeferenced_v3.tif` or a timestamped derivative; never overwrite silently.
7. Ensure the output receives a true destination alpha band. When using GDAL, use `-dstalpha`; do not convert RGB black ink into transparency.
8. Run the transformation and load the output into the current QGIS session.
9. Set Global Opacity to 100%.
10. Blink the result against surviving OSM intersections.
11. Blink the complete grid against **1921 Atlanta Kauffman Map_modified**, including streets that no longer survive.
12. Confirm that east–west streets remain east–west, north–south streets remain north–south, and no parallelogram-like shear has returned.
13. Compare neighboring completed sheets along their shared edges. A corrected sheet must not introduce an unexplained break in a street that continues onto its neighbor.
14. Keep all 1911 layers together in the QGIS layer stack. Within the 1911 cluster, retain the project's established order; if none exists, use sheet-number order 485, 486, 493, 494 for predictable organization.
15. Verify the source and protected project remain unchanged on disk, then leave QGIS open.

### 18.5 Citywide reasoning template for every future sheet

The exact coordinates above apply only to these four sheets. The reasoning applies throughout Atlanta.

For a new sheet in any neighborhood, build a compact evidence ledger:

| Candidate | Historic identity | Kauffman match | Survives on OSM | Cardinal or irregular geometry | Confidence | Use? |
|---|---|---|---|---|---|---|
| 1 |  |  |  |  |  |  |
| 2 |  |  |  |  |  |  |
| 3 |  |  |  |  |  |  |

Then use this sequence:

1. **Locate with Kauffman.** Match multiple street names and the network shape. A single repeated street name is insufficient.
2. **Orient with Kauffman.** Determine which historical streets are east–west, north–south, diagonal, curved, renamed, or vanished.
3. **Promote surviving OSM controls.** Use OSM only after confirming that the historic intersection truly survives and has not been shifted by road widening, ramps, superblocks, or redevelopment.
4. **Spread the triangle.** Choose distant points that span the sheet and avoid near-collinearity.
5. **Diagnose with the least flexible model.** Test Helmert for shape preservation; use a first-order affine model only when the scan requires independent X/Y scale or skew correction.
6. **Treat zero residual correctly.** With exactly three affine controls, zero residual is automatic and provides no independent evidence of accuracy.
7. **Use cardinal constraints only when proved.** Shared target eastings or northings are powerful when the streets truly define perpendicular axes; they are destructive when imposed on an angled or curved grid.
8. **Verify beyond the controls.** Inspect non-control streets against OSM and the full vanished-and-surviving grid against Kauffman.
9. **Escalate uncertainty.** If three strong surviving controls do not exist, pause and tell Joel which points are OSM-grounded, which are Kauffman-assisted, and what uncertainty remains.
10. **Preserve every decision.** Save the point file, record the three intersection identities, transformation, CRS, output name, and one-sentence rationale for any cardinal constraint.

The transferable insight is that georeferencing is not merely point matching. It is an evidence hierarchy: modern surviving geometry fixes precise ground position, Kauffman restores the lost historical network, the Sanborn sheet supplies local detail, and the transformation must respect all three without disguising uncertainty through rubber-sheeting.

---

## 19. Finding and downloading a 1911 Atlanta Sanborn sheet from the Library of Congress

This is the permanent acquisition workflow. It begins with Joel's orthorectified QGIS index, routes the requested sheet to one of four Library of Congress volumes, downloads the compressed full-resolution JPEG2000 file, and preserves enough provenance to repeat the result without searching again.

### 19.1 Permanent LOC bookmark

Use this filtered collection bookmark for 1911 Atlanta Sanborn maps:

```text
https://www.loc.gov/collections/sanborn-maps/?dates=1911&fa=location_state:georgia%7Clocation_city:atlanta
```

The LOC states that its online Sanborn Maps Collection is in the public domain and free to use and reuse. Credit the Library of Congress Geography and Map Division, Sanborn Maps Collection when publishing the material.

### 19.2 The QGIS index is the routing key

The QGIS layer named **1911 Sanborn Index Orthorectified** is the key to the individual LOC sheets.

- The printed number on the orthorectified index corresponds to the printed Sanborn sheet number.
- Use the index to locate the requested number geographically before opening LOC.
- Record the approximate QGIS coordinate under the requested number when useful for recovery.
- The index's four colors route the sheet to exactly one LOC volume.

| Index color | City section | LOC volume | Item page |
|---|---|---:|---|
| Red | Northwest | 1 | `https://www.loc.gov/item/sanborn01378_006/` |
| Green | Northeast | 2 | `https://www.loc.gov/item/sanborn01378_007/` |
| Blue | Southwest | 3 | `https://www.loc.gov/item/sanborn01378_008/` |
| Yellow | Southeast | 4 | `https://www.loc.gov/item/sanborn01378_009/` |

Each 1911 sheet occurs in only one volume. Do not search all four volumes after the index color has established the correct one.

### 19.3 Compass-first orientation is mandatory

The compass rose on **1911 Sanborn Index Orthorectified** is true north. Individual Sanborn sheets may be scanned or drawn with north pointing in another direction.

Before reading street geometry or choosing controls:

1. Find the compass rose on the downloaded sheet.
2. Determine the sheet's north direction.
3. Rotate the working view so north faces up.
4. Record the rotation used.

This is a display or temporary-working-copy operation. Never destructively rotate or overwrite the downloaded source file. A sheet whose north arrow already points up requires `0°` pre-rotation.

### 19.4 Match the printed tile number to the LOC sequence image

The printed Sanborn tile number is not the LOC sequence-image number. Front matter, indexes, skeleton maps, and supplementary sheets create an offset. Never assume Tile 89 is `sp=89`, or Tile 474 is `sp=474`.

Two reliable methods exist:

1. **Visual method:** Open the correct volume, browse its image sequence, and confirm the large printed sheet number on the page itself.
2. **Official metadata method:** Request the volume item's LOC JSON resource list with `?fo=json&at=resources`. Find the JPEG2000 URL whose filename ends in the zero-padded printed number, such as `-0474.jp2`. Its one-based position in the resource list is the LOC `sp=` sequence number.

Always verify the printed sheet number on a preview before accepting the match.

### 19.5 Download JPEG2000, not a preview

On the correct LOC image page:

1. Open the **Download** menu.
2. Choose **JPEG2000**.
3. Do not choose the small JPEG preview, GIF, or full TIFF by default.
4. Download one requested tile at a time; avoid rapid repetitive requests.
5. Rename the finished file:

   ```text
   Sanborn 1911 -- Tile <number>.jp2
   ```

6. Save it in:

   ```text
   /Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/AI AUTOMATED SANBORN BOT/1911 SANBORN DOWNLOADS/
   ```

JPEG2000 is the required default because it is compressed while retaining the LOC's full pixel dimensions.

Do not overwrite a file already present under the required name. Compare the existing file with LOC metadata and ask Joel before replacing it.

### 19.6 LOC human-verification challenges

If LOC presents a “prove you are human” challenge, pause and hand control to Joel. Do not solve, script, imitate, or attempt to evade the challenge. After Joel confirms completion, inspect the current page again and resume the download.

The official LOC JSON metadata and `tile.loc.gov` storage links are appropriate for ordinary single-sheet research downloads. Use them conservatively and never attempt to defeat a rate limit or access restriction.

### 19.7 Required download ledger and verification

Record this compact ledger for every acquired sheet:

```text
TILE_NUMBER=
INDEX_COORDINATE=
INDEX_COLOR=
CITY_SECTION=
LOC_VOLUME=
LOC_ITEM_URL=
LOC_SEQUENCE_SP=
LOC_RESOURCE_PAGE=
LOC_JPEG2000_URL=
DOWNLOADED_FILE=
PIXEL_DIMENSIONS=
FILE_SIZE_BYTES=
SHA256=
COMPASS_NORTH_DIRECTION=
PRE_ROTATION=
PRINTED_TILE_NUMBER_VISUALLY_CONFIRMED=
```

After download:

- confirm the file is JPEG2000/JP2;
- confirm its dimensions and byte size match LOC metadata;
- create a checksum;
- inspect one short preview to confirm the printed sheet number and that the image is not corrupt;
- do not spend time conducting a full cartographic analysis until georeferencing begins.

### 19.8 Worked example — Tile 89

```text
TILE_NUMBER=89
INDEX_COORDINATE=-9396910.9,4000354.3
INDEX_COLOR=red
CITY_SECTION=northwest
LOC_VOLUME=1
LOC_ITEM_URL=https://www.loc.gov/item/sanborn01378_006/
LOC_SEQUENCE_SP=102
LOC_RESOURCE_PAGE=https://www.loc.gov/resource/g3924am.g3924am_g01378191101/?sp=102&st=image&r=-0.542,-0.216,2.085,1.327,0
DOWNLOADED_FILE=Sanborn 1911 -- Tile 89.jp2
```

Tile 89 demonstrates why the sequence number must be discovered rather than calculated: printed Tile 89 is LOC image 102.

### 19.9 Approved test record — Tile 474

The first complete acquisition test was approved by Joel on 2026-07-14.

```text
TILE_NUMBER=474
INDEX_COLOR=yellow
CITY_SECTION=southeast
LOC_VOLUME=4
LOC_ITEM_URL=https://www.loc.gov/item/sanborn01378_009/
LOC_SEQUENCE_SP=37
LOC_RESOURCE_PAGE=https://www.loc.gov/resource/g3924am.g3924am_g01378191104/?sp=37&st=image
LOC_JPEG2000_URL=https://tile.loc.gov/storage-services/service/gmd/gmd392m/g3924m/g3924am/g3924am_g01378191104/01378_04_1911-0474.jp2
DOWNLOADED_FILE=/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/AI AUTOMATED SANBORN BOT/1911 SANBORN DOWNLOADS/Sanborn 1911 -- Tile 474.jp2
PIXEL_DIMENSIONS=6605 x 7795
FILE_SIZE_BYTES=12934452
SHA256=e6329018a46320bcc738c516ef49017f7ad6154c51e78f8a0f33760cd20ff7f9
COMPASS_NORTH_DIRECTION=up
PRE_ROTATION=0 degrees
PRINTED_TILE_NUMBER_VISUALLY_CONFIRMED=yes
```

The preview clearly shows the printed number `474`, Fulton Bag & Cotton Mills, Decatur Street, South Boulevard, Tennelle Street, Carroll Street, Wyman Street, Rinehart Street, Shelton Street, and Oakland Cemetery. This geographic content becomes the starting evidence for the georeferencing stage.

### 19.10 Approved georeferencing record — Tile 474

Tile 474 was completed and visually approved on 2026-07-14. It is a useful warning that three plausible modern names are not necessarily three surviving intersections.

```text
SOURCE_RASTER=/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/AI AUTOMATED SANBORN BOT/1911 SANBORN DOWNLOADS/Sanborn 1911 -- Tile 474.jp2
OUTPUT_RASTER=/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/AI AUTOMATED SANBORN BOT/1911 SANBORN DOWNLOADS/Sanborn 1911 -- Tile 474_georeferenced.tif
GCP_FILE=/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/AI AUTOMATED SANBORN BOT/1911 SANBORN DOWNLOADS/Sanborn 1911 -- Tile 474_3points_kauffman_assisted.points
TRANSFORMATION=Polynomial 1 / global affine
TARGET_CRS=EPSG:3857
RESAMPLING=cubic
OUTPUT_PIXEL_DIMENSIONS=7735 x 9239
OUTPUT_BANDS=Red, Green, Blue, Alpha
OUTPUT_COMPRESSION=DEFLATE, PREDICTOR 2
OUTPUT_SHA256=0e1907f8dca2a5c69a40fa538e346f510777c07e5bb6ba04126d5cb71bb0d3d3
CONFIDENCE=two surviving OSM anchors plus one Kauffman-assisted historical anchor
```

Exact enabled controls:

| ID | Intersection | Evidence | Source X | Source Y | Target X | Target Y |
|---:|---|---|---:|---:|---:|---:|
| 1 | Carroll Street × Tennelle Street | surviving OSM | 6452 | -3565 | -9391764.83930054 | 3995400.81652537 |
| 2 | Carroll Street × Shelton Street | surviving OSM | 6345 | -5592 | -9391811.2595282 | 3995198.25152542 |
| 3 | Boulevard × Tennelle Street | Kauffman-assisted; west Tennelle no longer survives to Boulevard | 2652 | -5056 | -9392093.75868612 | 3995262.59459438 |

Why these controls were accepted:

- Carroll–Tennelle and Carroll–Shelton survive and provide two precise modern anchors separated north–south.
- The third point had to close a wide triangle on the west side. Kauffman preserves the lost Boulevard–Tennelle relationship and keeps the historic grid cardinal.
- The result places the mill, railroad, Oakland Cemetery edge, Boulevard, Carroll, Tennelle, and Shelton in the expected historical configuration.
- A full-resolution automated rehearsal reproduced the accepted raster's four band pixel checksums exactly: `55125`, `14515`, `65108`, `51289`.

False friends and rejected choices:

- **Boulevard × Decatur:** not a clear usable intersection on this sheet.
- **Boulevard × Tennelle as a modern OSM control:** rejected because the historic west segment no longer survives; it is valid only as explicitly Kauffman-assisted.
- **Wyman × Shelton:** rejected because Wyman does not survive under the same geometry and identity.
- **Rinehardt/Reinhardt × Shelton:** rejected as a false name match. The modern tiny Reinhardt geometry is not the historic Rinehardt street shown here and would compress the sheet sideways.
- **Rigid Helmert diagnostic:** rejected because it rotated the sheet roughly ten degrees and contradicted the Kauffman/cardinal street grid.

The final layer was added exactly once at the root level after `1906 Race Massacre Pryor Street` and before the other 1911 Sanborn sheets. Three accidental duplicate layer entries inside the disabled `River REM` group were removed. The GeoTIFF itself was never deleted, and the protected master project remained unsaved.

---

## 20. Ten-minute and five-minute operating target

The deterministic warp is no longer the bottleneck. `tools/sanborn_georeference.py` converts a reviewed three-point QGIS file into a checked full-resolution GeoTIFF, alpha band, checksums, and audit ledger. On the Tile 474 rehearsal it completed the full 7735 × 9239 output in about eleven seconds and reproduced all four accepted pixel checksums.

### 20.1 What the helper automates

- refuses source or existing-output overwrite;
- validates exactly three enabled controls and their source-image bounds;
- converts QGIS's negative source-Y convention correctly for GDAL;
- performs one global affine warp with cubic resampling;
- adds destination alpha without erasing black ink;
- uses tiled, lossless DEFLATE compression;
- verifies CRS, dimensions, bands, checksums, alpha range, compression, and protected-project modification time;
- writes a small `.georef.json` record containing source, point, and output hashes;
- writes through a temporary partial file so an interrupted run cannot look complete.

### 20.2 Realistic time budget

For an ordinary sheet with a clear compass and three unambiguous surviving controls, five minutes is realistic:

| Stage | Target |
|---|---:|
| Read printed number, compass, and street names; locate on the Kauffman map | 60–90 seconds |
| Confirm three widely separated controls against OSM and record source/target points | 90–150 seconds |
| Run the helper and load exactly one root-level layer | 20–40 seconds |
| Blink OSM and Kauffman, check a non-control street, verify hierarchy | 60 seconds |

Ten minutes is the reliable production target across normal sheets because it leaves room for one rejected control or one name change. Five minutes is a fast-path target, not a quota.

Tile 474 is the kind of sheet that may exceed five minutes: the third surviving modern control was missing, Rinehardt/Reinhardt was a false friend, and the Kauffman map had to distinguish a historically valid street relationship from modern ground truth. Do not trade away that reasoning to meet a clock.

### 20.3 Where model speed helps—and where it does not

A faster reasoning model can transcribe labels, search OSM/Kauffman, and narrate choices more quickly. The larger savings come from removing fragile GUI repetition:

- acquire and checksum the LOC source through deterministic file tools;
- keep a single compact street/control table instead of re-reading screenshots;
- let code perform the warp, alpha, compression, and validation;
- use QGIS only for street judgment, visual blinking, and final layer placement;
- click Add once, close the dialog, and verify the root-level layer count before taking another action.

The irreducible task is historical identity: deciding whether a street truly survived, changed name, was severed, or merely resembles another street. That judgment remains the reason OSM and Kauffman are both mandatory.

### 20.4 Future automation priority

The next useful improvement is a three-panel comparison workspace: source crop on the left, Kauffman street grid in the center, and OSM on the right, with one table for source pixels, target coordinates, evidence type, confidence, and rejected candidates. It should propose candidates but never silently promote a Kauffman point to OSM ground truth. Once three controls are approved, the existing helper already completes the rest in seconds.

The build-ready specification is preserved in:

```text
work/instructions/THREE_PANEL_COMPARISON_PLAN.md
```

---

## 21. Three-panel comparison plan: permanent operating decisions

The three-panel workspace is intended to shorten street discovery without weakening historical judgment.

Permanent decisions:

1. The first version is a local comparison window, not a QGIS plugin. It does not upload maps or save the master project.
2. The left panel is the north-oriented Sanborn source in image-pixel coordinates.
3. The center panel is Kauffman, used to locate the historic network and vanished streets.
4. The right panel is OSM, used for current ground truth where the intersection genuinely survives.
5. Kauffman and OSM stay synchronized to the same EPSG:3857 extent; the unreferenced source remains an independent pixel view until controls link it geographically.
6. Every candidate records street names, source and target coordinates, evidence type, confidence, acceptance state, and rejection reason.
7. Clicking a target in Kauffman automatically marks the candidate Kauffman-assisted. It can never silently become an OSM control.
8. Rejected candidates remain in the session record so false friends are not rediscovered.
9. Exactly three wide, non-collinear controls remain the default.
10. A low-resolution OSM and Kauffman preview must pass before the full helper runs.
11. The tool warns about small or collinear triangles, evidence confusion, severe scale mismatch, cardinal-grid failure, output collisions, and protected-project timestamp changes.
12. After approval, the existing helper performs the final affine warp, alpha, compression, checksums, and ledger.
13. QGIS remains the final visual QA and layer-organization environment. Add exactly one root-level 1911 layer and leave the protected project unsaved.

The first regression target is Tile 474. The workspace must reproduce its three accepted controls, retain the Wyman and Rinehardt/Reinhardt rejection reasons, reproduce the accepted four pixel checksums, and never describe Boulevard–Tennelle as surviving OSM.

Live QGIS MCP verification completed on 2026-07-14 established:

- QGIS version `3.42.1-Münster`;
- protected project path `/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book/JLS Master Map File.qgz`;
- project CRS `EPSG:3857` with 42 loaded map layers;
- exact basemap name `Open Street Map`, provided as an XYZ tile source through QGIS's `wms` provider in EPSG:3857;
- exact historical reference name `1921 Atlanta Kauffman Map_modified`, a 19035 × 17464 three-band GDAL raster natively in EPSG:4326;
- exact index name `1911 Sanborn Index Orthorectified`, a 7251 × 6957 three-band GDAL raster natively in EPSG:4326;
- the index is a raster and therefore has no searchable tile-number attribute field;
- Tile 474 is a root-level 7735 × 9239 four-band GDAL raster in EPSG:3857 with renderer opacity `1.0` and alpha band `4`.

The three-panel workspace must reproject Kauffman and the index into the synchronized EPSG:3857 comparison view. It must use visual index reading, a recorded coordinate, or a separately built lookup table rather than inventing index fields. Verify any future QGIS Processing algorithm identifier live before using it.
