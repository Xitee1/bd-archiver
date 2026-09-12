"""Exercise clone fallback, publication and real copy-on-write behavior."""

import errno
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_archive.archive.disc_folder import prepare_folder
from bd_archive.tools import reflink


class ReflinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bd-reflink-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.write_bytes(b"original contents")
        self.source.chmod(0o640)
        os.utime(self.source, ns=(1_600_000_000_000_000_000,) * 2)
        self.target = self.root / "target"

    @staticmethod
    def clone_bytes(destination_fd, request, source_fd):
        # Emulate the kernel operation on the supplied file descriptors.
        os.write(destination_fd, os.read(source_fd, 1024))

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.root.glob(".bd-reflink-*")), [])

    def test_clone_publishes_complete_data_and_preserves_metadata(self):
        with patch("bd_archive.tools.reflink.fcntl.ioctl", side_effect=self.clone_bytes):
            self.assertTrue(reflink.try_clone(self.source, self.target))
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o640)
        self.assertEqual(self.target.stat().st_mtime_ns, self.source.stat().st_mtime_ns)
        self.assertNotEqual(self.source.stat().st_ino, self.target.stat().st_ino)
        self.assert_no_temporary_files()

    def test_unsupported_clone_leaves_destination_and_source_untouched(self):
        self.target.write_bytes(b"existing destination")
        for code in (errno.EXDEV, errno.EOPNOTSUPP, errno.ENOTTY, errno.EINVAL, errno.ENOSYS):
            with (
                self.subTest(errno=code),
                patch(
                    "bd_archive.tools.reflink.fcntl.ioctl", side_effect=OSError(code, "unsupported")
                ),
            ):
                self.assertFalse(reflink.try_clone(self.source, self.target))
            self.assertEqual(self.target.read_bytes(), b"existing destination")
            self.assertEqual(self.source.read_bytes(), b"original contents")
            self.assert_no_temporary_files()

    def test_storage_permission_and_io_errors_are_not_hidden(self):
        for code in (errno.EIO, errno.ENOSPC, errno.EACCES, errno.EPERM):
            with (
                self.subTest(errno=code),
                patch("bd_archive.tools.reflink.fcntl.ioctl", side_effect=OSError(code, "failure")),
                self.assertRaises(OSError) as raised,
            ):
                reflink.try_clone(self.source, self.target)
            self.assertEqual(raised.exception.errno, code)
            self.assertFalse(self.target.exists())
            self.assert_no_temporary_files()

    def test_metadata_failure_and_cancellation_do_not_publish_partial_clones(self):
        for exception in (OSError(errno.EIO, "metadata failure"), KeyboardInterrupt()):
            with (
                self.subTest(exception=type(exception).__name__),
                patch("bd_archive.tools.reflink.fcntl.ioctl", side_effect=self.clone_bytes),
                patch("bd_archive.tools.reflink.shutil.copystat", side_effect=exception),
                self.assertRaises(type(exception)),
            ):
                reflink.try_clone(self.source, self.target)
            self.assertFalse(self.target.exists())
            self.assert_no_temporary_files()

    def test_cancellation_during_clone_cleans_up(self):
        with (
            patch("bd_archive.tools.reflink.fcntl.ioctl", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            reflink.try_clone(self.source, self.target)
        self.assertFalse(self.target.exists())
        self.assert_no_temporary_files()

    def test_folder_falls_back_to_progress_copy_and_reports_counts(self):
        from bd_archive.ui.progress import copy_with_progress

        with (
            patch(
                "bd_archive.tools.reflink.fcntl.ioctl", side_effect=OSError(errno.EXDEV, "cross fs")
            ),
            patch(
                "bd_archive.archive.disc_folder.copy_with_progress", wraps=copy_with_progress
            ) as copy,
            patch("bd_archive.tools.mkisofs.estimate_size", return_value=100),
            patch("bd_archive.archive.disc_folder.log.info") as info,
        ):
            folder = prepare_folder(
                self.root / "disc", [("payload", self.source)], "Test", "test", 100
            )
        copy.assert_called_once_with(self.source, folder.root / "payload")
        self.assertEqual((folder.root / "payload").read_bytes(), b"original contents")
        self.assertEqual(
            (folder.root / "payload").stat().st_mtime_ns, self.source.stat().st_mtime_ns
        )
        info.assert_called_once_with("File transfer: reflinked 0 B; copied 17 B; moved 0 B")
        self.assertEqual(list(folder.root.iterdir()), [folder.root / "payload"])

    def test_folder_counts_clones_copies_and_moves_separately(self):
        copied = self.root / "copied"
        copied.write_bytes(b"copy")
        moved = self.root / "moved"
        moved.write_bytes(b"move me")
        move_inode = moved.stat().st_ino

        def clone(source, destination):
            if source == self.source:
                shutil.copy2(source, destination)
                return True
            return False

        with (
            patch(
                "bd_archive.archive.disc_folder.reflink.try_clone", side_effect=clone
            ) as clone_mock,
            patch("bd_archive.tools.mkisofs.estimate_size", return_value=100),
            patch("bd_archive.archive.disc_folder.log.info") as info,
        ):
            folder = prepare_folder(
                self.root / "disc",
                [("cloned", self.source), ("copied", copied), ("moved", moved)],
                "Test",
                "test",
                100,
                move_sources={moved},
            )
        self.assertEqual(clone_mock.call_count, 2)
        self.assertEqual((folder.root / "moved").stat().st_ino, move_inode)
        self.assertFalse(moved.exists())
        self.assertEqual((folder.root / "cloned").read_bytes(), b"original contents")
        self.assertEqual((folder.root / "copied").read_bytes(), b"copy")
        info.assert_called_once_with("File transfer: reflinked 17 B; copied 4 B; moved 7 B")

    def test_folder_does_not_fallback_or_publish_manifest_on_io_error(self):
        with (
            patch(
                "bd_archive.tools.reflink.fcntl.ioctl", side_effect=OSError(errno.EIO, "failure")
            ),
            patch("bd_archive.archive.disc_folder.copy_with_progress") as copy,
            self.assertRaises(OSError),
        ):
            prepare_folder(self.root / "disc", [("payload", self.source)], "Test", "test", 100)
        copy.assert_not_called()
        self.assertEqual(list((self.root / "disc").iterdir()), [])


class ReflinkFilesystemTests(unittest.TestCase):
    def setUp(self):
        # /tmp may be tmpfs even when the project is on Btrfs. Keep this small
        # integration fixture on the project's filesystem to exercise FICLONE.
        self.tmp = tempfile.TemporaryDirectory(
            prefix=".bd-reflink-test-", dir=Path(__file__).resolve().parents[1]
        )
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "payload"
        self.source.write_bytes(b"A" * (1024 * 1024))
        self.source.chmod(0o640)
        os.utime(self.source, ns=(1_600_000_000_000_000_000,) * 2)
        if not reflink.try_clone(self.source, self.root / "probe"):
            self.skipTest("Project filesystem does not support reflinks")

    def cli(self, *args):
        result = subprocess.run(
            [sys.executable, "-m", "bd_archive", *map(str, args)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    @unittest.skipUnless(shutil.which("mkisofs"), "requires mkisofs")
    def test_raw_create_reflinks_source_and_verifies_after_original_changes(self):
        source = self.root / "source tree"
        source.mkdir()
        shutil.copy2(self.source, source / "payload")
        (source / "empty").touch()
        (source / "empty directory").mkdir()
        output = self.root / "output"
        messages = self.cli(
            "create", "-s", source, "-n", "Test", "-o", output, "-b", "10000000", "-r", "0", "-y"
        )
        self.assertIn("reflinked 1.0 MiB; copied 0 B", messages)
        (source / "payload").write_bytes(b"changed original")
        disc = output / "discs/disc_0001"
        self.cli("verify", disc)
        self.assertEqual((disc / source.name / "payload").read_bytes(), b"A" * (1024 * 1024))
        self.assertTrue((disc / source.name / "empty directory").is_dir())
        self.assertEqual((disc / source.name / "empty").stat().st_size, 0)

    @unittest.skipUnless(
        all(shutil.which(tool) for tool in ("dar", "mkisofs", "dvd+rw-mediainfo")),
        "requires dar, mkisofs and dvd+rw-mediainfo",
    )
    def test_packing_reflinks_old_archive_and_survives_changes_to_original(self):
        source = self.root / "source tree"
        source.mkdir()
        shutil.copy2(self.source, source / "payload")
        first = self.root / "first"
        options = [
            "create",
            "-m",
            "dar",
            "-s",
            source,
            "-n",
            "Test",
            "-b",
            "10000000",
            "-c",
            "none",
            "-r",
            "0",
            "-y",
        ]
        self.cli(*options, "-o", first)
        old_disc = first / "discs/disc_0001"
        old_slice = old_disc / "Test-gen1/Test-gen1.0001.dar"
        original = old_slice.read_bytes()
        (source / "new file").write_bytes(b"new generation")
        second = self.root / "second"
        messages = self.cli(
            *options,
            "-o",
            second,
            "--base",
            first / "Test-gen1-catalog.0001.dar",
            "--pack-with",
            old_disc,
        )
        self.assertIn("reflinked 1.0 MiB; copied 0 B", messages)
        packed_disc = second / "discs/disc_0001"
        packed_slice = packed_disc / "Test-gen1/Test-gen1.0001.dar"
        self.assertNotEqual(old_slice.stat().st_ino, packed_slice.stat().st_ino)
        old_slice.write_bytes(b"original changed after packing")
        self.assertEqual(packed_slice.read_bytes(), original)
        self.cli("verify", packed_disc)

    def test_real_clone_has_independent_content_and_metadata(self):
        target = self.root / "clone"
        self.assertTrue(reflink.try_clone(self.source, target))
        self.assertNotEqual(target.stat().st_ino, self.source.stat().st_ino)
        self.assertEqual(target.stat().st_mode, self.source.stat().st_mode)
        self.assertEqual(target.stat().st_mtime_ns, self.source.stat().st_mtime_ns)
        with self.source.open("r+b") as source:
            source.write(b"source changed")
        self.assertEqual(target.read_bytes(), b"A" * (1024 * 1024))
        with target.open("r+b") as clone:
            clone.seek(512)
            clone.write(b"clone changed")
        with self.source.open("rb") as source:
            source.seek(512)
            self.assertEqual(source.read(13), b"A" * 13)
        target.chmod(0o600)
        self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o640)

    def test_real_folder_preparation_uses_clone_without_copying_payload(self):
        with (
            patch("bd_archive.archive.disc_folder.copy_with_progress") as copy,
            patch("bd_archive.tools.mkisofs.estimate_size", return_value=2_000_000),
            patch("bd_archive.archive.disc_folder.log.info") as info,
        ):
            folder = prepare_folder(
                self.root / "disc", [("payload", self.source)], "Test", "test", 2_000_000
            )
        copy.assert_not_called()
        self.assertIn("reflinked 1.0 MiB; copied 0 B; moved 0 B", info.call_args.args[0])
        self.source.write_bytes(b"changed source")
        folder.check_unchanged()
        self.assertEqual((folder.root / "payload").read_bytes(), b"A" * (1024 * 1024))
