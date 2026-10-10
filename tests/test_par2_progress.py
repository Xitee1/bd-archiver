"""Live scan line, problem reports and carriage-return subprocess streaming."""

import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bd_archive.shell.runner import run
from bd_archive.ui.logger import log
from bd_archive.ui.par2_progress import CLEAR_LINE, Par2ScanProgress, fit_name

LONG_NAME = "disc_0001/channels/UC0s46sfQCldOw45NTfY79XA/tPbN5XSTOjs/tPbN5XSTOjs.info.json"
# PAR2 prints names longer than 56 characters as 28 + "..." + 28 characters.
PAR2_SHORT_NAME = "disc_0001/channels/UC0s46sfQ...XSTOjs/tPbN5XSTOjs.info.json"


class FitNameTests(unittest.TestCase):
    def test_complete_name_is_kept_or_cut_in_the_middle(self):
        self.assertEqual(fit_name("photos/a.jpg", None, 12), "photos/a.jpg")
        self.assertEqual(fit_name("photos/a.jpg", None, 11), "photo…a.jpg")
        self.assertEqual(fit_name("photos/ab.jpg", None, 10), "photo….jpg")

    def test_par2_shortened_name_joins_or_shrinks_both_ends(self):
        head, tail = PAR2_SHORT_NAME.split("...")
        self.assertEqual(fit_name(head, tail, 57), f"{head}…{tail}")
        self.assertEqual(fit_name(head, tail, 56), f"{head}…{tail[1:]}")
        self.assertEqual(fit_name(head, tail, 21), "disc_0001/….info.json")

    def test_too_narrow_shows_nothing(self):
        self.assertEqual(fit_name("photos/a.jpg", None, 7), "")
        self.assertEqual(fit_name("photos/longer", "name.jpg", 7), "")


class Par2ScanProgressTests(unittest.TestCase):
    COLUMNS = 100

    def progress(self, tty=True, base_dir=None, started=True):
        self.enterContext(patch("sys.stdout.isatty", return_value=tty))
        self.enterContext(
            patch(
                "bd_archive.ui.par2_progress.shutil.get_terminal_size",
                return_value=os.terminal_size((self.COLUMNS, 24)),
            )
        )
        progress = Par2ScanProgress(base_dir)
        if started:
            progress("The total size of the data files is 104857600 bytes.\n")
            with patch("bd_archive.ui.par2_progress.time.monotonic", return_value=100):
                progress("Verifying source files:\n")
        return progress

    def scan(self, progress, record, at):
        with patch("bd_archive.ui.par2_progress.time.monotonic", return_value=at):
            return progress(record)

    def test_average_speed_and_eta_span_multiple_files(self):
        progress = self.progress()
        line = self.scan(progress, "Scanning: 25.0%\r", at=110)
        self.assertEqual(line, f"{CLEAR_LINE}Scanning: 25.0%  |  2.5 MiB/s  |  ETA 0m30s")
        self.assertEqual(progress('Target: "first" - found.\n'), "")
        self.assertEqual(
            progress('Opening: "second"\n'),
            f"{CLEAR_LINE}Scanning: 25.0%  |  2.5 MiB/s  |  ETA 0m30s  |  second",
        )
        line = self.scan(progress, "Scanning: 50.0%\r", at=120)
        self.assertEqual(
            line, f"{CLEAR_LINE}Scanning: 50.0%  |  2.5 MiB/s  |  ETA 0m20s  |  second"
        )

    def test_zero_progress_and_completion(self):
        progress = self.progress()
        self.assertNotIn("ETA", self.scan(progress, "Scanning: 0.0%\r", at=100))
        self.assertIn("ETA 0m00s", self.scan(progress, "Scanning: 100.0%\r", at=120))

    def test_file_name_is_shortened_to_the_terminal_width(self):
        progress = self.progress()
        self.scan(progress, "Scanning: 25.0%\r", at=110)
        line = progress(f'Opening: "{PAR2_SHORT_NAME}"\n')
        # Progress fields take 44 columns; the name fits the remaining width minus one column.
        name = line.removeprefix(f"{CLEAR_LINE}Scanning: 25.0%  |  2.5 MiB/s  |  ETA 0m30s  |  ")
        self.assertEqual(len(line) - len(CLEAR_LINE), self.COLUMNS - 1)
        # 50 columns remain for the name: 25 leading and 24 trailing characters around the cut.
        self.assertTrue(name.startswith("disc_0001/channels/UC0s46"))
        self.assertTrue(name.endswith("Ojs/tPbN5XSTOjs.info.json"))
        self.assertIn("…", name)
        self.assertNotIn("...", name)

    def test_opening_before_the_first_scan_record_prints_nothing(self):
        progress = self.progress()
        self.assertEqual(progress('Opening: "first"\n'), "")
        line = self.scan(progress, "Scanning: 25.0%\r", at=110)
        self.assertTrue(line.endswith("  |  first"))

    def test_problem_files_are_reported_above_the_live_line(self):
        progress = self.progress(base_dir=Path("/mnt/disc"))
        self.scan(progress, "Scanning: 25.0%\r", at=110)
        progress('Opening: "photos/b.jpg"\n')
        live = "Scanning: 25.0%  |  2.5 MiB/s  |  ETA 0m30s  |  photos/b.jpg"
        cases = (
            (
                'Target: "photos/a.jpg" - damaged. Found 3 of 17 data blocks.\n',
                "photos/a.jpg - damaged (found 3 of 17 blocks)",
            ),
            ('Target: "photos/c.jpg" - missing.\n', "photos/c.jpg - missing"),
            ('Target: "photos/d.jpg" - no data found.\n', "photos/d.jpg - no data found"),
            (
                "Could not read 1109 bytes from /mnt/disc/photos/b.jpg at offset 0: "
                "Input/output error\n",
                "photos/b.jpg - unreadable (Input/output error)",
            ),
        )
        for record, problem in cases:
            with self.subTest(record=record):
                self.assertEqual(progress(record), f"{CLEAR_LINE}{log.warn_line(problem)}\n{live}")

    def test_repeated_read_errors_of_one_file_are_reported_once(self):
        progress = self.progress(base_dir=Path("/mnt/disc"))
        error = "at offset 0: Input/output error\n"
        first = f"Could not read 90780077 bytes from /mnt/disc/v.mkv {error}"
        second = f"Could not read 24491896 bytes from /mnt/disc/v.mkv {error}"
        other = f"Could not read 550 bytes from /mnt/disc/w.nfo {error}"
        unreadable = "unreadable (Input/output error)"
        self.assertEqual(progress(first), f"{log.warn_line(f'v.mkv - {unreadable}')}\n")
        self.assertEqual(progress(second), "")
        self.assertEqual(progress(other), f"{log.warn_line(f'w.nfo - {unreadable}')}\n")

    def test_read_error_outside_base_dir_keeps_its_path(self):
        progress = self.progress(base_dir=Path("/mnt/disc"))
        record = "Could not read 550 bytes from /elsewhere/w.nfo at offset 0: Input/output error\n"
        problem = "/elsewhere/w.nfo - unreadable (Input/output error)"
        self.assertEqual(progress(record), f"{log.warn_line(problem)}\n")

    def test_other_output_ends_the_live_line_without_the_file_name(self):
        progress = self.progress()
        self.scan(progress, "Scanning: 25.0%\r", at=110)
        progress('Opening: "second"\n')
        self.assertEqual(progress("\n"), "")
        self.assertEqual(
            progress("Repair is required.\n"),
            f"{CLEAR_LINE}Scanning: 25.0%  |  2.5 MiB/s  |  ETA 0m30s\nRepair is required.\n",
        )
        summary = "1 file(s) exist but are damaged.\n"
        self.assertEqual(progress(summary), summary)
        self.assertEqual(progress.finish(), "")

    def test_finish_ends_the_live_line_once(self):
        progress = self.progress()
        self.assertEqual(progress.finish(), "")
        self.scan(progress, "Scanning: 100.0%\r", at=120)
        progress('Opening: "last"\n')
        self.assertEqual(
            progress.finish(), f"{CLEAR_LINE}Scanning: 100.0%  |  5.0 MiB/s  |  ETA 0m00s\n"
        )
        self.assertEqual(progress.finish(), "")

    def test_extra_scan_shows_progress_without_estimates(self):
        progress = self.progress()
        progress("Scanning extra files:\n")
        self.assertEqual(progress("Scanning: 10.0%\r"), f"{CLEAR_LINE}Scanning: 10.0%")

    def test_unknown_output_and_missing_size_are_preserved(self):
        progress = self.progress(started=False)
        for line in ("Loading: 50.0%\r", "Verifying source files:\n"):
            self.assertEqual(progress(line), line)
        self.assertEqual(progress("Scanning: 10.0%\r"), f"{CLEAR_LINE}Scanning: 10.0%")

    def test_logs_are_throttled_without_terminal_escape_codes(self):
        progress = self.progress(tty=False)
        self.assertIn("ETA", self.scan(progress, "Scanning: 25.0%\r", at=110))
        self.assertEqual(self.scan(progress, "Scanning: 26.0%\r", at=110), "")
        self.assertEqual(progress('Opening: "second"\n'), "")
        self.assertEqual(progress('Target: "first" - found.\n'), "")
        self.assertEqual(progress('Target: "second" - missing.\n'), "[WARN]  second - missing\n")
        line = self.scan(progress, "Scanning: 100.0%\r", at=120)
        self.assertEqual(line, "Scanning: 100.0%  |  5.0 MiB/s  |  ETA 0m00s\n")
        self.assertEqual(progress.finish(), "")

    def test_streams_carriage_returns_before_child_exits(self):
        # The child waits for an acknowledgement from the transform. A reader
        # that waits for a newline or EOF would deadlock until the timeout.
        import tempfile

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
