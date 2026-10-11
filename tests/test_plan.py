import contextlib
import io
import os
import shutil
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bd_archive.archive.content_dates import ContentDate
from bd_archive.archive.plan import (
    STRATEGIES,
    Unit,
    disorder,
    grouping,
    make_units,
    proposals,
)
from bd_archive.archive.plan_sizing import DiscLayout, Measurement
from bd_archive.archive.raw import RawEntry, scan_raw_source
from bd_archive.archive.share import (
    fixed_recovery_layout,
    parse_redundancy,
    parse_share,
    recovery_blocks,
)
from bd_archive.archive.sizing import compute_slice_bytes
from bd_archive.cli import build_parser
from bd_archive.commands.create import cmd_create
from bd_archive.commands.create_raw import (
    PACKING_TEXT,
    checked_proposals,
    order_consistency,
)
from bd_archive.constants import MAX_PAR2_BLOCKS, MiB
from bd_archive.tools.burn_timeout import MAX_WRITE_TIMEOUT

LAYOUT = DiscLayout("Test", "test", (), parse_redundancy("5"), 0)
NO_RECOVERY = replace(LAYOUT, recovery=None)


def identity_measure():
    """Measure a disc as exactly its data, for planner-only tests."""
    return patch(
        "bd_archive.commands.create_raw.measure",
        side_effect=lambda g, u, c, layout: Measurement(
            sum(u[i].size for i in g), sum(u[i].size for i in g)
        ),
    )


def toy_units(sizes):
    return [
        Unit(str(i), (), size, date, 1, size, date, date, date, 0)
        for i, size in enumerate(sizes)
        for date in [i * 86400 * 10**9]
    ]


class PlannerTests(unittest.TestCase):
    def test_units_are_atomic_and_every_selected_unit_occurs_once(self):
        units = toy_units([14, 14, 11, 11, 13, 12])
        plans = proposals(units, 25)
        self.assertEqual(list(plans), ["efficient", "balanced", "ordered"])
        self.assertEqual(len(plans["efficient"][0]), 3)
        self.assertEqual(len(plans["ordered"][0]), 4)
        self.assertEqual(disorder(plans["ordered"][0], units)[0], 0)
        for plan in (p for candidates in plans.values() for p in candidates):
            self.assertEqual(sorted(i for group in plan for i in group), list(range(6)))
            self.assertTrue(all(sum(units[i].size for i in group) <= 25 for group in plan))
        self.assertEqual(plans, proposals(units, 25))

    def test_strategies_agree_when_order_costs_nothing(self):
        units = toy_units([12, 12, 14, 11])
        for strategy, candidates in proposals(units, 25).items():
            with self.subTest(strategy=strategy):
                self.assertEqual(candidates, [((0, 1), (2, 3))])

    def test_deferred_units_are_always_a_suffix_in_fill_order(self):
        units = toy_units([14, 14, 11, 11, 13, 12])
        for strategy, candidates in proposals(units, 25, 25).items():
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
        self.assertEqual(len(proposals(units, 25)["efficient"][0]), 3)
        with self.assertRaisesRegex(ValueError, "Unit cannot fit"):
            proposals(toy_units([26]), 25)

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
        def checked(units, limit):
            share = parse_share(limit)
            with identity_measure():
                plans, _ = checked_proposals(
                    units, proposals(units, 25, share.bytes_of(25)), 25, share, LAYOUT
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

    def test_free_space_limit_finds_plans_the_heuristic_packings_miss(self):
        # Weights in MiB and dates in hours of a real collection on 50 GB BD-R DL
        # without reserve: size-first packing needs a fourth disc, yet three nearly
        # full discs exist.
        sizes = [3564, 1596, 12523, 3776, 18218, 8440, 5632, 17161, 5155, 8767, 5013, 14652]
        sizes += [7864, 10495, 19250]
        hours = [0, 805, 1511, 1674, 1733, 1786, 2071, 2159, 2300, 2626, 2715, 2721, 2793]
        hours += [2807, 3108]
        units = [
            replace(unit, date=date, order=date, date_start=date, date_end=date)
            for unit, date in zip(toy_units(sizes), (h * 3600 * 10**9 for h in hours), strict=True)
        ]
        budget = 47730
        max_unused = parse_share("5").bytes_of(budget)
        plan = proposals(units, budget, max_unused)["efficient"][0]
        self.assertEqual(sorted(i for g in plan for i in g), list(range(15)))
        self.assertEqual(len(plan), 3)
        for group in plan:
            self.assertLessEqual(budget - sum(units[i].size for i in group), max_unused)
            self.assertGreaterEqual(budget, sum(units[i].size for i in group))
        # The most ordered of all plans that meet the limit.
        self.assertEqual(plan, ((0, 1, 2, 3, 5, 7), (4, 8, 9, 10, 13), (6, 11, 12, 14)))
        self.assertEqual(order_consistency(disorder(plan, units)[0]), 84)

    def test_empty_units_travel_with_data_unless_nothing_is_protected(self):
        candidates = {"ordered": [((0,), (1,))]}
        data = RawEntry("Info.txt", stat.S_IFREG | 0o644, 5, 0, 0, 1, 1)
        with identity_measure():
            plans, _ = checked_proposals(toy_units([0, 10]), candidates, 25, None, LAYOUT)
            self.assertIsNone(plans["ordered"])
            for layout in (NO_RECOVERY, replace(LAYOUT, attachments=(data,))):
                with self.subTest(layout=layout):
                    plans, _ = checked_proposals(toy_units([0, 10]), candidates, 25, None, layout)
                    self.assertEqual(plans["ordered"], ((0,), (1,)))

    def test_unit_too_large_for_any_disc_names_the_options(self):
        with identity_measure(), self.assertRaisesRegex(ValueError, "--group-by.*-r.*-m dar"):
            checked_proposals(toy_units([30]), {"ordered": [((0,),)]}, 25, None, LAYOUT)


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
        create = ["create", "-s", ".", "-n", "Test", "-o", "out"]
        cases = [
            (
                [*create, "--group-by", "files"],
                "argument --group-by: invalid value 'files': "
                "expected depth:N (N >= 1) or depth:inf",
            ),
            (
                [*create, "--max-free", "5MiB"],
                "argument --max-free: invalid value '5MiB': "
                "expected a percentage (5), decimal MB (500M) or decimal GB (2G)",
            ),
            (
                [*create, "--max-free", "101"],
                "argument --max-free: invalid value '101': expected a percentage between 0 and 100",
            ),
            (
                [*create, "-r", "5MiB"],
                "argument -r/--redundancy: invalid value '5MiB': "
                "expected a percentage (5), decimal MB (500M) or decimal GB (2G), or none",
            ),
            (
                [*create, "--reserve", "5"],
                "unrecognized arguments: --reserve 5",
            ),
            (
                ["prepare", "-s", ".", "-o", "out"],
                "invalid choice: 'prepare'",
            ),
            (
                ["burn", "-i", "in", "--write-timeout", "0"],
                "argument --write-timeout: invalid value '0': "
                f"expected 1-{MAX_WRITE_TIMEOUT} seconds",
            ),
            (
                ["burn", "-i", "in", "--buffer", "0"],
                "argument --buffer: invalid value '0': expected 1M-64G",
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
        create = ["create", "-s", ".", "-n", "Test", "-o", "out"]
        args = build_parser().parse_args(create)
        # Unset planning options stay absent, so DAR mode can reject explicit ones.
        self.assertFalse(hasattr(args, "order_by"))
        self.assertFalse(hasattr(args, "group_by"))
        for value in ("name", "mtime", "content-date"):
            args = build_parser().parse_args([*create, "--order-by", value])
            self.assertEqual(args.order_by, value)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            build_parser().parse_args([*create, "--order-by", "date"])
        self.assertEqual(exc.exception.code, 2)

    def test_dar_mode_rejects_raw_planning_options(self):
        create = ["create", "-m", "dar", "-s", ".", "-n", "Test", "-o", "out"]
        for options in (
            ["--group-by", "depth:2"],
            ["--order-by", "name"],
            ["--max-free", "5"],
            ["--attach", "info.txt"],
        ):
            messages = io.StringIO()
            with (
                self.subTest(options=options),
                patch("bd_archive.commands.create.check_deps") as deps,
                contextlib.redirect_stdout(messages),
                contextlib.redirect_stderr(messages),
                self.assertRaises(SystemExit) as exc,
            ):
                cmd_create(build_parser().parse_args([*create, *options]))
            self.assertEqual(exc.exception.code, 1)
            self.assertIn(f"DAR mode cannot be combined with {options[0]}", messages.getvalue())
            deps.assert_not_called()

    @unittest.skipUnless(shutil.which("mkisofs"), "requires mkisofs")
    def test_name_order_previews_name_ranges_without_reading_metadata(self):
        self.file("b/movie")
        self.file("a/movie")
        args = build_parser().parse_args(
            [
                "create",
                "-s",
                str(self.source),
                "-n",
                "Test",
                "-o",
                str(self.output),
                "-b",
                str(20 * MiB),
            ]
        )
        captured = io.StringIO()
        with (
            patch("bd_archive.commands.create_raw.scan_dates") as scan,
            patch("bd_archive.commands.create_raw.check_deps") as deps,
            patch("bd_archive.commands.create_raw.software_info", return_value=""),
            patch("builtins.input", return_value="n"),
            contextlib.redirect_stdout(captured),
        ):
            cmd_create(args)
        scan.assert_not_called()
        self.assertNotIn("exiftool", deps.call_args.args)
        text = captured.getvalue()
        self.assertRegex(text, r"Order\s+: name")
        self.assertRegex(text, r"Max free\s+: unlimited")
        self.assertRegex(
            text, r"Redundancy\s+: 5% of the data \(-r\)\n.*Fill free space\s+: disabled\n"
        )
        self.assertNotIn("Reserve", text)
        self.assertNotIn("Files with metadata", text)
        self.assertNotIn("Recovery (fill)", text)
        for number, strategy in enumerate(STRATEGIES, 1):
            self.assertRegex(
                text,
                rf"Plan {number} \({strategy}\)\n.*Packing\s+: {PACKING_TEXT[strategy]}\n"
                r".*Units\s+: 2\n.*Data\s+: 14 B\n.*Name range\s+: a to b\n"
                r".*Free \(unused\)\s+: .*\n.*Recovery \(-r\)\s+: .* B \(\d+\.\d\d%\)\n"
                r".*Estimated disc\s+: .*\n.*Remaining\s+: 0 B\n.*Further discs\s+: 0\n",
            )
        self.assertEqual(text.count("Order consistency    : 100%"), 3)
        self.assertIn("Automatically selected plan: all available plans are identical", text)
        self.assertNotIn("Remaining in source", text)
        self.assertRegex(
            text,
            r"Disc layout\n.*Volume label\s+: Test\n.*├── Test/\n.*├── README.txt\n.*└── .bd-archive/\n",
        )
        self.assertIn("Cancelled; nothing was created.", text)
        self.assertNotIn("\n[INFO]    a (", text)
        self.assertFalse(self.output.exists())

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


if __name__ == "__main__":
    unittest.main()
