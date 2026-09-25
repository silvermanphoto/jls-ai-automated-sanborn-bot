#!/usr/bin/env python3
"""Review historical street-center measurements without altering source maps or queue states.

Three fit corners determine the affine transform. At least three other named
corners measure its error independently. Historical reference coordinates remain
historical evidence, never promoted to modern survey truth.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from urllib.parse import quote
from PIL import Image
from sanborn_georeference import (affine_diagnostics, affine_safety_warnings,
    split_affine_safety_warnings, sha256, capture_json, require_program, read_points)

MAP_ROOT = Path('/Users/joelsilverman/Desktop/2024 Files/2024 Atlanta Map Book')
PROJECT_ROOT = Path(os.environ.get('SANBORN_PROJECT_ROOT', Path(__file__).resolve().parents[1])).resolve()
BATCH_DIR = Path(os.environ.get('SANBORN_BATCH_DIR', PROJECT_ROOT / 'batch')).resolve()
REFERENCES = {
    'washington-rawson-topo-1958': {
        'id': 'washington-rawson-topo-1958',
        'label': '1958 Washington–Rawson original topographic map',
        'file_name': '1958 Washington-Rawson Original Topo Map atlpm0129f_geo.tif',
        'role': 'historical-street-reference', 'control_eligible': True,
        'guidance': 'Use original street centerlines within the drawn map. Blank margins are outside useful coverage. Washington widened between 1911 and 1958; curbs and parcel edges are not interchangeable.',
        'uncertainty': 'Existing georeferencing and historic drawing error remain. Independent street checks establish relative agreement, not survey-exact 1911 parcel boundaries.',
        'native_crs': 'EPSG:4326',
    },
    'washington-rawson-adjustments-1958': {
        'id': 'washington-rawson-adjustments-1958',
        'label': '1958 Washington–Rawson street-adjustment plan (revised through 1962)',
        'file_name': '1958 Washington Rawson Urban Redevelopment atlpm0130e_geo.tif',
        'role': 'planned-street-context', 'control_eligible': False,
        'guidance': 'Supporting context only: this plan includes proposed and realigned roads. Use the original topographic map to choose historic street centers.',
        'uncertainty': 'Planned lines do not prove that a street occupied that position in 1911 or 1958.',
        'native_crs': 'EPSG:4326',
    },
}


def fail(message):
    raise ValueError(message)


def profile(reference_id):
    if reference_id not in REFERENCES:
        fail('Choose a known historical reference.')
    return dict(REFERENCES[reference_id])


def reference_path(reference_id):
    return MAP_ROOT / 'Stage 1 -Orthorectified Atlanta Maps to print' / profile(reference_id)['file_name']


def file_record(path):
    path = Path(path).resolve()
    return {'path': str(path), 'bytes': path.stat().st_size, 'sha256': sha256(path)}


def verify_file(record, label):
    path = Path(record['path'])
    if not path.is_file() or file_record(path) != record:
        fail(f'{label} changed. Prepare a fresh comparison before continuing.')
    return path


def valid_tile(tile):
    if type(tile) is not int or not 1 <= tile <= 549:
        fail('Use a printed sheet number from 1 through 549.')
    return tile


@contextmanager
def tile_lock(tile):
    lock_dir = BATCH_DIR / 'locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f'tile-{valid_tile(tile):04d}.lock').open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def prepared_source(tile):
    database = BATCH_DIR / 'sanborn_batch.sqlite3'
    uri = 'file:' + quote(str(database), safe='/') + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM tiles WHERE tile=?', (valid_tile(tile),)).fetchone()
    if row is None or row['status'] not in {'review-ready', 'needs-chatgpt-review'}:
        fail('Historical corners can be chosen only for a prepared sheet. Reopen any locked review in the engine first.')
    if row['printed_number_seen'] != 1:
        fail('The printed sheet number must be confirmed first.')
    source = Path(row['source_path']).resolve()
    if not source.is_file():
        fail('The prepared scan is missing. Prepare this sheet again to restore its original download and recheck the printed number.')
    if sha256(source) != row['source_sha256']:
        fail('The prepared scan has changed since its identity check. Prepare this sheet again before choosing historical corners.')
    return source


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + f'.partial-{os.getpid()}')
    partial.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n')
    os.replace(partial, path)


def metadata(path):
    info = capture_json([require_program('gdalinfo'), '-json', str(path)])
    size, gt = info.get('size'), info.get('geoTransform')
    if not isinstance(size, list) or len(size) != 2:
        fail('The map has no readable pixel dimensions.')
    wkt = info.get('coordinateSystem', {}).get('wkt', '')
    return {'width': size[0], 'height': size[1], 'geotransform': gt, 'wkt': wkt}


def create_preview(raster, preview, width):
    command = [require_program('gdal_translate'), '-q']
    # QGIS's MrSID JPEG2000 reader can fail on reduced LOC scan blocks.
    # Read the unchanged source through OpenJPEG without tone adjustments.
    if Path(raster).suffix.lower() in {'.jp2', '.j2k', '.jpx'}:
        command.extend(['-if', 'JP2OpenJPEG'])
    command.extend(['-of', 'PNG', '-outsize', str(width), '0', str(raster), str(preview)])
    subprocess.run(command, check=True)


def prepare(tile, reference_id):
    spec = profile(reference_id)
    with tile_lock(tile):
        source = prepared_source(tile)
        reference = reference_path(reference_id)
        source_record, ref_record = file_record(source), file_record(reference)
        source_info, ref_info = metadata(source), metadata(reference)
        if '4326' not in ref_info['wkt'] or not ref_info['geotransform']:
            fail('This reference must have verified WGS84 geographic coordinates.')
        directory = BATCH_DIR / 'historical-workspaces' / f'tile-{tile:04d}' / reference_id
        directory.mkdir(parents=True, exist_ok=True)
        previews = {}
        for name, raster, width in [('source', source, 3200), ('reference', reference, 4000)]:
            preview = directory / (name + '.png')
            create_preview(raster, preview, width)
            previews[name] = file_record(preview)
        verify_file(source_record, 'The source scan')
        verify_file(ref_record, 'The historical reference')
        record = {'schema_version': 1, 'tile': tile, 'profile': spec,
                  'source': source_record, 'source_info': source_info,
                  'reference': ref_record, 'reference_info': ref_info, 'previews': previews,
                  'created_utc': datetime.now(timezone.utc).isoformat()}
        record['token'] = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        atomic_json(BATCH_DIR / 'historical-workspaces' / f'tile-{tile:04d}.json', record)
        print(f"Prepared {spec['label']} for sheet {tile}. Choose street centers in both images.")


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        fail('Every clicked coordinate must be a finite number.')
    return float(value)


def target_from_pixel(info, x, y):
    x, y = finite(x), finite(y)
    if not (0 <= x < info['width'] and 0 <= y < info['height']):
        fail('A target lies outside the historical raster.')
    gt = info['geotransform']
    lon, lat = gt[0] + x * gt[1] + y * gt[2], gt[3] + x * gt[4] + y * gt[5]
    if not (-85 < lon < -84 and 33 < lat < 35):
        fail('The reference coordinates are outside Atlanta. Check its CRS.')
    return [6378137 * math.radians(lon), 6378137 * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))]


def drawn_coverage(preview, info, x, y):
    """Reject locally uniform paper, including tan scans. Ink is not road identity."""
    with Image.open(preview) as image:
        image = image.convert('RGBA')
        px, py = x * image.width / info['width'], y * image.height / info['height']
        # Inspect about 150 original pixels (~35 ground metres here), so the
        # street center's clear carriageway can still include its drawn edges.
        radius = max(5, round(150 * image.width / info['width']))
        crop = image.crop((max(0, int(px)-radius), max(0, int(py)-radius),
                           min(image.width, int(px)+radius+1), min(image.height, int(py)+radius+1)))
        pixels = list(crop.getdata())
    luminance = sorted(.2126*r + .7152*g + .0722*b for r,g,b,a in pixels if a > 0)
    if not luminance or len(luminance) < .9*len(pixels):
        return False
    paper = luminance[int(.75*(len(luminance)-1))]
    # Tan paper has a low blue channel everywhere; use contrast against local
    # paper luminance, not the minimum RGB channel or a global white threshold.
    return sum(value < paper-30 for value in luminance) / len(luminance) >= .015


def assess_measurements(rows, source_info, reference_info, *, coverage=None):
    if not isinstance(rows, list) or not 6 <= len(rows) <= 30:
        fail('Choose three fit corners and at least three separate check corners.')
    labels, controls, checks = set(), [], []
    source_points, target_points = [], []
    for i, row in enumerate(rows):
        label = row.get('label', '').strip()
        if not label or len(label) > 180 or not re.search(r'\S\s*(?:×|&|/|\s(?:x|at|and)\s)\s*\S', label, re.IGNORECASE) or label.casefold() in labels:
            fail('Give each distinct crossing both street names, separated by × or &.')
        labels.add(label.casefold())
        sx, sy = finite(row.get('source_x')), finite(row.get('source_y'))
        rx, ry = finite(row.get('reference_x')), finite(row.get('reference_y'))
        if not (0 <= sx < source_info['width'] and 0 <= sy < source_info['height']):
            fail(f'{label} is outside the source scan.')
        target = target_from_pixel(reference_info, rx, ry)
        if coverage and not coverage(rx, ry):
            fail(f'{label} falls in blank or undrawn reference coverage. Choose a visibly drawn street center.')
        if any(math.hypot(sx-x, sy-y) < 3 for x,y in source_points) or any(math.hypot(rx-x, ry-y) < 3 for x,y in target_points):
            fail('Every fit and check corner must be a different location.')
        source_points.append((sx,sy)); target_points.append((rx,ry))
        control = {'candidate_id': i+1, 'label': label, 'source_x': sx, 'source_y': sy,
                   'source_line_gdal': sy, 'map_x': target[0], 'map_y': target[1],
                   'target_x': target[0], 'target_y': target[1], 'reference_x': rx, 'reference_y': ry}
        (controls if row.get('role') == 'fit' else checks if row.get('role') == 'check' else []).append(control)
    if len(controls) != 3 or len(checks) < 3 or len(controls)+len(checks) != len(rows):
        fail('Exactly three corners fit the map; at least three others must be withheld checks.')
    diagnostics = affine_diagnostics(controls, source_info['width'], source_info['height'])
    for axis, dimension in [('source_x', 'width'), ('source_y', 'height')]:
        if (max(c[axis] for c in checks)-min(c[axis] for c in checks)) / source_info[dimension] < .2:
            fail('Spread the withheld check corners across both directions of the sheet.')
    matrix, offset = diagnostics['matrix'], diagnostics['offset']
    for check in checks:
        x,y = check['source_x'],check['source_y']
        predicted = [matrix[0][0]*x+matrix[0][1]*y+offset[0], matrix[1][0]*x+matrix[1][1]*y+offset[1]]
        lat = 2*math.atan(math.exp(check['map_y']/6378137))-math.pi/2
        check['ground_error_metres'] = math.hypot(predicted[0]-check['map_x'],predicted[1]-check['map_y'])*math.cos(lat)
    errors=[p['ground_error_metres'] for p in checks]
    summary={'count':len(checks),'rms_ground_metres':math.sqrt(sum(e*e for e in errors)/len(errors)), 'max_ground_metres':max(errors)}
    if summary['rms_ground_metres'] > 5 or summary['max_ground_metres'] > 10:
        fail(f"Withheld streets disagree by {summary['rms_ground_metres']:.1f} m RMS / {summary['max_ground_metres']:.1f} m maximum. Correct the corners before building a comparison.")
    return controls, checks, diagnostics, summary


def validate_evidence(record, source=None, points=None):
    if record.get('selection_origin') != 'historical-reference':
        fail('The historical street evidence has an unknown origin.')
    spec = profile(record['reference_profile']['id'])
    if record['reference_profile'] != spec or not spec['control_eligible']:
        fail('Planned streets cannot be used as original historical controls.')
    verified_source = verify_file(record['current_source'], 'The source scan')
    reference = verify_file(record['reference'], 'The historical reference')
    if reference != reference_path(spec['id']).resolve():
        fail('Historical evidence names a different reference file.')
    if metadata(verified_source) != record["source_info"] or metadata(reference) != record["reference_info"]:
        fail("Historical raster dimensions or georeferencing no longer match the measured images.")
    preview_record = record.get("reference_preview")
    if not preview_record:
        fail("Historical measurements have no verified drawn-coverage preview.")
    preview = verify_file(preview_record, "The historical measurement preview")
    if source is not None and verified_source != Path(source).resolve():
        fail('The historical corners belong to another source scan.')
    controls, checks, diagnostics, summary = assess_measurements(record['measurements'],record['source_info'],record['reference_info'],coverage=lambda x,y: drawn_coverage(preview,record['reference_info'],x,y))
    if (controls,checks,diagnostics,summary) != (record['controls'],record['check_corners'],record['diagnostics'],record['independent_checks']):
        fail('Historical measurements no longer match their recorded fit and withheld checks.')
    if points is not None:
        crs,saved = read_points(Path(points))
        if crs != 'EPSG:3857' or len(saved)!=3:
            fail('Historical controls must use EPSG:3857.')
        for actual,expected in zip(saved,controls):
            for key in ['source_x','source_line_gdal','map_x','map_y']:
                if abs(actual[key]-expected[key]) > 0.00001:
                    fail('The control file differs from its historical evidence.')
    return record


def export(tile, request):
    with tile_lock(tile):
        source=prepared_source(tile)
        workspace_path=BATCH_DIR/'historical-workspaces'/f'tile-{tile:04d}.json'
        workspace=json.loads(workspace_path.read_text())
        if request.get('workspace_token') != workspace.get('token'):
            fail('The comparison changed. Reload the images before choosing corners.')
        spec=profile(workspace['profile']['id'])
        if not spec['control_eligible']:
            fail('The street-adjustment plan is supporting context, not a control reference.')
        if verify_file(workspace['source'],'The source scan') != source:
            fail('The comparison belongs to another scan.')
        verify_file(workspace['reference'],'The historical reference')
        preview=verify_file(workspace['previews']['reference'],'The reference preview')
        measurements=request.get('measurements')
        controls,checks,diagnostics,summary=assess_measurements(measurements,workspace['source_info'],workspace['reference_info'],coverage=lambda x,y: drawn_coverage(preview,workspace['reference_info'],x,y))
        warnings=affine_safety_warnings(diagnostics)
        distortion,hard=split_affine_safety_warnings(warnings)
        allow=request.get('allow_distortion') is True
        note=str(request.get('distortion_note') or '').strip()
        if hard: fail('Historical corners fail a required geometry gate: '+'; '.join(hard))
        if distortion and not allow: fail('This sheet needs a deliberate distortion exception: '+'; '.join(distortion))
        if allow and (not note or not distortion): fail('A distortion exception needs an actual scale/angle warning and its evidence note.')
        if note and not allow: fail('A distortion note needs the explicit exception choice.')
        record={'schema_version':1,'tile':tile,'selection_origin':'historical-reference',
                'created_utc':datetime.now(timezone.utc).isoformat(),'current_source':workspace['source'],
                'reference_profile':spec,'reference':workspace['reference'],'reference_preview':workspace['previews']['reference'],
                'source_info':workspace['source_info'],'reference_info':workspace['reference_info'],
                'measurements':measurements,'controls':controls,'check_corners':checks,
                'diagnostics':diagnostics,'independent_checks':summary,'safety_warnings':warnings,
                'allow_distortion':allow,'distortion_note':note,
                'status':'historical-corners-awaiting-new-comparison'}
        selected=BATCH_DIR/'selected-controls';selected.mkdir(parents=True,exist_ok=True)
        points=selected/f'tile-{tile:04d}.points'
        text='#CRS: EPSG:3857\nmapX,mapY,sourceX,sourceY,enable\n'+''.join(f"{p['map_x']:.15f},{p['map_y']:.15f},{p['source_x']:.6f},{-p['source_y']:.6f},1\n" for p in controls)
        temporary=points.with_name(points.name+'.partial')
        temporary.write_text(text)
        validate_evidence(record,source,temporary)
        atomic_json(selected/f'tile-{tile:04d}.comparison.json',record)
        os.replace(temporary,points)
        print(f"Saved three historical street corners; {len(checks)} withheld checks: {summary['rms_ground_metres']:.2f} m RMS, {summary['max_ground_metres']:.2f} m maximum. Build a fresh comparison before approval.")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    prepare_parser=sub.add_parser('prepare');prepare_parser.add_argument('tile',type=int);prepare_parser.add_argument('--reference',choices=REFERENCES,required=True)
    export_parser=sub.add_parser('export');export_parser.add_argument('tile',type=int)
    args=parser.parse_args()
    if args.command=='prepare': prepare(args.tile,args.reference)
    else: export(args.tile,json.load(sys.stdin))

if __name__=='__main__':
    try: main()
    except (ValueError,RuntimeError,OSError,KeyError,sqlite3.Error,subprocess.CalledProcessError) as error:
        print(f'ERROR: {error}',file=sys.stderr);raise SystemExit(1)
