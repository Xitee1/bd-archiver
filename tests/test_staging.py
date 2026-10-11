import errno
import hashlib
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bd_archive.archive.content_dates import ContentDate
from bd_archive.archive.plan import grouping, make_units
from bd_archive.archive.raw import scan_raw_source
from bd_archive.archive.staging import StageItem, check_space, remove_units, stage


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.disc = self.root / "disc"
        self.disc.mkdir()

    def file(self, path, data=b"payload"):
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def units(self, rule="depth:1"):
        inventory = scan_raw_source(self.source)
        dates = {
            e.path: ContentDate(e.mtime_ns, "mtime") for e in inventory if stat.S_ISREG(e.mode)
        }
        return make_units(inventory, grouping(rule), dates, "name")

    def items(self, units, *, link=True):
        return [
            StageItem(self.source / e.path, f"Test/{e.path}", e, link)
            for unit in units
            for e in unit.entries
        ]

    def test_hardlinks_units_and_keeps_directory_metadata_without_changing_source(self):
        movie = self.file("video/movie")
        (self.source / "video/empty").mkdir()
        os.utime(movie, ns=(100, 100))
        os.utime(self.source / "video", ns=(200, 200))
        before = scan_raw_source(self.source)
        with patch("bd_archive.archive.staging.reflink.try_clone", return_value=False):
            digests, totals = stage(self.items(self.units()), self.disc)
        target = self.disc / "Test/video/movie"
        self.assertEqual(target.stat().st_ino, movie.stat().st_ino)
        self.assertEqual(target.stat().st_mtime_ns, 100)
        self.assertEqual((self.disc / "Test/video").stat().st_mtime_ns, 200)
        self.assertTrue((self.disc / "Test/video/empty").is_dir())
        self.assertEqual(totals, {"reflinked": 0, "hardlinked": 7, "copied": 0})
        self.assertEqual(digests, {})
        after = scan_raw_source(self.source)
        # Only the link count changes the inode's ctime.
        self.assertEqual(
            [replace(e, ctime_ns=0) for e in after], [replace(e, ctime_ns=0) for e in before]
        )

    def test_copies_with_verification_when_links_are_unavailable(self):
        movie = self.file("video/movie", b"x" * 5000)
        os.utime(movie, ns=(100, 100))
        for link, error in ((False, None), (True, errno.EXDEV), (True, errno.EPERM)):
            target = self.root / f"disc-{link}-{error}"
            target.mkdir()
            with (
                self.subTest(link=link, error=error),
                patch("bd_archive.archive.staging.reflink.try_clone", return_value=False),
                patch(
                    "bd_archive.archive.staging.os.link",
                    side_effect=OSError(error, "no links"),
                ),
            ):
                digests, totals = stage(self.items(self.units(), link=link), target)
            copied = target / "Test/video/movie"
            self.assertNotEqual(copied.stat().st_ino, movie.stat().st_ino)
            self.assertEqual(copied.read_bytes(), b"x" * 5000)
            self.assertEqual(copied.stat().st_mtime_ns, 100)
            self.assertEqual(digests, {"Test/video/movie": hashlib.sha512(b"x" * 5000).hexdigest()})
            self.assertEqual(totals["copied"], 5000)

    def test_unexpected_link_errors_propagate(self):
        self.file("movie")
        with (
            patch("bd_archive.archive.staging.reflink.try_clone", return_value=False),
            patch("bd_archive.archive.staging.os.link", side_effect=OSError(errno.EIO, "I/O")),
            self.assertRaises(OSError),
        ):
            stage(self.items(self.units()), self.disc)

    def test_changed_source_is_never_staged(self):
        self.file("movie")
        items = self.items(self.units())
        self.file("movie", b"changed content")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            stage(items, self.disc)
        self.assertFalse((self.disc / "Test/movie").exists())

    def test_space_check_counts_only_files_that_may_need_a_copy(self):
        self.file("file", b"x" * 8192)
        items = self.items(self.units())
        empty_fs = SimpleNamespace(f_frsize=4096, f_bsize=4096, f_bavail=0)
        with patch("bd_archive.archive.staging.os.statvfs", return_value=empty_fs):
            check_space(self.disc / "new", items)
            for changed in (
                [replace(item, link=False) for item in items],
                [replace(item, entry=replace(item.entry, device=-1)) for item in items],
            ):
                with (
                    self.subTest(changed=changed),
                    self.assertRaisesRegex(ValueError, "need.*available"),
                ):
                    check_space(self.disc / "new", changed)

    def test_remove_units_removes_files_and_empty_ancestors_only(self):
        self.file("channel/first/movie")
        self.file("channel/second/movie")
        self.file("other/movie")
        units = self.units("depth:2")
        selected = [u for u in units if u.path == "channel/first"]
        with patch("bd_archive.archive.staging.reflink.try_clone", return_value=False):
            stage(self.items(selected), self.disc)
        remove_units(self.source, selected, ["channel"])
        self.assertFalse((self.source / "channel/first").exists())
        self.assertTrue((self.source / "channel/second/movie").exists())
        self.assertTrue((self.source / "other/movie").exists())
        self.assertEqual((self.disc / "Test/channel/first/movie").read_bytes(), b"payload")
        # An emptied ancestor is removed, the source itself never.
        remove_units(self.source, [u for u in units if u.path == "channel/second"], ["channel"])
        self.assertFalse((self.source / "channel").exists())
        self.assertTrue(self.source.is_dir())

    def test_remove_units_checks_every_unit_before_removing_anything(self):
        self.file("first/movie")
        self.file("second/movie")
        units = self.units()
        for change in ("addition", "modification"):
            with self.subTest(change=change):
                if change == "addition":
                    self.file("second/new")
                else:
                    self.file("second/movie", b"modified")
                with self.assertRaisesRegex(ValueError, "nothing was removed"):
                    remove_units(self.source, units, [])
                self.assertTrue((self.source / "first/movie").exists())
                units = self.units()


if __name__ == "__main__":
    unittest.main()
