#!/usr/bin/env python3

import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import sanborn_batch  # noqa: E402


class GeoreferenceIntegrationTests(unittest.TestCase):
    def test_real_gdal_warp_embeds_provenance_and_passes_resume_verification(self):
        required = ("gdalinfo", "gdal_translate", "gdalwarp", "gdal_edit.py")
        if not all(shutil.which(program) for program in required):
            self.skipTest("The full local GDAL toolchain is required")
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "source.tif"
            points = folder / "controls.points"
            output = folder / "Sanborn 1911 -- Tile 999_georeferenced.tif"
            ledger = output.with_suffix(".georef.json")
            protected = folder / "protected.qgz"
            database = folder / "queue.sqlite3"
            # Fixture creation needs no GDAL executable. Keep the actual warp,
            # embedded evidence and resume verification on the real toolchain.
            Image.new("RGB", (100, 100), (230, 220, 190)).save(source, format="TIFF")
            # Four measured points under a known rotation, uniform scale and shift
            # (0.06 EPSG:3857 units, about 0.05 ground metres, per source pixel).
            scale, angle = 0.06, math.radians(30.0)
            a, b = scale * math.cos(angle), scale * math.sin(angle)
            c, d = -9393003.0, 3996003.0
            rows = [(10, 10), (90, 10), (10, 90), (90, 90)]
            points.write_text(
                "#CRS: EPSG:3857\n"
                "mapX,mapY,sourceX,sourceY,enable\n"
                + "".join(
                    f"{a * x + b * y + c!r},{b * x - a * y + d!r},{x},{-y},1\n"
                    for x, y in rows
                ),
                encoding="utf-8",
            )
            protected.write_text("synthetic protected project\n", encoding="utf-8")
            protected_mtime = protected.stat().st_mtime_ns
            command = [
                sys.executable,
                str(ROOT / "tools" / "sanborn_georeference.py"),
                "--source",
                str(source),
                "--points",
                str(points),
                "--output",
                str(output),
                "--protected-project",
                str(protected),
                "--confidence",
                "three-osm",
                "--quality-note",
                "Synthetic metadata-bound integration.",
                "--expected-target-bbox",
                "-9393250",
                "3995750",
                "-9392750",
                "3996250",
                "--expected-target-seed",
                "-9393000",
                "3996000",
                "--max-target-seed-distance",
                "250",
            ]
            for label in (
                "Auburn Avenue x Butler Street",
                "Auburn Avenue x Fort Street",
                "Houston Street x Butler Street",
                "Houston Street x Fort Street",
            ):
                command.extend(["--control-label", label])
            subprocess.run(command, check=True, stdout=subprocess.PIPE, text=True)

            record = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(protected.stat().st_mtime_ns, protected_mtime)
            transformation = record["transformation"]
            self.assertEqual(transformation["fit_model"], "similarity")
            self.assertEqual(transformation["pipeline"], "local-first-similarity-v3")
            diagnostics = transformation["diagnostics"]
            self.assertEqual(diagnostics["point_count"], 4)
            self.assertEqual(diagnostics["skew_degrees"], 0.0)
            self.assertAlmostEqual(diagnostics["rotation_degrees"], 30.0, places=6)
            self.assertLess(diagnostics["leave_one_out_rms_ground_metres"], 1e-6)
            for expected, actual in zip((c, a, b, d, b, -a), diagnostics["geotransform"]):
                self.assertAlmostEqual(actual, expected, places=6)
            # The warped raster's extent is the rotated scan footprint, so the
            # final pixels come from the similarity geotransform, not GCPs.
            info = json.loads(subprocess.run(
                ["gdalinfo", "-json", str(output)], check=True, stdout=subprocess.PIPE, text=True
            ).stdout)
            corners = [(0, 0), (100, 0), (0, 100), (100, 100)]
            footprint_x = [a * x + b * y + c for x, y in corners]
            footprint_y = [b * x - a * y + d for x, y in corners]
            geotransform = info["geoTransform"]
            self.assertAlmostEqual(geotransform[0], min(footprint_x), delta=abs(geotransform[1]) * 1.1)
            self.assertAlmostEqual(geotransform[3], max(footprint_y), delta=abs(geotransform[5]) * 1.1)
            self.assertEqual(
                record["transformation"]["affine_provenance_signature"],
                sanborn_batch.affine_provenance_signature(
                    sanborn_batch.sha256(source),
                    sanborn_batch.sha256(points),
                    "EPSG:3857",
                    record["transformation"]["diagnostics"],
                    record["transformation"]["safety_limits"],
                ),
            )

            with sanborn_batch.connect(database) as db:
                db.execute(
                    """
                    INSERT INTO tiles(
                        tile, status, updated_utc, source_path, source_sha256,
                        points_path, target_seed_x, target_seed_y,
                        target_seed_max_distance, target_seed_quality,
                        target_seed_ambiguous
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        999,
                        "approved",
                        sanborn_batch.utc_now(),
                        str(source),
                        sanborn_batch.sha256(source),
                        str(points),
                        -9393000.0,
                        3996000.0,
                        250.0,
                        "high",
                        0,
                    ),
                )
                db.commit()
                row = db.execute("SELECT * FROM tiles WHERE tile=999").fetchone()
                with mock.patch.object(sanborn_batch, "PROTECTED_PROJECT", protected):
                    verified = sanborn_batch._verify_final_pair(row, output, ledger)

            self.assertEqual(verified["output"]["sha256"], sanborn_batch.sha256(output))
            self.assertEqual(
                verified["transformation"]["affine_provenance_signature"],
                record["transformation"]["affine_provenance_signature"],
            )


if __name__ == "__main__":
    unittest.main()
