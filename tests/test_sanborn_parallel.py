#!/usr/bin/env python3

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from unittest import mock  # noqa: E402

import sanborn_parallel  # noqa: E402
from sanborn_parallel import LOC_PAUSE_EXIT, LaunchPacer, batch_command, parse_args, run_tile  # noqa: E402


class SanbornParallelTests(unittest.TestCase):
    def test_defaults_use_bounded_local_concurrency(self):
        args = parse_args(["154-160"])
        self.assertEqual(args.jobs, 3)
        self.assertEqual(args.delay, 2.0)

    def test_each_child_runs_one_resumable_work_item(self):
        command = batch_command(
            Path("/tmp/queue.sqlite3"),
            154,
            1.5,
            Path("/tmp/osm.sqlite3"),
            Path("/tmp/index.json"),
            Path("/tmp/aliases.json"),
        )
        self.assertIn("work", command)
        self.assertEqual(command[command.index("work") + 1], "154")
        self.assertIn("--index-json", command)
        self.assertNotIn("approve", command)
        self.assertNotIn("finish", command)

    def test_shared_pacer_spaces_actual_worker_launches(self):
        now = [100.0]
        launches = []

        def clock():
            return now[0]

        def sleep(seconds):
            self.assertGreater(seconds, 0)
            now[0] += seconds

        pacer = LaunchPacer(1.5, clock=clock, sleeper=sleep)
        for _ in range(3):
            pacer.wait()
            launches.append(now[0])

        self.assertEqual(launches, [100.0, 101.5, 103.0])


class LocPauseTests(unittest.TestCase):
    def test_a_loc_pause_stops_new_tiles_from_starting(self):
        import subprocess
        import tempfile

        pacer = LaunchPacer(0.0)
        paused = subprocess.CompletedProcess([], LOC_PAUSE_EXIT, "", "PAUSED")
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            sanborn_parallel.subprocess, "run", return_value=paused
        ) as run:
            first = run_tile(Path("q"), 154, 0, Path("o"), Path("i"), Path("a"), Path(folder), pacer)
            second = run_tile(Path("q"), 155, 0, Path("o"), Path("i"), Path("a"), Path(folder), pacer)
        self.assertEqual(first["returncode"], LOC_PAUSE_EXIT)
        self.assertTrue(pacer.paused.is_set())
        self.assertTrue(second.get("skipped"))
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
