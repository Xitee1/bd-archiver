import contextlib
import errno
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bd_archive.archive.prepare import (
    Unit,
    chronology,
    free_limit,
    grouping,
    make_units,
    proposals,
)
from bd_archive.archive.prepare_move import (
    check_space,
    move_plan,
    move_unit,
    rename_exclusive,
)
from bd_archive.archive.prepare_sizing import Measurement, measure
from bd_archive.archive.raw import scan_raw_source
from bd_archive.cli import build_parser
from bd_archive.commands.prepare import checked_proposals, cmd_prepare
from bd_archive.constants import MiB


def toy_units(sizes):
    return [Unit(str(i), (), size, i * 86400 * 10**9, 1, size) for i, size in enumerate(sizes)]


class PlannerTests(unittest.TestCase):
    def test_units_are_atomic_and_every_selected_unit_occurs_once(self):
        units = toy_units([14, 14, 11, 11, 13, 12])
        plans = proposals(units, 25, True, False)
        self.assertEqual(len(plans[0]), 3)
        self.assertTrue(any(len(p) == 4 and chronology(p, units)[0] == 0 for p in plans))
        for plan in plans:
            self.assertEqual(sorted(i for group in plan for i in group), list(range(6)))
            self.assertTrue(all(sum(units[i].size for i in group) <= 25 for group in plan))
        self.assertEqual(plans, proposals(units, 25, True, False))

    def test_same_disc_count_prefers_chronology(self):
        units = toy_units([12, 12, 14, 11])
        main = proposals(units, 25, True, False)[0]
        self.assertEqual(main, ((0, 1), (2, 3)))
        self.assertEqual(chronology(main, units)[0], 0)

    def test_deferred_units_are_always_a_newest_suffix(self):
        units = toy_units([14, 14, 11, 11, 13, 12])
        for plan in proposals(units, 25, True, True):
            selected = sorted(i for g in plan for i in g)
            self.assertEqual(selected, list(range(len(selected))))

    def test_small_and_large_overlaps_both_contribute(self):
        units = toy_units([1] * 5)
        mild = chronology(((0, 2), (1, 3, 4)), units)
        severe = chronology(((0, 4), (1, 2, 3)), units)
        self.assertGreater(severe[0], mild[0])
        self.assertGreater(severe[1], mild[1])
        self.assertGreater(severe[2], mild[2])

    def test_file_limit_and_oversized_indivisible_unit(self):
        units = [Unit(str(i), (), 1, i, 20000, 1) for i in range(3)]
        self.assertEqual(len(proposals(units, 25, True, False)[0]), 3)
        self.assertEqual(len(proposals(units, 25, False, False)[0]), 1)
        with self.assertRaisesRegex(ValueError, "Unit cannot fit"):
            proposals(toy_units([26]), 25, True, False)

    def test_limit_parser(self):
        self.assertTrue(free_limit("5").allows(5, 100))
        self.assertFalse(free_limit("5").allows(6, 100))
        self.assertTrue(free_limit("0").allows(0, 100))
        self.assertTrue(free_limit("1.5G").allows(1_500_000_000, 100))
        self.assertFalse(free_limit("500M").allows(500_000_001, 10**10))
        for value in ("-1", "101", "nan", "5MiB", "inf"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                free_limit(value)

    def test_actual_measurement_controls_fill_and_alternatives(self):
        units = toy_units([14, 14, 11, 11, 13, 12])
        candidates = proposals(units, 25, False, True)
        with patch(
            "bd_archive.commands.prepare.measure",
            side_effect=lambda g, u, c, r: Measurement(
                sum(u[i].size for i in g), sum(u[i].size for i in g)
            ),
        ):
            choices, _ = checked_proposals(units, candidates, 25, 0, free_limit("5"))
        self.assertEqual(sum(len(g) for g in choices[0]), 6)
        self.assertTrue(any(sum(len(g) for g in p) == 5 for p in choices))
        self.assertTrue(any(len(p) == 2 for p in choices))


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"

    def file(self, path, data=b"payload"):
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def units(self, rule="top-level"):
        return make_units(scan_raw_source(self.source), grouping(rule))

    def test_grouping_depth_files_and_empty_directories(self):
        self.file("channel/video/movie.mkv")
        self.file("channel/video/subtitles.srt")
        self.file("channel/loose.jpg")
        self.file("root.jpg")
        (self.source / "empty").mkdir()
        self.assertEqual({u.path for u in self.units()}, {"channel", "root.jpg", "empty"})
        self.assertEqual(
            {u.path for u in self.units("depth:2")},
            {"channel/video", "channel/loose.jpg", "root.jpg", "empty"},
        )
        self.assertEqual(len(self.units("files")), 5)

    def test_folder_date_uses_latest_file_not_directory_mtime(self):
        old = self.file("video/movie")
        newer = self.file("video/sidecar")
        os.utime(old, ns=(100, 100))
        os.utime(newer, ns=(200, 200))
        os.utime(old.parent, ns=(999999, 999999))
        self.assertEqual(self.units()[0].date, 200)

    def test_moves_only_selected_units_preserves_paths_and_metadata(self):
        first = self.file("channel/first/movie")
        self.file("channel/second/movie")
        os.utime(first, ns=(100, 100))
        units = self.units("depth:2")
        inode = first.stat().st_ino
        move_plan(self.source, self.output, units, ((0,),))
        target = self.output / "disc_0001/channel/first/movie"
        self.assertEqual(target.read_bytes(), b"payload")
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(target.stat().st_mtime_ns, 100)
        self.assertFalse(first.exists())
        self.assertTrue((self.source / "channel/second/movie").exists())
        self.assertEqual([p.name for p in self.output.iterdir()], ["disc_0001"])

    def test_new_independent_units_are_left_for_next_session(self):
        self.file("first/file")
        units = self.units()
        self.file("new/file")
        move_plan(self.source, self.output, units, ((0,),))
        self.assertTrue((self.source / "new/file").exists())

    def test_changed_selected_unit_aborts_before_first_move(self):
        self.file("first/file")
        self.file("second/file")
        units = self.units()
        self.file("second/addition")
        with self.assertRaisesRegex(ValueError, "changed"):
            move_plan(self.source, self.output, units, ((0, 1),))
        self.assertFalse(self.output.exists())
        self.assertTrue((self.source / "first/file").exists())

    def test_exclusive_rename_does_not_replace_existing_file_or_directory(self):
        original = self.file("file")
        target = self.root / "existing"
        target.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            rename_exclusive(original, target)
        self.assertEqual(target.read_bytes(), b"keep")
        self.assertTrue(original.exists())

    def cross_device(self, source, target):
        if source.is_relative_to(self.source):
            raise OSError(errno.EXDEV, "cross-device test")
        return rename_exclusive(source, target)

    def test_cross_device_move_verifies_copy_then_removes_original(self):
        original = self.file("video/movie")
        self.file("video/sidecar")
        (self.source / "video/empty").mkdir()
        unit = self.units()[0]
        target = self.root / "moved"
        with patch(
            "bd_archive.archive.prepare_move.rename_exclusive", side_effect=self.cross_device
        ):
            move_unit(self.source, unit, target)
        self.assertEqual((target / "movie").read_bytes(), b"payload")
        self.assertTrue((target / "empty").is_dir())
        self.assertFalse(original.parent.exists())

    def test_failed_copy_keeps_original_and_no_published_destination(self):
        original = self.file("file")
        unit = self.units()[0]
        target = self.root / "moved"
        with (
            patch(
                "bd_archive.archive.prepare_move.rename_exclusive", side_effect=self.cross_device
            ),
            patch(
                "bd_archive.archive.prepare_move.copy_verified", side_effect=OSError("disk full")
            ),
            self.assertRaises(OSError),
        ):
            move_unit(self.source, unit, target)
        self.assertEqual(original.read_bytes(), b"payload")
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob(".bd-prepare-*")))

    def test_source_change_during_copy_never_removes_original(self):
        from bd_archive.archive.prepare_move import copy_verified

        original = self.file("video/file")
        unit = self.units()[0]

        def change_after_copy(src, dst, entry):
            copy_verified(src, dst, entry)
            self.file("video/new")

        with (
            patch(
                "bd_archive.archive.prepare_move.rename_exclusive", side_effect=self.cross_device
            ),
            patch("bd_archive.archive.prepare_move.copy_verified", side_effect=change_after_copy),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            move_unit(self.source, unit, self.root / "moved")
        self.assertTrue(original.exists())
        self.assertFalse((self.root / "moved").exists())

    def test_space_check_only_requires_additional_cross_device_bytes(self):
        from dataclasses import replace

        self.file("file", b"x" * 8192)
        unit = self.units()[0]
        empty_fs = SimpleNamespace(f_frsize=4096, f_bsize=4096, f_bavail=0)
        with patch("bd_archive.archive.prepare_move.os.statvfs", return_value=empty_fs):
            check_space(self.source, self.output, [unit])
            foreign = replace(unit, entries=tuple(replace(e, device=-1) for e in unit.entries))
            with self.assertRaisesRegex(ValueError, "need.*available"):
                check_space(self.source, self.output, [foreign])
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(shutil.which("mkisofs"), "requires mkisofs")
    def test_declining_preview_leaves_source_and_output_unchanged(self):
        self.file("video/file")
        before = scan_raw_source(self.source)
        args = build_parser().parse_args(
            [
                "prepare",
                "-s",
                str(self.source),
                "-o",
                str(self.output),
                "-b",
                str(20 * MiB),
            ]
        )
        with patch("builtins.input", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            cmd_prepare(args)
        self.assertEqual(scan_raw_source(self.source), before)
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(shutil.which("mkisofs"), "requires mkisofs")
    def test_space_failure_precedes_confirmation_and_any_move(self):
        self.file("video/file")
        args = build_parser().parse_args(
            [
                "prepare",
                "-s",
                str(self.source),
                "-o",
                str(self.output),
                "-b",
                str(20 * MiB),
            ]
        )
        with (
            patch("bd_archive.commands.prepare.check_space", side_effect=ValueError("No space")),
            patch("bd_archive.commands.prepare.prompt_yn") as confirm,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ValueError, "No space"),
        ):
            cmd_prepare(args)
        confirm.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertTrue((self.source / "video/file").exists())

    @unittest.skipUnless(shutil.which("mkisofs"), "requires mkisofs")
    def test_no_qualifying_last_disc_leaves_everything_untouched(self):
        self.file("video/file")
        args = build_parser().parse_args(
            [
                "prepare",
                "-s",
                str(self.source),
                "-o",
                str(self.output),
                "-b",
                str(20 * MiB),
                "--max-last-free",
                "5",
            ]
        )
        with patch("builtins.input") as prompt, contextlib.redirect_stdout(io.StringIO()):
            cmd_prepare(args)
        prompt.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertTrue((self.source / "video/file").exists())

    def test_confirmation_delay_rechecks_free_space(self):
        self.file("video/file")
        with (
            patch(
                "bd_archive.archive.prepare_move.check_space", side_effect=ValueError("No space")
            ),
            self.assertRaisesRegex(ValueError, "No space"),
        ):
            move_plan(self.source, self.output, self.units(), ((0,),))
        self.assertFalse(self.output.exists())
        self.assertTrue((self.source / "video/file").exists())

    def test_prepare_has_no_bypass_or_dry_run_flags(self):
        for option in ("-y", "--dry-run", "--move"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args(
                    [
                        "prepare",
                        "-s",
                        str(self.source),
                        "-o",
                        str(self.output),
                        option,
                    ]
                )


@unittest.skipUnless(shutil.which("mkisofs") and shutil.which("par2"), "requires mkisofs/par2")
class PrepareIntegrationTests(FilesystemTests):
    def test_prepare_then_create_and_verify_each_disc(self):
        for number, size in enumerate((14, 14, 10, 10)):
            path = self.file(f"video-{number}/movie")
            with path.open("wb") as stream:
                stream.truncate(size * MiB)
            os.utime(path, ns=(number * 10**9, number * 10**9))
        args = build_parser().parse_args(
            [
                "prepare",
                "-s",
                str(self.source),
                "-o",
                str(self.output),
                "-b",
                str(27 * MiB),
                "-r",
                "none",
            ]
        )
        with (
            patch("builtins.input", side_effect=["1", "y"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cmd_prepare(args)
        folders = sorted(self.output.iterdir())
        self.assertEqual(len(folders), 2)
        self.assertFalse(list(self.source.iterdir()))
        for folder in folders:
            target = self.root / f"created-{folder.name}"
            result = subprocess.run(
                [
                    str(Path.cwd() / ".venv/bin/python"),
                    "-m",
                    "bd_archive",
                    "create",
                    "-s",
                    str(folder),
                    "-n",
                    "Test",
                    "-o",
                    str(target),
                    "-b",
                    str(27 * MiB),
                    "-r",
                    "none",
                    "-y",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run(
                [
                    str(Path.cwd() / ".venv/bin/python"),
                    "-m",
                    "bd_archive",
                    "verify",
                    str(target / "discs/disc_0001"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_fixed_and_automatic_recovery_reservations_fit_real_creation(self):
        from bd_archive.commands.create import cmd_create

        movie = self.file("group/movie")
        with movie.open("wb") as stream:
            stream.truncate(3 * MiB)
        units = self.units()
        for redundancy in (None, 5):
            with self.subTest(redundancy=redundancy):
                size = measure((0,), units, 10 * MiB, redundancy)
                capacity = size.required + 128 * 1024
                disc = self.root / f"input-{redundancy}" / "disc_0001"
                shutil.copytree(self.source, disc)
                options = [] if redundancy is None else ["-r", str(redundancy)]
                args = build_parser().parse_args(
                    [
                        "create",
                        "-s",
                        str(disc),
                        "-n",
                        "Test",
                        "-o",
                        str(self.root / f"out-{redundancy}"),
                        "-b",
                        str(capacity),
                        "-y",
                        *options,
                    ]
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    cmd_create(args)


if __name__ == "__main__":
    unittest.main()
