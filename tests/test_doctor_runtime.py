#!/usr/bin/env python3

"""Doctor must prove the local toolchain runs, not merely that it is installed."""

import argparse
from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import sanborn_batch


# The real failure of 2026-09-22: every GDAL command aborted at launch because
# Homebrew libheif was linked against a libx265 the installed x265 no longer
# shipped. Every binary was still present on PATH the whole time.
DYLD_FAILURE = (
    "dyld[52341]: Library not loaded: /opt/homebrew/opt/x265/lib/libx265.216.dylib\n"
    "  Referenced from: /opt/homebrew/Cellar/libheif/1.20.2/lib/libheif.1.20.2.dylib\n"
    "  Reason: tried: '/opt/homebrew/opt/x265/lib/libx265.216.dylib' (no such file)"
)
BINDING_FAILURE = (
    "Traceback (most recent call last):\n"
    '  File "/opt/homebrew/bin/gdal_edit.py", line 32, in <module>\n'
    "    from osgeo import gdal\n"
    "ImportError: dlopen(_gdal.cpython-313-darwin.so, 0x0002): "
    "Library not loaded: /opt/homebrew/opt/x265/lib/libx265.216.dylib"
)
GDAL_VERSION = "GDAL 3.3.2, released 2021/09/01"
TESSERACT_VERSION = "tesseract 5.5.3"


def completed(command, code, out="", err=""):
    return subprocess.CompletedProcess(command, code, out, err)


def healthy(command, **_ignored):
    """Stand in for a working toolchain, including gdal_edit.py's odd exit."""
    program = Path(command[0]).name
    if program == "tesseract":
        return completed(command, 0, TESSERACT_VERSION + "\n")
    if program == "gdal_edit.py":
        # It reports the version and still exits 255, exactly as installed.
        return completed(command, 255, GDAL_VERSION + "\n")
    return completed(command, 0, GDAL_VERSION + "\n")


class DoctorRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.arguments = argparse.Namespace(
            database=ROOT / "batch" / "sanborn_batch.sqlite3",
            osm_db=ROOT / "batch" / "osm.sqlite3",
        )
        # Every program is found on PATH. Only running one tells the truth.
        which = mock.patch(
            "shutil.which", side_effect=lambda name: f"/opt/homebrew/bin/{name}"
        )
        which.start()
        self.addCleanup(which.stop)
        # Doctor's existing OpenCV check is not what these tests measure.
        modules = mock.patch.dict(
            sys.modules, {"cv2": types.SimpleNamespace(__version__="4.10.0")}
        )
        modules.start()
        self.addCleanup(modules.stop)

    def run_doctor(self):
        printed = io.StringIO()
        with redirect_stdout(printed):
            code = sanborn_batch.cmd_doctor(self.arguments)
        return code, printed.getvalue()

    def broken(self, program, code, out="", err=""):
        """Run doctor with one program that fails and the rest working."""

        def dispatch(command, **ignored):
            if Path(command[0]).name == program:
                return completed(command, code, out, err)
            return healthy(command, **ignored)

        with mock.patch("subprocess.run", side_effect=dispatch):
            with self.assertRaises(RuntimeError) as raised:
                self.run_doctor()
        return str(raised.exception)

    def test_gdal_on_path_that_aborts_in_the_loader_is_an_error(self):
        message = self.broken("gdal_translate", 133, err=DYLD_FAILURE)
        self.assertIn("gdal_translate", message)
        self.assertIn("/opt/homebrew/bin/gdal_translate", message)
        self.assertIn("cannot run", message)
        self.assertIn("dynamic loader", message)
        # The actual loader text, so the cause is visible without a second hunt.
        self.assertIn("libx265.216.dylib", message)

    def test_gdalwarp_that_exits_nonzero_without_a_loader_hint_is_an_error(self):
        message = self.broken("gdalwarp", 1, err="gdalwarp: unexpected internal failure")
        self.assertIn("gdalwarp", message)
        self.assertIn("exited 1", message)
        self.assertIn("unexpected internal failure", message)

    def test_gdalinfo_that_prints_nothing_at_all_is_still_an_error(self):
        message = self.broken("gdalinfo", 127)
        self.assertIn("gdalinfo", message)
        self.assertIn("(no output)", message)

    def test_gdal_edit_that_cannot_load_its_bindings_is_an_error(self):
        message = self.broken("gdal_edit.py", 1, err=BINDING_FAILURE)
        self.assertIn("gdal_edit.py", message)
        self.assertIn("Library not loaded", message)

    def test_gdal_edit_that_reports_no_version_is_an_error(self):
        message = self.broken("gdal_edit.py", 255, out="Usage: gdal_edit [--help-general]")
        self.assertIn("gdal_edit.py", message)
        self.assertIn("cannot run", message)

    def test_tesseract_on_path_that_cannot_execute_is_an_error(self):
        message = self.broken("tesseract", 133, err=DYLD_FAILURE)
        self.assertIn("tesseract", message)
        self.assertIn("libx265.216.dylib", message)

    def test_a_program_that_cannot_be_started_is_an_error(self):
        def refuse(command, **_ignored):
            if Path(command[0]).name == "gdalinfo":
                raise OSError(13, "Permission denied")
            return healthy(command)

        with mock.patch("subprocess.run", side_effect=refuse):
            with self.assertRaises(RuntimeError) as raised:
                self.run_doctor()
        message = str(raised.exception)
        self.assertIn("gdalinfo", message)
        self.assertIn("Permission denied", message)

    def test_a_program_that_never_answers_is_an_error(self):
        def hang(command, **_ignored):
            if Path(command[0]).name == "gdalwarp":
                raise subprocess.TimeoutExpired(command, sanborn_batch.PROBE_TIMEOUT_SECONDS)
            return healthy(command)

        with mock.patch("subprocess.run", side_effect=hang):
            with self.assertRaises(RuntimeError) as raised:
                self.run_doctor()
        message = str(raised.exception)
        self.assertIn("gdalwarp", message)
        self.assertIn("did not answer", message)

    def test_a_working_toolchain_is_actually_executed_and_reported(self):
        with mock.patch("subprocess.run", side_effect=healthy) as run:
            code, printed = self.run_doctor()
        self.assertEqual(code, 0)
        attempted = [list(call.args[0]) for call in run.call_args_list]
        for program in ("gdal_translate", "gdalwarp", "gdalinfo", "gdal_edit.py", "tesseract"):
            self.assertIn([f"/opt/homebrew/bin/{program}", "--version"], attempted)
        # gdal_edit.py exits 255 by design; reporting the version is what counts.
        self.assertIn(f"gdal_edit.py: /opt/homebrew/bin/gdal_edit.py ({GDAL_VERSION})", printed)
        self.assertIn(f"gdalinfo: /opt/homebrew/bin/gdalinfo ({GDAL_VERSION})", printed)
        self.assertIn(f"tesseract: /opt/homebrew/bin/tesseract ({TESSERACT_VERSION})", printed)
        # Programs without a probe keep the plain name-and-path listing.
        self.assertIn("clang: /opt/homebrew/bin/clang\n", printed)
        self.assertIn("Local worker is ready.", printed)


if __name__ == "__main__":
    unittest.main()
