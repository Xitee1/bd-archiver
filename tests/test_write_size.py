"""Capacity gates must include growisofs's final 32-KiB write block."""

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bd_archive.archive.disc_folder import DiscFolder, prepare_folder, tree_signature
from bd_archive.archive.raw import RawPar2Sizing
from bd_archive.archive.sizing import disc_write_bytes
from bd_archive.cli import build_parser
from bd_archive.commands.burn import _burn_one_disc
from bd_archive.commands.create import cmd_create
from bd_archive.commands.create_raw import _plan_auto_recovery
from bd_archive.constants import DISC_END_MARGIN


class WriteSizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "payload").write_bytes(b"payload")

    def test_rounding_includes_partial_blocks_without_extra_aligned_block(self):
        for size, expected in (
            (0, 0),
            (1, 32768),
            (30720, 32768),
            (32768, 32768),
            (34816, 65536),
            (65536, 65536),
            (4295831552, 4295852032),
        ):
            with self.subTest(size=size):
                self.assertEqual(disc_write_bytes(size), expected)

    def test_burn_checks_padding_for_both_inputs_and_block_boundaries(self):
        args = argparse.Namespace(skip_fit_check=False, no_verify=True, speed=None)
        for size in (30720, 32768, 34816):
            required = disc_write_bytes(size)
            iso = self.root / "disc.iso"
            iso.write_bytes(b"x" * size)
            folder = DiscFolder(
                self.source, "Test", "test", False, size, tree_signature(self.source)
            )
            for item in (iso, folder):
                for capacity in (required - 1, required, required + 1):
                    with (
                        self.subTest(size=size, folder=item is folder, capacity=capacity),
                        patch("bd_archive.commands.burn.prompt_disc"),
                        patch(
                            "bd_archive.commands.burn.detect_disc_capacity", return_value=capacity
                        ),
                        patch("bd_archive.tools.mkisofs.estimate_size", return_value=size),
                        contextlib.redirect_stdout(io.StringIO()),
                        contextlib.redirect_stderr(io.StringIO()),
                    ):
                        drive = Mock(device="/dev/test")
                        if capacity < required:
                            with self.assertRaises(SystemExit):
                                _burn_one_disc(args, self.root, item, 1, 1, drive, required)
                            drive.burn.assert_not_called()
                            drive.burn_folder.assert_not_called()
                        else:
                            _burn_one_disc(args, self.root, item, 1, 1, drive, required)
                            self.assertEqual(
                                drive.burn.call_count + drive.burn_folder.call_count, 1
                            )

    def test_folder_creation_keeps_image_size_but_enforces_write_size(self):
        for capacity in (34816, 65535, 65536, 65537):
            with (
                self.subTest(capacity=capacity),
                patch("bd_archive.tools.mkisofs.estimate_size", return_value=34816),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                target = self.root / str(capacity)
                if capacity < 65536:
                    with self.assertRaisesRegex(ValueError, "65536 bytes including 32-KiB"):
                        prepare_folder(target, [("", self.source)], "Test", "test", capacity)
                else:
                    folder = prepare_folder(target, [("", self.source)], "Test", "test", capacity)
                    self.assertEqual(folder.image_bytes, 34816)

    def test_auto_recovery_accounts_for_padding_before_safety_margin(self):
        metadata = self.root / "metadata"
        metadata.mkdir()
        # Include the write block reserved for the final README.
        for available, succeeds in ((98303, False), (98304, True)):
            with (
                self.subTest(available=available),
                patch("bd_archive.tools.mkisofs.estimate_size", return_value=34816),
            ):

                def plan(available=available):
                    return _plan_auto_recovery(
                        metadata,
                        [],
                        "Test",
                        "test",
                        RawPar2Sizing(4, 128),
                        available + DISC_END_MARGIN,
                    )

                if succeeds:
                    _, estimate = plan()
                    self.assertEqual(estimate, 65536)
                else:
                    with self.assertRaisesRegex(ValueError, "Not enough free disc capacity"):
                        plan()
                self.assertEqual(list(metadata.iterdir()), [])

    def test_final_iso_guard_in_raw_and_dar_includes_padding(self):
        size = 8 * 1024 * 1024 + 2048
        required = 8 * 1024 * 1024 + 32768

        def build(path, *args, **kwargs):
            with path.open("wb") as iso:
                iso.truncate(size)

        def create_slice(base, *args, **kwargs):
            Path(str(base) + ".0001.dar").write_bytes(b"slice")

        def create_catalog(base):
            Path(str(base) + "-catalog.0001.dar").write_bytes(b"catalog")

        for mode in ("raw", "dar"):
            for capacity in (size, required - 1, required, required + 1):
                output = self.root / f"{mode}-{capacity}"
                args = build_parser().parse_args(
                    [
                        "create",
                        "-m",
                        mode,
                        "--iso",
                        "-s",
                        str(self.source),
                        "-n",
                        "Test",
                        "-o",
                        str(output),
                        "-b",
                        str(capacity),
                        "-r",
                        "0",
                        "-c",
                        "none",
                        "-y",
                    ]
                )
                with (
                    self.subTest(mode=mode, capacity=capacity),
                    patch("bd_archive.commands.create.check_deps"),
                    patch("bd_archive.commands.create_raw.check_deps"),
                    patch("bd_archive.tools.mkisofs.estimate_size", return_value=65536),
                    patch("bd_archive.tools.mkisofs.build", side_effect=build) as builder,
                    patch("bd_archive.tools.dar.create_sliced", side_effect=create_slice),
                    patch("bd_archive.tools.dar.isolate_catalog", side_effect=create_catalog),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    if capacity < required:
                        with self.assertRaises(SystemExit) as exc:
                            cmd_create(args)
                        self.assertEqual(exc.exception.code, 1)
                        self.assertFalse((output / "images/disc_0001.iso").exists())
                    else:
                        cmd_create(args)
                        self.assertEqual((output / "images/disc_0001.iso").stat().st_size, size)
                    builder.assert_called_once()
