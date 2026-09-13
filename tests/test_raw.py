import argparse
import contextlib
import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bd_archive.archive.raw import raw_par2_sizing, scan_raw_source, write_raw_checksums
from bd_archive.archive.sizing import disc_write_bytes
from bd_archive.cli import build_parser
from bd_archive.commands.burn import _burn_one_disc
from bd_archive.commands.create import cmd_create
from bd_archive.constants import DISC_END_MARGIN, DISC_WRITE_BLOCK, RAW_ROOT_MARKER, MiB


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

    def test_default_and_explicit_raw_modes_dispatch_without_dar(self):
        for options in ([], ["-m", "raw"], ["--mode", "raw"]):
            args = self.args(*options)
            with (
                self.subTest(options=options),
                patch("bd_archive.commands.create.cmd_create_raw") as raw,
                patch("bd_archive.commands.create.check_deps") as deps,
            ):
                cmd_create(args)
            self.assertEqual(args.mode, "raw")
            self.assertIsNone(args.redundancy)
            self.assertIsNone(args.compression)
            raw.assert_called_once_with(args)
            deps.assert_not_called()

    def test_mode_choices(self):
        self.assertEqual(self.args("--mode", "dar").mode, "dar")
        for options in (["-m", "invalid"], ["--raw"]):
            with (
                self.subTest(options=options),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as exc,
            ):
                self.args(*options)
            self.assertEqual(exc.exception.code, 2)

    def test_rejects_incompatible_options_before_tools(self):
        for options in (
            ["-c", "zstd"],
            ["-l", "1"],
            ["--base", "base.dar"],
            ["--pack-with", "old.iso"],
            ["--sample", "."],
            ["--ratio", "1"],
            ["--min-last-disc-fill", "50"],
            ["-r", "-1"],
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

    def test_dar_keeps_five_percent_default(self):
        args = self.args("-m", "dar")
        with (
            patch("bd_archive.commands.create.check_deps", side_effect=RuntimeError("stop")),
            self.assertRaises(RuntimeError),
        ):
            cmd_create(args)
        self.assertEqual(args.redundancy, 5)
        self.assertEqual(args.compression, "zstd")

    def test_source_folder_cannot_collide_with_root_metadata(self):
        for name in (
            "README.txt",
            "CHECKSUMS.SHA512",
            "recovery.par2",
            "recovery.vol000+001.par2",
            RAW_ROOT_MARKER,
            "line\nbreak",
            "line\rbreak",
        ):
            source = self.root / name
            source.mkdir()
            with (
                self.subTest(name=name),
                patch("bd_archive.commands.create_raw.check_deps") as deps,
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                cmd_create(self.args("-s", str(source)))
            deps.assert_not_called()

    def test_metadata_names_inside_source_are_ordinary_payload(self):
        (self.source / ".bd-archive").mkdir()
        (self.source / ".bd-archive/raw-v1").touch()
        for name in ("README.txt", "checksums.sha512", "recovery.par2", RAW_ROOT_MARKER):
            (self.source / name).touch()
        entries = {entry.path for entry in scan_raw_source(self.source)}
        self.assertIn(".bd-archive/raw-v1", entries)
        self.assertIn("recovery.par2", entries)

    def test_manifest_prefix_preserves_source_folder_and_backslashes(self):
        from bd_archive.archive.checksums import verify_manifest

        source = self.root / "source = [Grüße] \\ media"
        self.source.rename(source)
        manifest = self.root / "checksums.sha512"
        inventory = scan_raw_source(source)
        write_raw_checksums(source, inventory, manifest, placeholder=True, path_prefix=source.name)
        size = manifest.stat().st_size
        write_raw_checksums(source, inventory, manifest, path_prefix=source.name)
        self.assertEqual(size, manifest.stat().st_size)
        verify_manifest(manifest, self.root, expected_files={source / "video.mkv"})
        if shutil.which("sha512sum"):
            result = subprocess.run(
                ["sha512sum", "-c", str(manifest)], cwd=self.root, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_manifest_covers_empty_hidden_unicode_and_escaped_names(self):
        for name in ("empty", ".hidden", "Grüße = [2024]", "back\\slash", "-dash"):
            (self.source / name).write_bytes(b"" if name == "empty" else name.encode())
        before = scan_raw_source(self.source)
        manifest = self.root / "checksums.sha512"
        write_raw_checksums(self.source, before, manifest, placeholder=True)
        size = manifest.stat().st_size
        write_raw_checksums(self.source, before, manifest)
        self.assertEqual(manifest.stat().st_size, size)
        self.assertEqual(scan_raw_source(self.source), before)
        self.assertIn(hashlib.sha512(b"").hexdigest() + "  empty", manifest.read_text())
        self.assertEqual(len(manifest.read_text().splitlines()), 6)
        if shutil.which("sha512sum"):
            result = subprocess.run(
                ["sha512sum", "-c", str(manifest)], cwd=self.source, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_declining_preview_does_not_hash_or_create_recovery(self):
        args = self.args()
        args.yes = False
        with (
            patch("bd_archive.commands.create_raw.check_deps"),
            patch("bd_archive.commands.create_raw.mkisofs.estimate_size", return_value=100),
            patch("bd_archive.commands.create_raw.prompt_yn", return_value=False),
            patch("bd_archive.commands.create_raw.write_readme") as readme,
            patch("bd_archive.archive.raw._hash_file_sha512") as hash_file,
            patch("bd_archive.commands.create_raw.par2.create_tree") as recovery,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cmd_create(args)
        readme.assert_not_called()
        hash_file.assert_not_called()
        recovery.assert_not_called()
        self.assertFalse(list((self.root / "output").rglob("disc_*.iso")))
        self.assertFalse((self.root / "output/.bd-archive-work").exists())

    def test_capacity_failures_suggest_dar_and_do_not_publish(self):
        def recovery(source, index, *args, **kwargs):
            index.write_bytes(b"index")
            index.with_name("recovery.vol000+001.par2").write_bytes(b"recovery")

        def build(pending, *args, **kwargs):
            with pending.open("wb") as iso:
                iso.truncate(10_000_001)

        for stage, sizes, options in (
            ("payload", [10_000_001], []),
            ("auto recovery", [100] + [10_000_001] * 16, []),
            ("exact size", [100, 100, 10_000_001], ["-r", "10"]),
            ("built image", [100, 100, 100], ["-r", "10"]),
        ):
            output = self.root / stage
            messages = io.StringIO()
            with (
                self.subTest(stage=stage),
                patch("bd_archive.commands.create_raw.check_deps"),
                patch("bd_archive.commands.create_raw.mkisofs.estimate_size", side_effect=sizes),
                patch("bd_archive.commands.create_raw.par2.create_tree", side_effect=recovery),
                patch("bd_archive.commands.create_raw.mkisofs.build", side_effect=build),
                contextlib.redirect_stdout(messages),
                contextlib.redirect_stderr(messages),
                self.assertRaises(SystemExit) as exc,
            ):
                cmd_create(self.args("--iso", "-o", str(output), *options))
            self.assertEqual(exc.exception.code, 1)
            self.assertIn("-m dar", messages.getvalue())
            self.assertIn("multiple discs", messages.getvalue())
            self.assertFalse(list(output.rglob("disc_*.iso")))

    def test_oversized_preview_suggests_dar_before_confirmation(self):
        args = self.args("-r", "10")
        args.yes = False
        messages = io.StringIO()

        def decline(prompt):
            self.assertIn("-m dar", messages.getvalue())
            return False

        with (
            patch("bd_archive.commands.create_raw.check_deps"),
            patch(
                "bd_archive.commands.create_raw.mkisofs.estimate_size", side_effect=[100, 9_000_000]
            ),
            patch("bd_archive.commands.create_raw.prompt_yn", side_effect=decline) as prompt,
            patch("bd_archive.commands.create_raw.par2.create_tree") as recovery,
            contextlib.redirect_stdout(messages),
        ):
            cmd_create(args)
        prompt.assert_called_once()
        recovery.assert_not_called()

    def test_single_disc_allows_unused_space_but_never_overflow(self):
        iso = self.root / "disc_0001.iso"
        iso.write_bytes(b"x" * 100)
        args = argparse.Namespace(skip_fit_check=False, no_verify=True, speed=None)
        for count, capacity, succeeds in ((1, 65536, True), (1, 32767, False), (2, 65536, False)):
            drive = Mock(device="/dev/fake")
            with (
                self.subTest(count=count, capacity=capacity),
                patch("bd_archive.commands.burn.prompt_disc"),
                patch("bd_archive.commands.burn.detect_disc_capacity", return_value=capacity),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                if succeeds:
                    _burn_one_disc(args, self.root, iso, 1, count, drive, 32768)
                    drive.burn.assert_called_once_with(iso, None)
                else:
                    with self.assertRaises(SystemExit):
                        _burn_one_disc(args, self.root, iso, 1, count, drive, 32768)
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
        self.source = self.root / "BD_2026-09-12_153152_223667437 = [Grüße]"
        self.source.mkdir()
        (self.source / "Urlaub [2024]").mkdir()
        self.movie = Path("Urlaub [2024]/Grüße = Film.mkv")
        (self.source / self.movie).write_bytes(bytes(range(256)) * 256)
        (self.source / ".hidden").write_bytes(b"hidden content" * 10)
        (self.source / "user.par2").write_bytes(b"ordinary file, not a recovery index")
        (self.source / "empty").touch()
        (self.source / "empty directory").mkdir()
        (self.source / ".bd-archive").mkdir()
        (self.source / ".bd-archive/raw-v1").write_bytes(b"ordinary user data")
        (self.source / "README.txt").write_text("User's original README")
        (self.source / "checksums.sha512").write_text("User's own manifest")
        (self.source / "recovery.par2").write_bytes(b"ordinary payload")
        (self.root / "unrelated-sibling").write_bytes(b"must not be archived")
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

    def create(self, *options, expected=0, auto=False):
        return self.cli(
            "create",
            "--iso",
            "-s",
            self.source,
            "-n",
            "Media",
            "-o",
            self.output,
            "-b",
            "10000000",
            *([] if auto else ["-r", "10"]),
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
        self.assertEqual(
            {p.name for p in restored.iterdir()},
            {
                self.source.name,
                "README.txt",
                "checksums.sha512",
                "recovery.par2",
                *[p.name for p in restored.glob("recovery.vol*.par2")],
            },
        )
        self.assertFalse((restored / ".bd-archive").exists())
        readme = (restored / "README.txt").read_text()
        self.assertIn("ARCHIVE NAME:", readme)
        self.assertIn("Index:      recovery.par2", readme)
        self.assertIn("File:      checksums.sha512", readme)
        for original in self.source.rglob("*"):
            target = restored / self.source.name / original.relative_to(self.source)
            if original.is_dir():
                self.assertTrue(target.is_dir())
            else:
                self.assertEqual(target.read_bytes(), original.read_bytes())
        # bsdtar's UDF reader restores optical-media read-only modes.
        # Repair needs a writable copy.
        for target in restored.rglob("*"):
            target.chmod(0o755 if target.is_dir() else 0o644)
        self.cli("verify", restored)
        with (restored / self.source.name / self.movie).open("r+b") as damaged:
            damaged.write(b"!" * 64)
        (restored / self.source.name / ".hidden").unlink()
        self.cli("verify", restored, expected=1)
        repair = subprocess.run(
            [shutil.which("par2"), "repair", "-B.", "recovery.par2"],
            cwd=restored,
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(repair.returncode, 0, repair.stdout + repair.stderr)
        self.assertEqual(
            (restored / self.source.name / self.movie).read_bytes(),
            (self.source / self.movie).read_bytes(),
        )
        self.assertEqual(
            (restored / self.source.name / ".hidden").read_bytes(),
            (self.source / ".hidden").read_bytes(),
        )
        self.cli("verify", restored)
        # Remove repair backups, otherwise par2 can recover from those.
        for backup in restored.rglob("*.1"):
            backup.unlink()
        (restored / self.source.name / self.movie).unlink()
        self.cli("verify", restored, expected=2)
        (restored / "recovery.par2").unlink()
        self.cli("verify", restored, expected=2)

    def test_auto_fills_nearly_full_disc_with_less_than_one_percent(self):
        # A pre-packed tree that cannot accommodate fixed 5% redundancy.
        with (self.source / self.movie).open("wb") as movie:
            movie.truncate(64 * MiB)
        payload = subprocess.run(
            [
                shutil.which("mkisofs"),
                "-udf",
                "-iso-level",
                "3",
                "-R",
                "-print-size",
                str(self.source),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        capacity = int(payload.stdout.strip()) * 2048 + MiB + MiB // 2
        before = scan_raw_source(self.source)
        result = self.create("-b", str(capacity), auto=True)
        recovery_bytes = int(re.search(r"Block size: (\d+)", result.stdout)[1]) * int(
            re.search(r"Recovery block count: (\d+)", result.stdout)[1]
        )
        self.assertGreater(recovery_bytes, 0)
        self.assertLess(recovery_bytes, 64 * MiB / 100)
        self.assertIn("PAR2 redundancy: automatic", result.stdout)
        iso = self.output / "images/disc_0001.iso"
        free = capacity - disc_write_bytes(iso.stat().st_size)
        block_size = int(re.search(r"Block size: (\d+)", result.stdout)[1])
        self.assertGreaterEqual(free, DISC_END_MARGIN)
        # Allow one recovery block plus packet/filesystem overhead and write
        # alignment. The former byte-level 16-KiB allowance is too tight once
        # the planner allocates complete 32-KiB write blocks.
        self.assertLess(free, DISC_END_MARGIN + block_size + 2 * DISC_WRITE_BLOCK)
        self.assertEqual(scan_raw_source(self.source), before)
        restored = self.root / "auto-restored"
        restored.mkdir()
        subprocess.run([shutil.which("bsdtar"), "-xf", str(iso), "-C", str(restored)], check=True)
        readme = (restored / "README.txt").read_text()
        self.assertNotIn("automatic", readme)
        payload_bytes = sum(entry.size for entry in before if stat.S_ISREG(entry.mode))
        self.assertIn(f"Redundancy: {100 * recovery_bytes / payload_bytes:.2f}%", readme)
        self.cli("verify", restored)
        if shutil.which("sha512sum"):
            hashes = subprocess.run(
                ["sha512sum", "-c", "checksums.sha512"],
                cwd=restored,
                capture_output=True,
            )
            self.assertEqual(hashes.returncode, 0, hashes.stdout + hashes.stderr)
        self.output = self.root / "too-full-for-recovery"
        result = self.create("-b", str(capacity - MiB // 2), auto=True, expected=1)
        self.assertIn("-m dar", result.stdout + result.stderr)
        self.assertFalse(list(self.output.rglob("disc_*.iso")))

    def test_auto_uses_space_above_five_percent(self):
        result = self.create(auto=True)
        iso = self.output / "images/disc_0001.iso"
        self.assertGreater(iso.stat().st_size, 8_900_000)
        self.assertLessEqual(iso.stat().st_size, 10_000_000)
        self.assertIn("PAR2 redundancy: automatic", result.stdout)

    def test_par2_size_model_bounds_real_packet_sizes(self):
        from bd_archive.tools import par2

        inventory = scan_raw_source(self.source)
        sizing = raw_par2_sizing(inventory, 10_000_000, 9_000_000, path_prefix=self.source.name)
        for count in (1, 2, 3, 16, 255):
            directory = self.root / f"par2-{count}"
            directory.mkdir()
            index = directory / "recovery.par2"
            with patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()):
                par2.create_tree(
                    self.source,
                    index,
                    block_size=sizing.block_size,
                    recovery_blocks=count,
                    base_dir=self.source.parent,
                )
            volume = next(directory.glob("recovery.vol*.par2"))
            for actual, bound in zip(
                (index.stat().st_size, volume.stat().st_size), sizing.file_sizes(count), strict=True
            ):
                self.assertLessEqual(actual, bound)
                self.assertLess(bound - actual, 4096)

    def test_capacity_checks_do_not_publish_oversized_image(self):
        self.create()
        size = (self.output / "images/disc_0001.iso").stat().st_size
        self.output = self.root / "too-small-with-par2"
        result = self.create("-b", str(size - 2048), expected=1)
        self.assertIn("exceeds disc capacity", result.stdout + result.stderr)
        self.assertIn("-m dar", result.stdout + result.stderr)
        self.assertFalse(list(self.output.rglob("disc_*.iso")))
        self.assertFalse((self.output / ".bd-archive-work").exists())
        self.output = self.root / "too-small-for-source"
        result = self.create("-b", "2048", expected=1)
        self.assertIn("even without PAR2", result.stdout + result.stderr)
        self.assertIn("-m dar", result.stdout + result.stderr)
        self.assertFalse(self.output.exists())

    def test_source_changes_prevent_publication(self):
        from bd_archive.tools import par2

        create_tree = par2.create_tree

        def change_source(*args, **kwargs):
            create_tree(*args, **kwargs)
            (self.source / self.movie).write_bytes(b"changed")

        args = build_parser().parse_args(
            [
                "create",
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
