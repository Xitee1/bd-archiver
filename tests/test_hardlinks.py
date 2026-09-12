"""Only repeated hardlink names in the archive selection must stop creation."""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_archive.archive.hardlinks import HardlinkTracker
from bd_archive.archive.raw import scan_raw_source
from bd_archive.archive.source_scan import scan_source
from bd_archive.cli import main
from bd_archive.tools.reflink import try_clone


class HardlinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.first = self.source / "first"
        self.first.write_bytes(b"payload")
        (self.source / "nested").mkdir()
        self.second = self.source / "nested/second"

    def check_both_scans(self):
        scan_raw_source(self.source)
        scan_source(self.source).hardlinks.check()

    def test_internal_hardlinks_report_both_relative_paths(self):
        os.link(self.first, self.second)
        for scan in (
            lambda: scan_raw_source(self.source),
            lambda: scan_source(self.source).hardlinks.check(),
        ):
            with self.subTest(scan=scan), self.assertRaises(ValueError) as exc:
                scan()
            self.assertIn("'first'", str(exc.exception))
            self.assertIn("'nested/second'", str(exc.exception))
            self.assertIn("independent copies", str(exc.exception))

    def test_external_hardlinks_are_allowed(self):
        os.link(self.first, self.root / "outside")
        os.link(self.first, self.root / "another-outside")
        self.assertEqual(self.first.stat().st_nlink, 3)
        self.check_both_scans()

    def test_independent_copies_with_identical_content_are_allowed(self):
        shutil.copy2(self.first, self.second)
        self.check_both_scans()

    def test_empty_hardlinked_files_are_rejected(self):
        self.first.write_bytes(b"")
        os.link(self.first, self.second)
        with self.assertRaisesRegex(ValueError, "Hardlinks"):
            scan_raw_source(self.source)

    def test_identity_includes_device(self):
        tracker = HardlinkTracker()
        tracker.add("first", 1, 42)
        tracker.add("different-filesystem", 2, 42)
        tracker.check()

    def test_exclusions_only_allow_a_single_remaining_name(self):
        os.link(self.first, self.second)
        os.link(self.first, self.source / "third")
        tracker = scan_source(self.source).hardlinks
        with self.assertRaisesRegex(ValueError, "Hardlinks"):
            tracker.check({"third"})
        tracker.check({"third", "nested/second"})
        tracker.check({"first", "third", "nested/second"})

    def test_reflinks_are_allowed_when_supported(self):
        # The checkout may support reflinks even when /tmp is a tmpfs.
        with tempfile.TemporaryDirectory(prefix=".hardlink-test-", dir=Path.cwd()) as tmp:
            source = Path(tmp)
            first = source / "first"
            first.write_bytes(b"payload")
            if not try_clone(first, source / "second"):
                self.skipTest("checkout filesystem does not support reflinks")
            scan_raw_source(source)
            scan_source(source).hardlinks.check()

    def args(self, mode, iso=False):
        return [
            "create",
            "-m",
            mode,
            "-s",
            str(self.source),
            "-n",
            "Test",
            "-o",
            str(self.root / "output"),
            "-b",
            str(16 * 1024 * 1024),
            "-r",
            "0",
            "-c",
            "none",
            *(["--iso"] if iso else []),
        ]

    def test_all_creation_modes_reject_before_generating_output(self):
        os.link(self.first, self.second)
        for mode in ("raw", "dar"):
            for iso in (False, True):
                output = io.StringIO()
                with (
                    self.subTest(mode=mode, iso=iso),
                    patch("sys.argv", ["bd-archive", *self.args(mode, iso)]),
                    patch("bd_archive.commands.create.check_deps"),
                    patch("bd_archive.commands.create_raw.check_deps"),
                    patch("bd_archive.tools.mkisofs.estimate_size") as measure,
                    patch("bd_archive.commands.create_raw.write_raw_checksums") as hashes,
                    patch("bd_archive.commands.create.DarArchive.create") as dar,
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(output),
                    self.assertRaises(SystemExit) as exc,
                ):
                    main()
                self.assertEqual(exc.exception.code, 1)
                self.assertIn("Hardlinks within the selected source", output.getvalue())
                self.assertFalse((self.root / "output").exists())
                measure.assert_not_called()
                hashes.assert_not_called()
                dar.assert_not_called()

    def test_dar_deferral_excludes_a_hardlink_before_validation(self):
        with self.first.open("wb") as out:
            out.truncate(6 * 1024 * 1024)
        os.link(self.first, self.second)
        output = io.StringIO()
        with (
            patch("bd_archive.commands.create.check_deps"),
            patch("bd_archive.commands.create.prompt_yn", return_value=False) as confirm,
            patch("sys.argv", ["bd-archive", *self.args("dar"), "--min-last-disc-fill", "60"]),
            patch("bd_archive.commands.create.DarArchive.create") as dar,
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as exc,
        ):
            main()
        self.assertEqual(exc.exception.code, 0, output.getvalue())
        self.assertIn("Files deferred:   1", output.getvalue())
        confirm.assert_called_once()
        dar.assert_not_called()
