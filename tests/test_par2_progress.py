"""Scan estimates and carriage-return subprocess streaming."""

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from bd_archive.shell.runner import run
from bd_archive.ui.par2_progress import Par2ScanProgress


class Par2ScanProgressTests(unittest.TestCase):
    def progress(self, tty=True):
        with patch("sys.stdout.isatty", return_value=tty):
            progress = Par2ScanProgress()
        progress("The total size of the data files is 104857600 bytes.\n")
        with patch("bd_archive.ui.par2_progress.time.monotonic", return_value=100):
            progress("Verifying source files:\n")
        return progress

    def test_average_speed_and_eta_span_multiple_files(self):
        progress = self.progress()
        with patch("bd_archive.ui.par2_progress.time.monotonic", return_value=110):
            line = progress("Scanning: 25.0%\r")
        self.assertIn("2.5 MiB/s", line)
        self.assertIn("ETA 0m30s", line)
        self.assertTrue(line.startswith("\r\033[K"))
        self.assertEqual(progress('Target: "first" - found.\n'), '\nTarget: "first" - found.\n')
        progress('Opening: "second"\n')
        with patch("bd_archive.ui.par2_progress.time.monotonic", return_value=120):
            line = progress("Scanning: 50.0%\r")
        self.assertIn("2.5 MiB/s", line)
        self.assertIn("ETA 0m20s", line)

    def test_zero_progress_and_completion(self):
        progress = self.progress()
        with patch("bd_archive.ui.par2_progress.time.monotonic", return_value=100):
            self.assertNotIn("ETA", progress("Scanning: 0.0%\r"))
        with patch("bd_archive.ui.par2_progress.time.monotonic", return_value=120):
            self.assertIn("ETA 0m00s", progress("Scanning: 100.0%\r"))

    def test_extra_scan_does_not_reuse_source_size(self):
        progress = self.progress()
        progress("Scanning extra files:\n")
        self.assertEqual(progress("Scanning: 10.0%\r"), "Scanning: 10.0%\r")

    def test_unknown_output_and_missing_size_are_preserved(self):
        progress = Par2ScanProgress()
        for line in ("Loading: 50.0%\r", "Verifying source files:\n", "Scanning: 10.0%\r"):
            self.assertEqual(progress(line), line)

    def test_logs_are_throttled_without_terminal_escape_codes(self):
        progress = self.progress(tty=False)
        with patch("bd_archive.ui.par2_progress.time.monotonic", return_value=110):
            self.assertIn("ETA", progress("Scanning: 25.0%\r"))
            self.assertEqual(progress("Scanning: 26.0%\r"), "")
        with patch("bd_archive.ui.par2_progress.time.monotonic", return_value=120):
            line = progress("Scanning: 100.0%\r")
        self.assertTrue(line.endswith("\n"))
        self.assertNotIn("\033", line)
        self.assertNotIn("\r", line)

    def test_streams_carriage_returns_before_child_exits(self):
        # The child waits for an acknowledgement from the transform. A reader
        # that waits for a newline or EOF would deadlock until the timeout.
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            ack = Path(directory) / "ack"
            script = (
                "import pathlib, sys, time\n"
                "sys.stdout.write('Scanning: 25.0%\\rScanning: '); sys.stdout.flush()\n"
                "deadline = time.monotonic() + 5\n"
                "while not pathlib.Path(sys.argv[1]).exists():\n"
                "    if time.monotonic() > deadline: sys.exit(9)\n"
                "    time.sleep(0.01)\n"
                "sys.stdout.write('100.0%\\rDone\\n'); sys.stdout.flush()\n"
                "sys.exit(1)\n"
            )
            records = []

            def transform(record):
                records.append(record)
                ack.touch()
                return record

            with redirect_stdout(io.StringIO()):
                result = run(
                    [sys.executable, "-c", script, str(ack)],
                    check=False,
                    output_transform=transform,
                )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(records, ["Scanning: 25.0%\r", "Scanning: 100.0%\r", "Done\n"])

    def test_transform_retains_error_handling(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(subprocess.CalledProcessError):
            run([sys.executable, "-c", "raise SystemExit(2)"], output_transform=lambda line: line)
