import copy
import json
import math
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import sanborn_historical as historical
from sanborn_review import create_packet, current_review_state, approve_packet, require_approval
from test_review_packet import LocalReviewPacketTests, inverse_mercator

class HistoricalMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.source = {"width": 1000, "height": 1000}
        self.reference = {"width": 1000, "height": 1000,
            "geotransform": [-84.4, .000002, 0, 33.75, 0, -.000002]}
        pixels = [(100,100),(900,100),(100,900),(350,350),(800,700),(400,850)]
        self.rows = [{"label":f"Street {i} × Avenue {i}", "role":"fit" if i<3 else "check",
            "source_x":x,"source_y":y,"reference_x":x,"reference_y":y} for i,(x,y) in enumerate(pixels)]

    def test_independent_checks_do_not_fit_the_transform(self):
        fit = historical.assess_measurements(self.rows,self.source,self.reference)
        self.rows[3]["reference_x"] += 5
        moved = historical.assess_measurements(self.rows,self.source,self.reference)
        self.assertEqual(fit[2],moved[2])
        self.assertGreater(moved[3]["rms_ground_metres"],fit[3]["rms_ground_metres"])
        self.assertEqual(moved[3]["count"],3)

    def test_three_perfect_fit_points_are_insufficient(self):
        with self.assertRaisesRegex(ValueError,"at least three"):
            historical.assess_measurements(self.rows[:3],self.source,self.reference)

    def test_blank_coverage_cannot_supply_a_corner(self):
        with self.assertRaisesRegex(ValueError,"blank or undrawn"):
            historical.assess_measurements(self.rows,self.source,self.reference,coverage=lambda x,y:False)

    def test_wrong_check_location_is_rejected(self):
        self.rows[3]["reference_x"] += 200
        with self.assertRaisesRegex(ValueError,"Withheld streets disagree"):
            historical.assess_measurements(self.rows,self.source,self.reference)

    def test_nonfinite_or_out_of_bounds_coordinates_are_rejected(self):
        for value in (float("nan"),float("inf"),True,1001):
            with self.subTest(value=value):
                rows=copy.deepcopy(self.rows);rows[3]["reference_x"]=value
                with self.assertRaises(ValueError):historical.assess_measurements(rows,self.source,self.reference)

    def test_same_corner_cannot_be_a_fit_and_check(self):
        self.rows[3].update({k:self.rows[0][k] for k in ("source_x","source_y","reference_x","reference_y")})
        with self.assertRaisesRegex(ValueError,"different location"):
            historical.assess_measurements(self.rows,self.source,self.reference)

    def test_checks_must_span_the_sheet(self):
        for i in range(3,6):
            self.rows[i]["source_x"]=self.rows[i]["reference_x"]=500+i
        with self.assertRaisesRegex(ValueError,"both directions"):
            historical.assess_measurements(self.rows,self.source,self.reference)

    def test_planned_roads_are_not_primary_controls(self):
        self.assertFalse(historical.profile("washington-rawson-adjustments-1958")["control_eligible"])
        with self.assertRaisesRegex(ValueError,"Planned streets"):
            historical.validate_evidence({"selection_origin":"historical-reference",
                "reference_profile":historical.profile("washington-rawson-adjustments-1958")})


class HistoricalPreviewTests(unittest.TestCase):
    def test_jpeg2000_preview_preserves_rgb_values_and_aspect_ratio(self):
        from PIL import Image
        if not shutil.which('gdal_translate'):
            self.skipTest('The local GDAL toolchain is required')
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            source=folder/'source.JP2'; preview=folder/'source.png'
            with Image.new('RGB',(640,480),(180,130,70)) as image:
                image.paste((24,57,93),(320,0,640,480))
                image.save(source,format='JPEG2000')
            before=historical.file_record(source)
            historical.create_preview(source,preview,160)
            with Image.open(preview) as image:
                self.assertEqual(image.size,(160,120))
                self.assertEqual(image.mode,'RGB')
                self.assertEqual(image.getpixel((40,60)),(180,130,70))
                self.assertEqual(image.getpixel((120,60)),(24,57,93))
            self.assertEqual(historical.file_record(source),before)

    def test_tiff_reference_preview_preserves_alpha_and_rgb_values(self):
        from PIL import Image
        if not shutil.which('gdal_translate'):
            self.skipTest('The local GDAL toolchain is required')
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            source=folder/'reference.tif'; preview=folder/'reference.png'
            with Image.new('RGBA',(640,480),(180,130,70,255)) as image:
                image.paste((24,57,93,0),(320,0,640,480))
                image.save(source,format='TIFF')
            before=historical.file_record(source)
            historical.create_preview(source,preview,160)
            with Image.open(preview) as image:
                self.assertEqual(image.size,(160,120))
                self.assertEqual(image.mode,'RGBA')
                self.assertEqual(image.getpixel((40,60)),(180,130,70,255))
                self.assertEqual(image.getpixel((120,60)),(24,57,93,0))
            self.assertEqual(historical.file_record(source),before)


class HistoricalPacketTests(LocalReviewPacketTests):
    # Reuse the existing synthetic source/OSM/Kauffman setup, not production data.
    def test_topo_and_withheld_evidence_are_locked_to_approval(self):
        reference_dir=self.root/'Stage 1 -Orthorectified Atlanta Maps to print'
        reference_dir.mkdir()
        reference=reference_dir/historical.profile('washington-rawson-topo-1958')['file_name']
        subprocess.run(['gdalwarp','-q','-t_srs','EPSG:4326',str(self.kauffman),str(reference)],check=True)
        reference_info=historical.metadata(reference)
        preview=self.root/'topo-preview.png'
        subprocess.run(['gdal_translate','-q','-of','PNG',str(reference),str(preview)],check=True)
        gt=reference_info['geotransform']
        rows=[]
        for i,(x,y) in enumerate([(100,100),(900,100),(100,900),(350,350),(800,700),(400,850)]):
            lon,lat=inverse_mercator(self.base_x+x-100,self.base_y-y+100)
            rows.append({'label': f'Street {i} × Avenue {i}','role':'fit' if i<3 else 'check',
                'source_x':x,'source_y':y,'reference_x':(lon-gt[0])/gt[1],'reference_y':(lat-gt[3])/gt[5]})
        source_info=historical.metadata(self.source)
        controls,checks,diagnostics,summary=historical.assess_measurements(rows,source_info,reference_info)
        record={'tile':486,'selection_origin':'historical-reference','reference_profile':historical.profile('washington-rawson-topo-1958'),
            'current_source':historical.file_record(self.source),'reference':historical.file_record(reference),
            'reference_preview':historical.file_record(preview),'measurements':rows,'source_info':source_info,
            'reference_info':reference_info,'controls':controls,'check_corners':checks,'diagnostics':diagnostics,
            'independent_checks':summary}
        evidence=self.root/'historical-evidence.json';evidence.write_text(json.dumps(record))
        args=self._create_args();args.replace=True;args.historical_evidence=evidence
        args.control_label=[row['label'] for row in rows[:3]]
        seed=json.loads(args.target_seed_json);seed['tile']=486;args.target_seed_json=json.dumps(seed)
        # Synthetic reference has sparse grid ink. The dedicated coverage test above
        # verifies rejection; here bypass only that visual-content classifier.
        with mock.patch.object(historical,'MAP_ROOT',self.root), mock.patch.object(historical,'drawn_coverage',return_value=True):
            create_packet(args)
            review=json.loads((self.review_dir/'review.json').read_text())
            self.assertIn('historical_overlay',review['artifacts'])
            self.assertEqual(review['render_spec']['historical_reference']['independent_checks']['count'],3)
            from PIL import Image
            with Image.open(self.review_dir/'review-contact-sheet.png') as image:self.assertEqual(image.height,2112)
            current_review_state(review)
            approval_args=type('Args',(),{'review_dir':self.review_dir,'approved_by':'Test','note':''})()
            approve_packet(approval_args)
            reviewed,approval=require_approval(self.review_dir)
            self.assertIn('historical_overlay',approval['geographic_verification']['artifact_sha256'])
            from sanborn_placement_policy import require_current_placement_evidence
            require_current_placement_evidence(486, reviewed)
            frozen,_=require_approval(self.review_dir,frozen=True)
            require_current_placement_evidence(486, frozen)
            with self.assertRaisesRegex(RuntimeError, 'superseded'):
                require_current_placement_evidence(487, frozen)
            changed=json.loads(evidence.read_text());changed['measurements'][3]['reference_x']+=30
            evidence.write_text(json.dumps(changed))
            with self.assertRaisesRegex(RuntimeError,'changed'):
                require_approval(self.review_dir)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([loader.loadTestsFromTestCase(HistoricalMeasurementTests),
        loader.loadTestsFromTestCase(HistoricalPreviewTests),
        HistoricalPacketTests("test_topo_and_withheld_evidence_are_locked_to_approval")])
