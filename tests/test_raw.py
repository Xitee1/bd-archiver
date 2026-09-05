import argparse
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bd_archive.archive.raw import scan_raw_source
from bd_archive.cli import build_parser
from bd_archive.commands.burn import _burn_one_disc
from bd_archive.commands.create import cmd_create


class RawValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "video.mkv").write_bytes(b"video")

    def args(self, *options):
        return build_parser().parse_args(
            [
                "create",
                "--raw",
                "-s",
                str(self.source),
                "-n",
                "Media",
                "-o",
                str(self.root / "output"),
                "-b",
                "10000000",
                "-y",
                *options,
            ]
        )

    def test_rejects_incompatible_options_before_tools(self):
        for options in (
            ["-c", "zstd"],
            ["-l", "1"],
            ["--base", "base.dar"],
            ["--pack-with", "old.iso"],
            ["--sample", "."],
            ["--ratio", "1"],
            ["--min-last-disc-fill", "50"],
            ["-r", "0"],
            ["-r", "101"],
            ["-b", "0"],
        ):
            with (
                self.subTest(options=options),
                patch("bd_archive.commands.create_raw.check_deps") as deps,
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                cmd_create(self.args(*options))
            deps.assert_not_called()

    def test_rejects_overlapping_paths_and_stale_isos(self):
        output = self.root / "output"
        (output / "images").mkdir(parents=True)
        (output / "images" / "disc_0001.iso").write_bytes(b"old image")
        for options in ([], ["-o", str(self.source / "out")], ["-w", str(self.source)]):
            with (
                self.subTest(options=options),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                cmd_create(self.args(*options))
        self.assertEqual((output / "images/disc_0001.iso").read_bytes(), b"old image")

    def test_inventory_rejects_unsupported_entries(self):
        for name, kind in (
            ("link", "link"),
            ("fifo", "fifo"),
            ("a\nb", "file"),
            (".bd-archive", "dir"),
        ):
            entry = self.source / name
            if kind == "link":
                entry.symlink_to(self.source / "video.mkv")
            elif kind == "fifo":
                os.mkfifo(entry)
            elif kind == "dir":
                entry.mkdir()
            else:
                entry.touch()
            with self.subTest(name=name), self.assertRaises(ValueError):
                scan_raw_source(self.source)
            entry.rmdir() if kind == "dir" else entry.unlink()

    def test_single_disc_allows_unused_space_but_never_overflow(self):
        iso = self.root / "disc_0001.iso"
        iso.write_bytes(b"x" * 100)
        args = argparse.Namespace(skip_fit_check=False, no_verify=True, speed=None)
        for count, capacity, succeeds in ((1, 1000, True), (1, 99, False), (2, 1000, False)):
            drive = Mock(device="/dev/fake")
            with (
                self.subTest(count=count, capacity=capacity),
                patch("bd_archive.commands.burn.prompt_disc"),
                patch("bd_archive.commands.burn.detect_disc_capacity", return_value=capacity),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                if succeeds:
                    _burn_one_disc(args, self.root, iso, 1, count, drive, 100)
                    drive.burn.assert_called_once_with(iso, None)
                else:
                    with self.assertRaises(SystemExit):
                        _burn_one_disc(args, self.root, iso, 1, count, drive, 100)
                    drive.burn.assert_not_called()


@unittest.skipUnless(
    all(shutil.which(tool) for tool in ("par2", "mkisofs", "bsdtar")),
    "requires par2, mkisofs and bsdtar",
)
class RawIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bd-raw-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source = [media]"
        self.source.mkdir()
        (self.source / "Urlaub [2024]").mkdir()
        self.movie = Path("Urlaub [2024]/Grüße = Film.mkv")
        (self.source / self.movie).write_bytes(bytes(range(256)) * 256)
        (self.source / ".hidden").write_bytes(b"hidden content" * 10)
        (self.source / "user.par2").write_bytes(b"ordinary file, not a recovery index")
        (self.source / "empty").touch()
        (self.source / "empty directory").mkdir()
        self.output = self.root / "output = [disc]"
        # Prove that raw creation/verification require neither dar nor a
        # drive utility when capacity is supplied. Use real external tools.
        bindir = self.root / "bin"
        bindir.mkdir()
        for tool in ("par2", "mkisofs"):
            (bindir / tool).symlink_to(shutil.which(tool))
        self.env = {**os.environ, "PATH": str(bindir), "OMP_NUM_THREADS": "2"}

    def cli(self, *args, expected=0):
        result = subprocess.run(
            [sys.executable, "-m", "bd_archive", *map(str, args)],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def create(self, *options, expected=0):
        return self.cli(
            "create",
            "--raw",
            "-s",
            self.source,
            "-n",
            "Media",
            "-o",
            self.output,
            "-b",
            "10000000",
            "-r",
            "10",
            "-y",
            *options,
            expected=expected,
        )

    def test_roundtrip_verify_damage_and_repair_without_dar(self):
        self.create()
        self.assertFalse((self.output / ".bd-archive-work").exists())
        self.assertEqual(list(self.output.iterdir()), [self.output / "images"])
        restored = self.root / "restored"
        restored.mkdir()
        subprocess.run(
            [
                shutil.which("bsdtar"),
                "-xf",
                str(self.output / "images/disc_0001.iso"),
                "-C",
                str(restored),
            ],
            check=True,
        )
        for original in self.source.rglob("*"):
            target = restored / original.relative_to(self.source)
            if original.is_dir():
                self.assertTrue(target.is_dir())
            else:
                self.assertEqual(target.read_bytes(), original.read_bytes())
        # bsdtar's UDF reader restores optical-media read-only modes.
        # Repair needs a writable copy, as described in the disc README.
        for target in restored.rglob("*"):
            target.chmod(0o755 if target.is_dir() else 0o644)
        self.cli("verify", restored)
        with (restored / self.movie).open("r+b") as damaged:
            damaged.write(b"!" * 64)
        (restored / ".hidden").unlink()
        self.cli("verify", restored, expected=1)
        repair = subprocess.run(
            [shutil.which("par2"), "repair", "-B.", ".bd-archive/recovery.par2"],
            cwd=restored,
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(repair.returncode, 0, repair.stdout + repair.stderr)
        self.assertEqual(
            (restored / self.movie).read_bytes(), (self.source / self.movie).read_bytes()
        )
        self.assertEqual(
            (restored / ".hidden").read_bytes(), (self.source / ".hidden").read_bytes()
        )
        self.cli("verify", restored)
        # Remove repair backups, otherwise par2 can recover from those.
        for backup in restored.rglob("*.1"):
            backup.unlink()
        (restored / self.movie).unlink()
        self.cli("verify", restored, expected=2)
        (restored / ".bd-archive/recovery.par2").unlink()
        self.cli("verify", restored, expected=2)

    def test_capacity_checks_do_not_publish_oversized_image(self):
        self.create()
        size = (self.output / "images/disc_0001.iso").stat().st_size
        self.output = self.root / "too-small-with-par2"
        result = self.create("-b", str(size - 2048), expected=1)
        self.assertIn("exceeds disc capacity", result.stdout + result.stderr)
        self.assertFalse(list(self.output.rglob("disc_*.iso")))
        self.assertFalse((self.output / ".bd-archive-work").exists())
        self.output = self.root / "too-small-for-source"
        result = self.create("-b", "2048", expected=1)
        self.assertIn("even without PAR2", result.stdout + result.stderr)
        self.assertFalse(self.output.exists())

    def test_source_changes_prevent_publication(self):
        from bd_archive.tools import par2

        create_tree = par2.create_tree

        def change_source(*args):
            create_tree(*args)
            (self.source / self.movie).write_bytes(b"changed")

        args = build_parser().parse_args(
            [
                "create",
                "--raw",
                "-s",
                str(self.source),
                "-n",
                "Media",
                "-o",
                str(self.output),
                "-b",
                "10000000",
                "-y",
            ]
        )
        with (
            patch("bd_archive.commands.create_raw.par2.create_tree", side_effect=change_source),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            cmd_create(args)
        self.assertFalse(list(self.output.rglob("disc_*.iso")))


if __name__ == "__main__":
    unittest.main()
