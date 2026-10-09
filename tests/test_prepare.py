import contextlib
import errno
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bd_archive.archive.content_dates import ContentDate
from bd_archive.archive.prepare import (
    STRATEGIES,
    Unit,
    disorder,
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
from bd_archive.archive.share import (
    fixed_recovery_layout,
    parse_redundancy,
    parse_share,
    recovery_blocks,
)
from bd_archive.archive.sizing import compute_slice_bytes
from bd_archive.cli import build_parser
from bd_archive.commands.prepare import (
    PACKING_TEXT,
    checked_proposals,
    cmd_prepare,
    order_consistency,
)
from bd_archive.constants import MAX_PAR2_BLOCKS, MiB
from bd_archive.tools.burn_timeout import MAX_WRITE_TIMEOUT


def toy_units(sizes):
    return [
        Unit(str(i), (), size, date, 1, size, date, date, date, 0)
        for i, size in enumerate(sizes)
        for date in [i * 86400 * 10**9]
    ]


class PlannerTests(unittest.TestCase):
    def test_units_are_atomic_and_every_selected_unit_occurs_once(self):
        units = toy_units([14, 14, 11, 11, 13, 12])
        plans = proposals(units, 25, True, False)
        self.assertEqual(list(plans), ["efficient", "balanced", "ordered"])
        self.assertEqual(len(plans["efficient"][0]), 3)
        self.assertEqual(len(plans["ordered"][0]), 4)
        self.assertEqual(disorder(plans["ordered"][0], units)[0], 0)
        for plan in (p for candidates in plans.values() for p in candidates):
            self.assertEqual(sorted(i for group in plan for i in group), list(range(6)))
            self.assertTrue(all(sum(units[i].size for i in group) <= 25 for group in plan))
        self.assertEqual(plans, proposals(units, 25, True, False))

    def test_strategies_agree_when_order_costs_nothing(self):
        units = toy_units([12, 12, 14, 11])
        for strategy, candidates in proposals(units, 25, True, False).items():
            with self.subTest(strategy=strategy):
                self.assertEqual(candidates, [((0, 1), (2, 3))])

    def test_deferred_units_are_always_a_suffix_in_fill_order(self):
        units = toy_units([14, 14, 11, 11, 13, 12])
        for strategy, candidates in proposals(units, 25, True, True).items():
            with self.subTest(strategy=strategy):
                self.assertEqual(sum(len(g) for g in candidates[0]), 6)
                for plan in candidates:
                    selected = sorted(i for g in plan for i in g)
                    self.assertEqual(selected, list(range(len(selected))))

    def test_small_and_large_overlaps_both_contribute(self):
        units = toy_units([1] * 5)
        mild = disorder(((0, 2), (1, 3, 4)), units)
        severe = disorder(((0, 4), (1, 2, 3)), units)
        self.assertGreater(severe[0], mild[0])
        self.assertGreater(severe[1], mild[1])
        self.assertGreater(severe[2], mild[2])

    def test_file_limit_and_oversized_indivisible_unit(self):
        units = [Unit(str(i), (), 1, i, 20000, 1, i, i, i, 0) for i in range(3)]
        self.assertEqual(len(proposals(units, 25, True, False)["efficient"][0]), 3)
        self.assertEqual(len(proposals(units, 25, False, False)["efficient"][0]), 1)
        with self.assertRaisesRegex(ValueError, "Unit cannot fit"):
            proposals(toy_units([26]), 25, True, False)

    def test_share_parser(self):
        self.assertTrue(parse_share("5").allows(5, 100))
        self.assertFalse(parse_share("5").allows(6, 100))
        self.assertTrue(parse_share("0").allows(0, 100))
        self.assertTrue(parse_share("1.5G").allows(1_500_000_000, 100))
        self.assertFalse(parse_share("500M").allows(500_000_001, 10**10))
        self.assertEqual(parse_share("2.5").bytes_of(1000), 25)
        self.assertEqual(parse_share("2G").bytes_of(1000), 2_000_000_000)
        self.assertEqual((parse_share("5").label, parse_share("2G").label), ("5%", "2G"))
        for value in ("-1", "101", "nan", "5MiB", "inf", "none"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_share(value)

    def test_fixed_recovery_blocks_and_slice_sizing(self):
        self.assertEqual(recovery_blocks(parse_redundancy("5"), 2000, 4096), 100)
        self.assertEqual(recovery_blocks(parse_redundancy("5"), 1, 4096), 1)
        self.assertEqual(recovery_blocks(parse_redundancy("1M"), 2000, 4096), 245)
        block_size, count = fixed_recovery_layout(parse_redundancy("50M"), 25 * 10**9)
        self.assertEqual(block_size % 4, 0)
        self.assertLessEqual(25 * 10**9 / block_size, MAX_PAR2_BLOCKS)
        self.assertLessEqual(count * block_size, 50 * 10**6)
        self.assertGreater(count * block_size, 50 * 10**6 - block_size)
        block_size, count = fixed_recovery_layout(parse_redundancy("1M"), 25 * 10**9)
        self.assertEqual((block_size, count), (1_000_000, 1))
        self.assertEqual(fixed_recovery_layout(parse_redundancy("1M"), 10), (4, 65535))
        self.assertEqual(compute_slice_bytes(100 * MiB, 0, parse_redundancy("none")), 96 * MiB)
        self.assertEqual(compute_slice_bytes(100 * MiB, 0, parse_redundancy("20")), 80 * MiB)
        self.assertEqual(compute_slice_bytes(100 * MiB, 0, parse_redundancy("10M")), 86 * MiB)
        self.assertEqual(compute_slice_bytes(100 * MiB, 0, parse_redundancy("1G")), 0)

    def test_redundancy_parser(self):
        self.assertTrue(parse_redundancy("none").disabled)
        self.assertTrue(parse_redundancy("0").disabled)
        self.assertEqual(parse_redundancy("none").label, "none")
        self.assertEqual(parse_redundancy("5").bytes_of(2000), 100)
        self.assertEqual(parse_redundancy("500M").bytes_of(2000), 500_000_000)
        for value in ("off", "1.5", "101", "5MiB"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_redundancy(value)

    def test_free_space_limit_applies_to_every_disc(self):
        identity = patch(
            "bd_archive.commands.prepare.measure",
            side_effect=lambda g, u, c, r: Measurement(
                sum(u[i].size for i in g), sum(u[i].size for i in g)
            ),
        )

        def checked(units, limit):
            with identity:
                plans, _ = checked_proposals(
                    units,
                    proposals(units, 25, False, True),
                    25,
                    parse_redundancy("none"),
                    parse_share(limit),
                )
            return plans

        plans = checked(toy_units([14, 14, 11, 11, 13, 12]), "5")
        self.assertEqual(sum(len(g) for g in plans["efficient"]), 6)
        self.assertEqual(len(plans["efficient"]), 3)
        self.assertEqual(sum(len(g) for g in plans["balanced"]), 6)
        # Strictly in order, disc 1 holds 14 of 25 and cannot be filled by any cutoff.
        self.assertIsNone(plans["ordered"])
        plans = checked(toy_units([14, 11, 14, 11, 5]), "5")
        for strategy in ("efficient", "balanced", "ordered"):
            with self.subTest(strategy=strategy):
                self.assertEqual(plans[strategy], ((0, 1), (2, 3)))
        self.assertEqual(checked(toy_units([14]), "5"), dict.fromkeys(STRATEGIES))
        plans = checked(toy_units([14, 11, 14, 11, 5]), "100")
        self.assertEqual(sum(len(g) for g in plans["efficient"]), 5)

    def test_reserve_shrinks_the_usable_capacity(self):
        identity = patch(
            "bd_archive.commands.prepare.measure",
            side_effect=lambda g, u, c, r: Measurement(
                sum(u[i].size for i in g), sum(u[i].size for i in g)
            ),
        )
        units = toy_units([13, 12])
        with identity:
            plans, _ = checked_proposals(
                units, proposals(units, 25, False, False), 25, parse_redundancy("none"), None
            )
            self.assertEqual(plans["efficient"], ((0, 1),))
            plans, _ = checked_proposals(
                units, proposals(units, 24, False, False), 25, parse_redundancy("none"), None, 1
            )
            self.assertEqual(plans["efficient"], ((0,), (1,)))


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

    def units(self, rule="depth:1", order_by="name"):
        inventory = scan_raw_source(self.source)
        dates = {
            e.path: ContentDate(e.mtime_ns, "mtime") for e in inventory if stat.S_ISREG(e.mode)
        }
        return make_units(inventory, grouping(rule), dates, order_by)

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
        self.assertEqual(len(self.units("depth:inf")), 5)

    def test_cli_shows_expected_format_for_invalid_values(self):
        prepare = ["prepare", "-s", ".", "-o", "out"]
        cases = [
            (
                [*prepare, "--group-by", "files"],
                "argument --group-by: invalid value 'files': "
                "expected depth:N (N >= 1) or depth:inf",
            ),
            (
                [*prepare, "--max-free", "5MiB"],
                "argument --max-free: invalid value '5MiB': "
                "expected a percentage (5), decimal MB (500M) or decimal GB (2G)",
            ),
            (
                [*prepare, "--max-free", "101"],
                "argument --max-free: invalid value '101': expected a percentage between 0 and 100",
            ),
            (
                [*prepare, "-r", "off"],
                "argument -r/--redundancy: invalid value 'off': expected a percentage (5), "
                "decimal MB (500M) or decimal GB (2G), or none",
            ),
            (
                ["burn", "-i", "in", "--write-timeout", "0"],
                "argument --write-timeout: invalid value '0': "
                f"expected 1-{MAX_WRITE_TIMEOUT} seconds",
            ),
        ]
        for argv, message in cases:
            stderr = io.StringIO()
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(stderr),
                self.assertRaises(SystemExit) as exc,
            ):
                build_parser().parse_args(argv)
            self.assertEqual(exc.exception.code, 2)
            self.assertIn(message, stderr.getvalue())

    def test_name_order_ranks_units_by_path_and_date_orders_by_date(self):
        for number, name in enumerate(("c/movie", "a/movie", "b/movie")):
            os.utime(self.file(name), ns=(number * 10**9, number * 10**9))
        by_name = self.units()
        self.assertEqual([u.path for u in by_name], ["a", "b", "c"])
        self.assertEqual([u.order for u in by_name], [0, 1, 2])
        by_date = self.units(order_by="mtime")
        self.assertEqual([u.path for u in by_date], ["c", "a", "b"])
        self.assertEqual([u.order for u in by_date], [u.date for u in by_date])

    def test_units_count_files_dated_from_metadata(self):
        self.file("video/movie.mkv", b"x" * 100)
        self.file("video/info.json")
        inventory = scan_raw_source(self.source)
        dates = {
            "video/movie.mkv": ContentDate(2017, "metadata", True),
            "video/info.json": ContentDate(2026, "mtime"),
        }
        (unit,) = make_units(inventory, 1, dates, "content-date")
        self.assertEqual(unit.metadata, 1)
        (unit,) = make_units(inventory, 1, dates, "name")
        self.assertEqual(unit.metadata, 1)

    def test_order_option_defaults_to_name_and_rejects_other_values(self):
        args = build_parser().parse_args(["prepare", "-s", ".", "-o", "out"])
        self.assertEqual(args.order_by, "name")
        for value in ("name", "mtime", "content-date"):
            args = build_parser().parse_args(
                ["prepare", "-s", ".", "-o", "out", "--order-by", value]
            )
            self.assertEqual(args.order_by, value)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            build_parser().parse_args(["prepare", "-s", ".", "-o", "out", "--order-by", "date"])
        self.assertEqual(exc.exception.code, 2)

    @unittest.skipUnless(shutil.which("mkisofs"), "requires mkisofs")
    def test_name_order_previews_name_ranges_without_reading_metadata(self):
        self.file("b/movie")
        self.file("a/movie")
        args = build_parser().parse_args(
            ["prepare", "-s", str(self.source), "-o", str(self.output), "-b", str(20 * MiB)]
        )
        captured = io.StringIO()
        with (
            patch("bd_archive.commands.prepare.scan_dates") as scan,
            patch("bd_archive.commands.prepare.check_deps") as deps,
            patch("builtins.input", return_value="n"),
            contextlib.redirect_stdout(captured),
        ):
            cmd_prepare(args)
        scan.assert_not_called()
        self.assertNotIn("exiftool", deps.call_args.args)
        text = captured.getvalue()
        self.assertRegex(text, r"Order\s+: name")
        self.assertRegex(text, r"Max free per disc\s+: unlimited")
        self.assertNotIn("Files with metadata", text)
        for number, strategy in enumerate(STRATEGIES, 1):
            self.assertRegex(
                text,
                rf"Plan {number} \({strategy}\)\n.*Packing\s+: {PACKING_TEXT[strategy]}\n"
                r".*Discs\s+: 1\n",
            )
        self.assertEqual(text.count("Order consistency    : 100%"), 3)
        self.assertIn("Automatically selected plan: all available plans are identical", text)
        self.assertRegex(text, r"Name range\s+: a to b")
        self.assertNotIn("\n[INFO]    a (", text)

    def test_order_consistency_is_whole_percent_and_only_perfect_is_full(self):
        self.assertEqual(order_consistency(0.0), 100)
        self.assertEqual(order_consistency(0.001), 99)
        self.assertEqual(order_consistency(0.29), 71)
        self.assertEqual(order_consistency(1.0), 0)
        self.assertEqual(order_consistency(1.5), 0)

    def test_grouping_parser(self):
        self.assertEqual(grouping("depth:1"), 1)
        self.assertEqual(grouping("depth:12"), 12)
        self.assertIsNone(grouping("depth:inf"))
        for value in ("top-level", "files", "depth:0", "depth:", "depth:-1", "inf", "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                grouping(value)

    def test_folder_date_uses_weighted_median_not_directory_or_sidecar_mtime(self):
        old = self.file("video/movie", b"old movie" * 100)
        newer = self.file("video/sidecar")
        os.utime(old, ns=(100, 100))
        os.utime(newer, ns=(200, 200))
        os.utime(old.parent, ns=(999999, 999999))
        self.assertEqual(self.units()[0].date, 100)

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

    def test_exclusive_rename_falls_back_to_plain_rename_without_flag_support(self):
        original = self.file("video/movie")
        target = self.root / "moved"
        with patch(
            "bd_archive.archive.prepare_move.rename_noreplace", return_value=errno.EINVAL
        ) as noreplace:
            rename_exclusive(original.parent, target)
        noreplace.assert_called_once()
        self.assertEqual((target / "movie").read_bytes(), b"payload")
        self.assertFalse(original.parent.exists())

    def test_plain_rename_fallback_does_not_replace_existing_target(self):
        original = self.file("file")
        target = self.root / "existing"
        target.write_bytes(b"keep")
        with (
            patch("bd_archive.archive.prepare_move.rename_noreplace", return_value=errno.EINVAL),
            self.assertRaises(FileExistsError),
        ):
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

    @unittest.skipUnless(
        shutil.which("mkisofs") and shutil.which("exiftool"), "requires mkisofs/exiftool"
    )
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

    @unittest.skipUnless(
        shutil.which("mkisofs") and shutil.which("exiftool"), "requires mkisofs/exiftool"
    )
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

    @unittest.skipUnless(
        shutil.which("mkisofs") and shutil.which("exiftool"), "requires mkisofs/exiftool"
    )
    def test_no_qualifying_plan_leaves_everything_untouched(self):
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
                "--max-free",
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


@unittest.skipUnless(
    all(shutil.which(tool) for tool in ("mkisofs", "par2", "exiftool")),
    "requires mkisofs/par2/exiftool",
)
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
        for redundancy in (None, "5", "100M"):
            with self.subTest(redundancy=redundancy):
                share = None if redundancy is None else parse_redundancy(redundancy)
                size = measure((0,), units, 10 * MiB, share)
                capacity = size.required + 128 * 1024
                disc = self.root / f"input-{redundancy}" / "disc_0001"
                shutil.copytree(self.source, disc)
                options = [] if redundancy is None else ["-r", redundancy]
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
