"""Disc folders must fit exactly, remain portable and never need an intermediate ISO."""

import argparse
import contextlib
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bd_archive.archive.disc_folder import (
    MANIFEST,
    check_output_available,
    load_disc_set,
    prepare_folder,
    save_disc_set,
)
from bd_archive.cli import build_parser
from bd_archive.commands.burn import _burn_one_disc, cmd_burn
from bd_archive.tools import growisofs, mkisofs


class DiscFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bd-folder-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.payload = self.root / "payload"
        self.payload.write_bytes(b"archive data")
        self.discs = self.root / "output/discs"

    def prepare(self, **kwargs):
        with patch("bd_archive.tools.mkisofs.estimate_size", return_value=100):
            return prepare_folder(
                self.discs / "disc_0001",
                [("Archive/file.dar", self.payload)],
                "Test_G01_0001",
                "bd-archive test",
                32768,
                **kwargs,
            )

    def test_output_default_and_iso_opt_in(self):
        args = ["create", "-s", ".", "-n", "Test", "-o", "out"]
        self.assertFalse(build_parser().parse_args(args).iso)
        self.assertTrue(build_parser().parse_args([*args, "--iso"]).iso)

    def test_generated_dar_files_are_moved_without_rewriting(self):
        inode = self.payload.stat().st_ino
        folder = self.prepare(move_sources={self.payload})
        self.assertEqual((folder.root / "Archive/file.dar").stat().st_ino, inode)
        self.assertFalse(self.payload.exists())

    def test_source_payload_is_copied_and_independent(self):
        folder = self.prepare()
        self.assertNotEqual(
            (folder.root / "Archive/file.dar").stat().st_ino, self.payload.stat().st_ino
        )
        self.payload.write_bytes(b"changed source")
        folder.check_unchanged()
        self.assertEqual((folder.root / "Archive/file.dar").read_bytes(), b"archive data")

    def test_custom_workdir_cross_filesystem_falls_back_to_copy(self):
        real_rename = Path.rename

        def rename(path, target):
            if path == self.payload:
                raise OSError(errno.EXDEV, "different filesystem")
            return real_rename(path, target)

        with patch.object(Path, "rename", rename):
            folder = self.prepare(move_sources={self.payload})
        self.assertEqual((folder.root / "Archive/file.dar").read_bytes(), self.payload.read_bytes())

    def test_manifest_is_off_disc_and_relocatable(self):
        folder = self.prepare()
        save_disc_set(self.discs, [folder])
        moved = self.root / "moved"
        self.discs.parent.rename(moved)
        loaded = load_disc_set(moved / "discs")[0]
        loaded.check_unchanged()
        self.assertEqual(loaded.entries, [("", moved / "discs/disc_0001")])
        self.assertFalse((loaded.root / MANIFEST).exists())

    def test_stale_and_incomplete_sets_are_rejected(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, "already contains"):
            check_output_available(self.discs.parent)
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            load_disc_set(self.discs)

    def test_changes_missing_files_and_links_are_rejected(self):
        for change in ("modify", "add", "remove", "link"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                self.discs = Path(tmp) / "discs"
                folder = self.prepare()
                payload = folder.root / "Archive/file.dar"
                if change == "modify":
                    payload.write_bytes(b"changed data")
                elif change == "add":
                    (folder.root / "added").touch()
                elif change == "remove":
                    payload.unlink()
                else:
                    (folder.root / "link").symlink_to(self.payload)
                with self.assertRaises(ValueError):
                    folder.check_unchanged()

    def test_oversize_folder_is_not_published(self):
        with (
            patch("bd_archive.tools.mkisofs.estimate_size", return_value=101),
            self.assertRaisesRegex(ValueError, "exceeding writable capacity"),
        ):
            prepare_folder(
                self.discs / "disc_0001", [("payload", self.payload)], "Test", "test", 100
            )
        self.assertFalse((self.discs / MANIFEST).exists())

    def test_manifest_rejects_paths_and_missing_or_extra_discs(self):
        folder = self.prepare()
        save_disc_set(self.discs, [folder])
        manifest = self.discs / MANIFEST
        original = manifest.read_text()
        data = json.loads(original)
        data["discs"][0]["directory"] = "../../payload"
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "invalid disc sequence"):
            load_disc_set(self.discs)
        manifest.write_text(original)
        (self.discs / "disc_0002").mkdir()
        with self.assertRaisesRegex(ValueError, "unexpected"):
            load_disc_set(self.discs)

    def test_burn_uses_folder_after_remeasurement_and_preserves_fit_gate(self):
        folder = self.prepare()
        args = argparse.Namespace(
            skip_fit_check=False, no_verify=True, speed="4", write_timeout=600
        )
        for capacity, burns in ((32768, True), (32767, False), (None, False)):
            drive = Mock(device="/dev/test")
            with (
                self.subTest(capacity=capacity),
                patch("bd_archive.commands.burn.prompt_disc"),
                patch("bd_archive.commands.burn.detect_disc_capacity", return_value=capacity),
                patch("bd_archive.tools.mkisofs.estimate_size", return_value=100),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                if burns:
                    _burn_one_disc(args, self.discs.parent, folder, 1, 1, drive, 100)
                    drive.burn_folder.assert_called_once()
                    drive.burn.assert_not_called()
                else:
                    with self.assertRaises((SystemExit, ValueError)):
                        _burn_one_disc(args, self.discs.parent, folder, 1, 1, drive, 100)
                    drive.burn_folder.assert_not_called()

    def test_change_during_insertion_prompt_prevents_burn_even_with_skip_fit(self):
        folder = self.prepare()
        args = argparse.Namespace(
            skip_fit_check=True, no_verify=True, speed=None, write_timeout=600
        )
        drive = Mock(device="/dev/test")
        with (
            patch(
                "bd_archive.commands.burn.prompt_disc",
                side_effect=lambda *a: (folder.root / "extra").write_bytes(b"x"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            _burn_one_disc(args, self.discs.parent, folder, 1, 1, drive, 100)
        drive.burn_folder.assert_not_called()

    def test_incomplete_set_does_not_touch_drive(self):
        self.prepare()
        args = argparse.Namespace(input=str(self.discs.parent))
        with (
            patch("bd_archive.commands.burn.check_deps"),
            patch("bd_archive.commands.burn.resolve_device") as resolve,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            cmd_burn(args)
        resolve.assert_not_called()

    def test_growisofs_uses_same_options_and_backend_without_intermediate_file(self):
        args = mkisofs.filesystem_args(
            [("odd=path", self.payload)], "Test", "publisher", rock_ridge=True
        )
        proc = Mock(stdout=iter([]), returncode=0)
        with (
            patch("bd_archive.tools.growisofs.subprocess.Popen", return_value=proc) as popen,
            patch("bd_archive.tools.growisofs.shutil.which", return_value="/usr/bin/mkisofs"),
            patch.dict(os.environ, {"MKISOFS": "/wrong/backend"}),
            patch(
                "bd_archive.tools.growisofs.prepared_burn",
                side_effect=lambda device, seconds, env: contextlib.nullcontext({"env": env}),
            ),
        ):
            growisofs.burn("/dev/test", None, "4", filesystem_args=args)
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("-Z") + 1], "/dev/test")
        self.assertEqual(command[-len(args) :], args)
        self.assertNotIn("-o", command)
        self.assertEqual(popen.call_args.kwargs["env"]["MKISOFS"], "/usr/bin/mkisofs")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


@unittest.skipUnless(
    all(shutil.which(tool) for tool in ("dar", "par2", "mkisofs", "dvd+rw-mediainfo")),
    "requires dar, par2, mkisofs and dvd+rw-mediainfo",
)
class DiscFolderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bd-folder-integration-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source ä = folder"
        self.source.mkdir()
        (self.source / "payload").write_bytes(os.urandom(7_000_000))
        (self.source / "empty").touch()
        (self.source / "empty folder").mkdir()

    def cli(self, *args, expected=0):
        result = subprocess.run(
            [sys.executable, "-m", "bd_archive", *map(str, args)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def create(self, mode, output, *options):
        self.cli(
            "create",
            "-m",
            mode,
            "-s",
            self.source,
            "-n",
            "Test",
            "-o",
            output,
            "-b",
            "10000000",
            "-c",
            "none",
            "-y",
            *options,
        )
        self.assertFalse(list(output.rglob("*.iso")))
        self.assertFalse((output / ".bd-archive-work").exists())
        return load_disc_set(output / "discs")

    def test_raw_and_multidisc_dar_sizes_match_actual_iso_and_restore(self):
        for mode in ("raw", "dar"):
            with self.subTest(mode=mode):
                output = self.root / mode
                folders = self.create(mode, output, "-r", "5")
                self.assertEqual(len(folders), 1 if mode == "raw" else 2)
                for folder in folders:
                    self.cli("verify", folder.root)
                    iso = self.root / f"{mode}-{folder.root.name}.iso"
                    with contextlib.redirect_stdout(io.StringIO()):
                        mkisofs.build(
                            iso,
                            folder.entries,
                            folder.volume_label,
                            folder.publisher,
                            rock_ridge=folder.rock_ridge,
                        )
                    self.assertEqual(folder.measure(), iso.stat().st_size)
                    self.assertLessEqual(iso.stat().st_size, 10_000_000)
                if mode == "dar":
                    restored = self.root / "restored"
                    self.cli("extract", "--input", output, "-o", restored)
                else:
                    restored = folders[0].root / self.source.name
                self.assertEqual(
                    (restored / "payload").read_bytes(), (self.source / "payload").read_bytes()
                )
                self.assertEqual((restored / "empty").read_bytes(), b"")
                self.assertTrue((restored / "empty folder").is_dir())

    def test_raw_automatic_recovery_folder_fits_capacity(self):
        output = self.root / "auto"
        folder = self.create("raw", output)[0]
        self.assertGreater(folder.image_bytes, 8_900_000)
        self.assertLessEqual(folder.image_bytes, 10_000_000)
        self.cli("verify", folder.root)

    def test_restore_workdir_cannot_change_input_folder(self):
        (self.source / "payload").write_bytes(b"small archive")
        folder = self.create("dar", self.root / "overlap", "-r", "0")[0]
        self.cli(
            "extract",
            "-i",
            folder.root,
            "-o",
            self.root / "restore",
            "-w",
            folder.root / "scratch",
            expected=1,
        )
        folder.check_unchanged()
        self.cli("extract", "-i", folder.root, "-o", folder.root / "restore", expected=1)
        folder.check_unchanged()

    def test_packing_a_folder_keeps_original_and_restores_chain(self):
        # A small full generation leaves enough space for generation 2.
        (self.source / "payload").write_bytes(b"old content")
        first = self.root / "first"
        old = self.create("dar", first, "-r", "0")[0]
        catalog = first / "Test-gen1-catalog.0001.dar"
        (self.source / "new file").write_bytes(b"new content")
        second = self.root / "second"
        packed = self.create("dar", second, "-r", "0", "--base", catalog, "--pack-with", old.root)[
            0
        ]
        old.check_unchanged()
        self.assertTrue((packed.root / "Test-gen1").is_dir())
        self.assertTrue((packed.root / "Test-gen2").is_dir())
        self.cli("verify", packed.root)
        restored = self.root / "restored"
        self.cli("extract", "-i", packed.root, "-o", restored)
        self.assertEqual((restored / "payload").read_bytes(), b"old content")
        self.assertEqual((restored / "new file").read_bytes(), b"new content")
