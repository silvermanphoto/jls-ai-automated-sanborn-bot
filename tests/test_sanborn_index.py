#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import sanborn_index  # noqa: E402
from sanborn_index import (  # noqa: E402
    build_seed_record,
    cluster_detections,
    digit_groups,
    observations_to_detections,
    pixel_to_map,
)


class SanbornIndexTests(unittest.TestCase):
    def test_digit_groups_preserve_multiple_printed_numbers(self):
        groups = digit_groups("E 349 - 350")
        self.assertEqual([number for number, _ in groups], [349, 350])
        self.assertLess(groups[0][1], groups[1][1])
        self.assertEqual(digit_groups("1911"), [])
        self.assertEqual([number for number, _ in digit_groups("·154")], [154])

    def test_vision_bottom_left_coordinates_become_preview_top_left(self):
        crops = {"crop.png": {"left": 100, "top": 200, "width": 500, "height": 400}}
        observations = [
            {
                "image": "crop.png",
                "text": "154",
                "confidence": 1.0,
                "x": 0.4,
                "y": 0.7,
                "width": 0.1,
                "height": 0.05,
            }
        ]
        rows = observations_to_detections(observations, crops)
        self.assertEqual(rows[0]["tile"], 154)
        self.assertAlmostEqual(rows[0]["preview_x"], 325)
        self.assertAlmostEqual(rows[0]["preview_y"], 310)

    def test_overlapping_crop_reads_cluster_and_ambiguity_is_retained(self):
        detections = [
            {"tile": 154, "preview_x": 100, "preview_y": 200, "confidence": 1.0, "crop": "a", "vision_text": "154", "exact_digits": True},
            {"tile": 154, "preview_x": 102, "preview_y": 198, "confidence": 1.0, "crop": "b", "vision_text": "154", "exact_digits": True},
            {"tile": 154, "preview_x": 900, "preview_y": 800, "confidence": 0.3, "crop": "c", "vision_text": "154", "exact_digits": True},
        ]
        clusters = cluster_detections(detections, radius=20)
        self.assertEqual(clusters[154][0]["support"], 2)
        self.assertEqual(len(clusters[154]), 2)

    def test_geotransform_and_seed_record(self):
        self.assertEqual(pixel_to_map(10, 20, [1000, 2, 0, 2000, 0, -3]), (1020, 1940))
        metadata = {
            "width": 2000,
            "height": 1000,
            "geotransform": [1000, 2, 0, 2000, 0, -3],
        }
        clusters = {
            154: [
                {
                    "preview_x": 500,
                    "preview_y": 250,
                    "support": 3,
                    "mean_confidence": 1.0,
                    "score": 12.0,
                }
            ]
        }
        seed = build_seed_record(metadata, (1000, 500), clusters)[0]
        self.assertEqual(seed["tile"], 154)
        self.assertEqual((seed["map_x"], seed["map_y"]), (3000, 500))
        self.assertEqual(seed["quality"], "high")

    def test_index_build_records_stable_raster_and_preview_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            raster = folder / "index.tif"
            raster.write_bytes(b"index raster fixture")
            output = folder / "seeds.json"
            cache = folder / "cache"
            metadata = {
                "width": 2000,
                "height": 1000,
                "geotransform": [1000.0, 2.0, 0.0, 2000.0, 0.0, -3.0],
                "crs": "EPSG:3857",
            }

            def make_preview(_raster, path, _width):
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (1000, 500), "white").save(path)
                return 1000, 500

            arguments = argparse.Namespace(
                index=raster,
                output=output,
                cache=cache,
                preview_width=1000,
                crop_size=[],
                stride=0,
                maximum_tile=549,
                cluster_radius=55.0,
            )
            with mock.patch("sanborn_index.raster_metadata", return_value=metadata), mock.patch(
                "sanborn_index.make_preview", side_effect=make_preview
            ), mock.patch("sanborn_index.compile_native_recognizer"), mock.patch(
                "sanborn_index.make_crops", return_value={}
            ), mock.patch("sanborn_index.recognize_crops", return_value=[]):
                sanborn_index.cmd_build(arguments)

            record = json.loads(output.read_text(encoding="utf-8"))
            preview = cache / "index-preview.png"
            self.assertEqual(record["index"]["sha256"], hashlib.sha256(raster.read_bytes()).hexdigest())
            self.assertEqual(record["preview"]["sha256"], hashlib.sha256(preview.read_bytes()).hexdigest())
            self.assertEqual(record["preview"]["bytes"], preview.stat().st_size)

    def test_native_recognizer_is_published_atomically_and_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "vision.m"
            source.write_text("fixture source", encoding="utf-8")
            binary = folder / "vision-helper"
            binary.write_bytes(b"previous complete helper")
            os.utime(binary, ns=(1, 1))
            calls = []

            def successful_compile(command, **_kwargs):
                if command[0] == "/usr/bin/clang":
                    calls.append(command)
                    Path(command[-1]).write_bytes(b"new complete helper")
                    return mock.Mock(stdout="", stderr="")
                return mock.Mock(stdout="[]\n", stderr="")

            with mock.patch.object(sanborn_index, "NATIVE_SOURCE", source), mock.patch(
                "sanborn_index.shutil.which", return_value="/usr/bin/clang"
            ), mock.patch("sanborn_index.run", side_effect=successful_compile):
                sanborn_index.compile_native_recognizer(binary, folder / "module-cache")
                sanborn_index.compile_native_recognizer(binary, folder / "module-cache")
            self.assertEqual(binary.read_bytes(), b"new complete helper")
            self.assertEqual(len(calls), 1)
            self.assertEqual(list(folder.glob(".vision-helper.*.compiling")), [])

    def test_failed_native_compile_preserves_previous_complete_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "vision.m"
            source.write_text("fixture source", encoding="utf-8")
            binary = folder / "vision-helper"
            binary.write_bytes(b"previous complete helper")
            os.utime(binary, ns=(1, 1))

            def failed_compile(command, **_kwargs):
                Path(command[-1]).write_bytes(b"partial")
                raise RuntimeError("injected compile failure")

            with mock.patch.object(sanborn_index, "NATIVE_SOURCE", source), mock.patch(
                "sanborn_index.shutil.which", return_value="/usr/bin/clang"
            ), mock.patch("sanborn_index.run", side_effect=failed_compile):
                with self.assertRaisesRegex(RuntimeError, "injected compile failure"):
                    sanborn_index.compile_native_recognizer(binary, folder / "module-cache")
            self.assertEqual(binary.read_bytes(), b"previous complete helper")
            self.assertEqual(list(folder.glob(".vision-helper.*.compiling")), [])

    def test_three_processes_compile_once_and_only_publish_a_complete_helper(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "vision.m"
            source.write_text("fixture source", encoding="utf-8")
            binary = folder / "vision-helper"
            module_cache = folder / "module-cache"
            compile_log = folder / "compile.log"
            fake_clang = folder / "clang"
            fake_clang.write_text(
                f"""#!{sys.executable}
import os
from pathlib import Path
import sys
import time

output = Path(sys.argv[sys.argv.index('-o') + 1])
with Path(os.environ['FAKE_CLANG_LOG']).open('a', encoding='utf-8') as log:
    log.write('compile\\n')
    log.flush()
time.sleep(0.15)
output.write_text('#!{sys.executable}\\nprint(\"[]\")\\n', encoding='utf-8')
""",
                encoding="utf-8",
            )
            fake_clang.chmod(0o755)
            worker = (
                "from pathlib import Path; import sanborn_index, sys; "
                "sanborn_index.NATIVE_SOURCE=Path(sys.argv[1]); "
                "sanborn_index.compile_native_recognizer(Path(sys.argv[2]), Path(sys.argv[3]))"
            )
            environment = dict(os.environ)
            environment["PATH"] = f"{folder}:{environment.get('PATH', '')}"
            environment["FAKE_CLANG_LOG"] = str(compile_log)
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "tools")
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        worker,
                        str(source),
                        str(binary),
                        str(module_cache),
                    ],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(3)
            ]
            results = [process.communicate(timeout=15) for process in processes]
            for process, (stdout, stderr) in zip(processes, results):
                self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual(compile_log.read_text(encoding="utf-8").splitlines(), ["compile"])
            self.assertTrue(binary.read_text(encoding="utf-8").endswith('print("[]")\n'))
            self.assertEqual(list(folder.glob(".vision-helper.*.compiling")), [])


class ManualSeedConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.raster = self.folder / "1911-index.tif"
        self.raster.write_bytes(b"stable georeferenced index raster fixture")
        self.preview = self.folder / "index-preview.png"
        Image.new("RGB", (1000, 500), "white").save(self.preview)
        self.metadata = {
            "width": 2000,
            "height": 1000,
            "geotransform": [1000.0, 2.0, 0.0, 2000.0, 0.0, -3.0],
            "crs": "EPSG:3857",
        }
        self.index_json = self.folder / "tile-location-seeds.json"
        self.override_dir = self.folder / "manual-overrides"

    def tearDown(self):
        self.temporary.cleanup()

    def write_record(self, seeds, *, index_overrides=None, preview_overrides=None):
        index = {"path": str(self.raster), **self.metadata}
        index.update(index_overrides or {})
        preview = {
            "path": str(self.preview),
            "sha256": hashlib.sha256(self.preview.read_bytes()).hexdigest(),
            "bytes": self.preview.stat().st_size,
            "width": 1000,
            "height": 500,
        }
        preview.update(preview_overrides or {})
        record = {
            "schema_version": 1,
            "created_utc": "2026-07-15T04:00:00+00:00",
            "purpose": "fixture independent location safeguard",
            "index": index,
            "preview": preview,
            "seed_count": len(seeds),
            "seeds": seeds,
        }
        self.index_json.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    def arguments(self, **overrides):
        values = {
            "index_json": self.index_json,
            "tile": 154,
            "index_raster": self.raster,
            "override_dir": self.override_dir,
            "preview_pixel": [500.0, 250.0],
            "map_coordinate": None,
            "reviewer": "Joel Silverman",
            "note": "The printed 154 label is centered here on the index.",
            "tolerance": 200.0,
            "replace_existing": False,
            "override_high_confidence": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def override_path(self, tile=154):
        return self.override_dir / f"tile-{tile:04d}.json"

    def ambiguous_seed(self):
        return {
            "tile": 154,
            "map_x": 2800.0,
            "map_y": 700.0,
            "crs": "EPSG:3857",
            "suggested_max_distance": 250.0,
            "quality": "review",
            "ambiguous": True,
            "support": 2,
            "alternatives": [
                {"preview_x": 490.0, "preview_y": 245.0, "vision_texts": ["154"]},
                {"preview_x": 800.0, "preview_y": 100.0, "vision_texts": ["154"]},
            ],
        }

    def test_preview_confirmation_creates_one_high_manual_seed_and_preserves_ocr(self):
        original = self.ambiguous_seed()
        self.write_record([original])
        base_before = self.index_json.read_bytes()
        with mock.patch("sanborn_index.raster_metadata", return_value=self.metadata):
            self.assertEqual(sanborn_index.cmd_confirm(self.arguments()), 0)

        self.assertEqual(self.index_json.read_bytes(), base_before)
        record = json.loads(self.override_path().read_text(encoding="utf-8"))
        self.assertEqual(record["seed_count"], 1)
        self.assertEqual(len(record["seeds"]), 1)
        seed = record["seeds"][0]
        self.assertEqual((seed["map_x"], seed["map_y"]), (3000.0, 500.0))
        self.assertEqual(seed["source"], "manual-confirmation")
        self.assertEqual(seed["quality"], "high")
        self.assertFalse(seed["ambiguous"])
        self.assertEqual(seed["suggested_max_distance"], 200.0)
        self.assertEqual(seed["original_ocr_evidence"], [original])
        self.assertEqual(record["base_seed_records"], [original])
        self.assertIsNone(record["history"][0]["replaced_manual_seed"])
        evidence = seed["manual_confirmation"]
        self.assertEqual(evidence["reviewer"], "Joel Silverman")
        self.assertEqual(evidence["method"], "preview-pixel")
        self.assertEqual(evidence["input"]["preview"], record["preview"])
        self.assertEqual(record["preview"]["sha256"], hashlib.sha256(self.preview.read_bytes()).hexdigest())
        self.assertEqual(record["preview"]["bytes"], self.preview.stat().st_size)
        self.assertTrue(Path(evidence["index_raster"]["path"]).is_absolute())
        expected_raster_hash = hashlib.sha256(self.raster.read_bytes()).hexdigest()
        self.assertEqual(evidence["index_raster"]["sha256"], expected_raster_hash)
        self.assertEqual(record["index_raster"]["sha256"], expected_raster_hash)
        self.assertEqual(
            record["base_index"]["sha256"], hashlib.sha256(base_before).hexdigest()
        )

    def test_missing_seed_accepts_in_bounds_epsg3857_coordinate(self):
        self.write_record([])
        args = self.arguments(
            preview_pixel=None,
            map_coordinate=[3000.0, 500.0],
            tolerance=250.0,
        )
        with mock.patch("sanborn_index.raster_metadata", return_value=self.metadata):
            sanborn_index.cmd_confirm(args)
        record = json.loads(self.override_path().read_text(encoding="utf-8"))
        seed = record["seeds"][0]
        self.assertEqual(seed["manual_confirmation"]["method"], "map-coordinate")
        self.assertEqual((seed["index_pixel_x"], seed["index_pixel_y"]), (1000.0, 500.0))
        self.assertEqual(seed["original_ocr_evidence"], [])
        self.assertEqual(record["base_seed_records"], [])

    def test_rerun_requires_explicit_replacement_and_keeps_prior_history(self):
        original = self.ambiguous_seed()
        self.write_record([original])
        with mock.patch("sanborn_index.raster_metadata", return_value=self.metadata):
            sanborn_index.cmd_confirm(self.arguments())
            with self.assertRaisesRegex(RuntimeError, "--replace-existing"):
                sanborn_index.cmd_confirm(
                    self.arguments(
                        preview_pixel=None,
                        map_coordinate=[3200.0, 800.0],
                        reviewer="Second reviewer",
                        note="A closer inspection moved the center.",
                    )
                )
            sanborn_index.cmd_confirm(
                self.arguments(
                    preview_pixel=None,
                    map_coordinate=[3200.0, 800.0],
                    reviewer="Second reviewer",
                    note="A closer inspection moved the center.",
                    replace_existing=True,
                )
            )
        record = json.loads(self.override_path().read_text(encoding="utf-8"))
        self.assertEqual(len(record["history"]), 2)
        self.assertEqual(
            record["history"][1]["replaced_manual_seed"]["source"],
            "manual-confirmation",
        )
        self.assertEqual(record["seeds"][0]["original_ocr_evidence"], [original])
        self.assertEqual(
            (record["seeds"][0]["map_x"], record["seeds"][0]["map_y"]),
            (3200.0, 800.0),
        )

    def test_refuses_mismatched_raster_identity_hash_and_metadata(self):
        original = self.ambiguous_seed()
        other = self.folder / "other-index.tif"
        other.write_bytes(b"other")
        self.write_record([original])
        with self.assertRaisesRegex(RuntimeError, "not the raster recorded"):
            sanborn_index.cmd_confirm(self.arguments(index_raster=other))

        self.write_record([original], index_overrides={"sha256": "0" * 64})
        with self.assertRaisesRegex(RuntimeError, "no longer matches"):
            sanborn_index.cmd_confirm(self.arguments())

        self.write_record([original])
        changed_metadata = {**self.metadata, "width": 1999}
        with mock.patch("sanborn_index.raster_metadata", return_value=changed_metadata):
            with self.assertRaisesRegex(RuntimeError, "dimensions do not match"):
                sanborn_index.cmd_confirm(self.arguments())

    def test_refuses_bad_preview_outside_location_and_unsafe_tolerance(self):
        self.write_record([self.ambiguous_seed()], preview_overrides={"width": 999})
        with mock.patch("sanborn_index.raster_metadata", return_value=self.metadata):
            with self.assertRaisesRegex(RuntimeError, "preview dimensions"):
                sanborn_index.cmd_confirm(self.arguments())

        self.write_record([self.ambiguous_seed()])
        with mock.patch("sanborn_index.raster_metadata", return_value=self.metadata):
            with self.assertRaisesRegex(RuntimeError, "outside the georeferenced"):
                sanborn_index.cmd_confirm(
                    self.arguments(preview_pixel=None, map_coordinate=[99_999.0, 99_999.0])
                )
        for tolerance in (0, -1, 250.0001, float("inf")):
            with self.subTest(tolerance=tolerance):
                with self.assertRaisesRegex(RuntimeError, "no greater than 250"):
                    sanborn_index.cmd_confirm(self.arguments(tolerance=tolerance))

    def test_preview_pixel_refuses_same_dimensions_and_bytes_with_different_content(self):
        self.write_record([self.ambiguous_seed()])
        base_before = self.index_json.read_bytes()
        recorded_size = self.preview.stat().st_size
        replacement = self.folder / "replacement-preview.png"
        Image.new("RGB", (1000, 500), "black").save(replacement)
        replacement_bytes = replacement.read_bytes()
        self.assertLessEqual(len(replacement_bytes), recorded_size)
        self.preview.write_bytes(replacement_bytes + b"\0" * (recorded_size - len(replacement_bytes)))
        self.assertEqual(self.preview.stat().st_size, recorded_size)
        with Image.open(self.preview) as image:
            self.assertEqual(image.size, (1000, 500))
            image.verify()

        with mock.patch("sanborn_index.raster_metadata", return_value=self.metadata):
            with self.assertRaisesRegex(RuntimeError, "preview no longer matches"):
                sanborn_index.cmd_confirm(self.arguments())
        self.assertEqual(self.index_json.read_bytes(), base_before)
        self.assertFalse(self.override_path().exists())

    def test_preview_pixel_refuses_legacy_index_without_preview_hash(self):
        self.write_record(
            [self.ambiguous_seed()],
            preview_overrides={"sha256": None, "bytes": None},
        )
        with mock.patch("sanborn_index.raster_metadata", return_value=self.metadata):
            with self.assertRaisesRegex(RuntimeError, "rebuild this legacy"):
                sanborn_index.cmd_confirm(self.arguments())
        self.assertFalse(self.override_path().exists())

    def test_refuses_to_override_unique_high_quality_ocr_seed(self):
        seed = self.ambiguous_seed()
        seed.update({"quality": "high", "ambiguous": False})
        self.write_record([seed])
        before = self.index_json.read_bytes()
        with mock.patch("sanborn_index.raster_metadata", return_value=self.metadata):
            with self.assertRaisesRegex(RuntimeError, "already has one unique high-quality"):
                sanborn_index.cmd_confirm(self.arguments())
        self.assertEqual(self.index_json.read_bytes(), before)

    def test_explicit_flag_can_correct_visibly_wrong_high_quality_ocr_seed(self):
        high_seed = self.ambiguous_seed()
        high_seed.update({"quality": "high", "ambiguous": False})
        self.write_record([high_seed])
        base_before = self.index_json.read_bytes()
        with mock.patch("sanborn_index.raster_metadata", return_value=self.metadata):
            sanborn_index.cmd_confirm(
                self.arguments(
                    override_high_confidence=True,
                    note=(
                        "The high-confidence OCR hit is visibly on the wrong printed label; "
                        "this click is centered on the actual 154 label."
                    ),
                )
            )
        record = json.loads(self.override_path().read_text(encoding="utf-8"))
        manual = record["seeds"][0]
        self.assertTrue(manual["manual_confirmation"]["overrode_high_confidence_ocr"])
        self.assertEqual(record["base_seed_records"], [high_seed])
        self.assertEqual(manual["original_ocr_evidence"], [high_seed])
        self.assertEqual(self.index_json.read_bytes(), base_before)

    def test_atomic_replace_failure_leaves_original_json_untouched(self):
        self.write_record([self.ambiguous_seed()])
        base_before = self.index_json.read_bytes()
        with mock.patch("sanborn_index.raster_metadata", return_value=self.metadata):
            sanborn_index.cmd_confirm(self.arguments())
        override_before = self.override_path().read_bytes()
        replacement = self.arguments(
            preview_pixel=None,
            map_coordinate=[3200.0, 800.0],
            note="Replacement that will fail atomically.",
            replace_existing=True,
        )
        with mock.patch("sanborn_index.raster_metadata", return_value=self.metadata), mock.patch(
            "sanborn_index.os.replace", side_effect=OSError("injected replacement failure")
        ):
            with self.assertRaisesRegex(OSError, "injected replacement failure"):
                sanborn_index.cmd_confirm(replacement)
        self.assertEqual(self.index_json.read_bytes(), base_before)
        self.assertEqual(self.override_path().read_bytes(), override_before)
        self.assertEqual(list(self.override_dir.glob(f".{self.override_path().name}.*.tmp")), [])

    def test_confirming_third_tile_cannot_invalidate_two_existing_tile_overrides(self):
        seeds = []
        for tile in (101, 102, 103):
            seed = self.ambiguous_seed()
            seed["tile"] = tile
            seeds.append(seed)
        self.write_record(seeds)
        base_before = self.index_json.read_bytes()
        base_digest = hashlib.sha256(base_before).hexdigest()
        with mock.patch("sanborn_index.raster_metadata", return_value=self.metadata):
            for tile in (101, 102):
                sanborn_index.cmd_confirm(
                    self.arguments(
                        tile=tile,
                        note=f"Reviewed printed tile {tile} on the immutable base index.",
                    )
                )
            first_before = self.override_path(101).read_bytes()
            second_before = self.override_path(102).read_bytes()
            sanborn_index.cmd_confirm(
                self.arguments(
                    tile=103,
                    note="Reviewed printed tile 103 without touching 101 or 102.",
                )
            )

        self.assertEqual(self.index_json.read_bytes(), base_before)
        self.assertEqual(self.override_path(101).read_bytes(), first_before)
        self.assertEqual(self.override_path(102).read_bytes(), second_before)
        for tile in (101, 102, 103):
            record = json.loads(self.override_path(tile).read_text(encoding="utf-8"))
            self.assertEqual(record["base_index"]["sha256"], base_digest)
            self.assertEqual(record["tile"], tile)

    def test_two_replacement_processes_are_serialized_without_losing_history(self):
        self.write_record([self.ambiguous_seed()])
        base_before = self.index_json.read_bytes()
        with mock.patch("sanborn_index.raster_metadata", return_value=self.metadata):
            sanborn_index.cmd_confirm(self.arguments())

        fake_gdalinfo = self.folder / "gdalinfo"
        gdal_payload = {
            "geoTransform": self.metadata["geotransform"],
            "size": [self.metadata["width"], self.metadata["height"]],
            "coordinateSystem": {"wkt": "EPSG:3857 Pseudo-Mercator"},
        }
        fake_gdalinfo.write_text(
            f"""#!{sys.executable}
import json
import time
time.sleep(0.15)
print(json.dumps({gdal_payload!r}))
""",
            encoding="utf-8",
        )
        fake_gdalinfo.chmod(0o755)
        environment = dict(os.environ)
        environment["PATH"] = f"{self.folder}:{environment.get('PATH', '')}"
        tool = Path(__file__).resolve().parents[1] / "tools" / "sanborn_index.py"

        def command(x, y, reviewer):
            return [
                sys.executable,
                str(tool),
                "confirm",
                str(self.index_json),
                "154",
                "--index-raster",
                str(self.raster),
                "--override-dir",
                str(self.override_dir),
                "--map-coordinate",
                str(x),
                str(y),
                "--reviewer",
                reviewer,
                "--note",
                f"{reviewer} deliberately refined the selected map center.",
                "--tolerance",
                "200",
                "--replace-existing",
            ]

        processes = [
            subprocess.Popen(
                command(3100, 650, "Reviewer A"),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ),
            subprocess.Popen(
                command(3200, 800, "Reviewer B"),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ),
        ]
        results = [process.communicate(timeout=15) for process in processes]
        for process, (stdout, stderr) in zip(processes, results):
            self.assertEqual(process.returncode, 0, stdout + stderr)
        record = json.loads(self.override_path().read_text(encoding="utf-8"))
        self.assertEqual(len(record["history"]), 3)
        self.assertEqual(
            {item["reviewer"] for item in record["history"][1:]},
            {"Reviewer A", "Reviewer B"},
        )
        self.assertEqual(self.index_json.read_bytes(), base_before)

    def test_cli_requires_exactly_one_coordinate_pair_and_a_tolerance(self):
        parser = sanborn_index.build_parser()
        arguments = parser.parse_args(
            [
                "confirm",
                str(self.index_json),
                "154",
                "--index-raster",
                str(self.raster),
                "--map-coordinate",
                "3000",
                "500",
                "--reviewer",
                "Joel",
                "--note",
                "Reviewed printed label",
                "--tolerance",
                "250",
            ]
        )
        self.assertEqual(arguments.map_coordinate, [3000.0, 500.0])
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "confirm",
                    str(self.index_json),
                    "154",
                    "--index-raster",
                    str(self.raster),
                    "--reviewer",
                    "Joel",
                    "--note",
                    "Missing coordinates",
                    "--tolerance",
                    "250",
                ]
            )


if __name__ == "__main__":
    unittest.main()
