import argparse
import contextlib
import errno
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from bd_archive.archive.raw import raw_par2_sizing, scan_raw_source, write_raw_checksums
from bd_archive.archive.sizing import disc_write_bytes
from bd_archive.cli import build_parser
from bd_archive.commands.burn import _burn_one_disc
from bd_archive.commands.create import cmd_create
from bd_archive.constants import DISC_END_MARGIN, RAW_ROOT_MARKER, MiB
from bd_archive.tools.burn_timeout import DEFAULT_WRITE_TIMEOUT
from bd_archive.tools.growisofs import DEFAULT_RING_BUFFER


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
            self.assertEqual(args.redundancy.label, "5%")
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
        self.assertEqual(args.redundancy.label, "5%")
        self.assertEqual(args.compression, "zstd")

    def test_attachments_are_validated_before_tools(self):
        info = self.root / "Info.txt"
        info.write_text("info")
        (self.source / "inside.txt").write_text("inside")
        (self.root / "link").symlink_to(info)
        (self.root / "folder").mkdir()
        (self.root / "folder/nested.txt").write_text("nested")
        nested = self.root / "folder/nested.txt"
        cases = [
            (["--attach", str(self.source / "inside.txt")], "outside the source"),
            (["--attach", str(self.source)], "outside the source"),
            (["--attach", str(self.root)], "outside the source"),
            (["--attach", str(self.root / "link")], "symlinks"),
            (["--attach", str(self.root / "missing")], "does not exist"),
            (["--attach", str(info), "--attach", str(info)], "must not overlap"),
            (["--attach", str(self.root / "folder"), "--attach", str(nested)], "must not overlap"),
        ]
        for name in (
            "README.txt",
            "CHECKSUMS.SHA512",
            "recovery.par2",
            "recovery.vol000+001.par2",
            RAW_ROOT_MARKER,
            ".BD-ARCHIVE",
            "media",
        ):
            reserved = self.root / "reserved" / name
            reserved.parent.mkdir(exist_ok=True)
            reserved.write_text("reserved")
            cases.append((["--attach", str(reserved)], "reserved at the disc root"))
        other = self.root / "other"
        other.mkdir()
        (other / "info.txt").write_text("same name")
        cases.append((["--attach", str(info), "--attach", str(other / "info.txt")], "unique"))
        for options, message in cases:
            messages = io.StringIO()
            with (
                self.subTest(options=options),
                patch("bd_archive.commands.create_raw.check_deps") as deps,
                contextlib.redirect_stdout(messages),
                contextlib.redirect_stderr(messages),
                self.assertRaises(SystemExit),
            ):
                cmd_create(self.args(*options))
            self.assertIn(message, messages.getvalue())
            deps.assert_not_called()

    def test_metadata_names_inside_source_are_ordinary_payload(self):
        (self.source / ".bd-archive").mkdir()
        (self.source / ".bd-archive/raw-v1").touch()
        for name in ("README.txt", "checksums.sha512", "recovery.par2", RAW_ROOT_MARKER):
            (self.source / name).touch()
        entries = {entry.path for entry in scan_raw_source(self.source)}
        self.assertIn(".bd-archive/raw-v1", entries)
        self.assertIn("recovery.par2", entries)

    def test_manifest_keeps_disc_relative_folder_names_and_backslashes(self):
        from bd_archive.archive.checksums import verify_manifest

        folder = self.root / "disc" / "source = [Grüße] \\ media"
        folder.parent.mkdir()
        self.source.rename(folder)
        manifest = self.root / "checksums.sha512"
        inventory = scan_raw_source(folder.parent)
        write_raw_checksums(folder.parent, inventory, manifest, placeholder=True)
        size = manifest.stat().st_size
        write_raw_checksums(folder.parent, inventory, manifest)
        self.assertEqual(size, manifest.stat().st_size)
        verify_manifest(manifest, folder.parent, expected_files={folder / "video.mkv"})
        if shutil.which("sha512sum"):
            result = subprocess.run(
                ["sha512sum", "-c", str(manifest)], cwd=folder.parent, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_known_digests_are_reused_instead_of_reading_files_again(self):
        manifest = self.root / "checksums.sha512"
        inventory = scan_raw_source(self.source)
        digest = hashlib.sha512(b"video").hexdigest()
        with patch("bd_archive.archive.raw._hash_file_sha512") as hash_file:
            write_raw_checksums(self.source, inventory, manifest, digests={"video.mkv": digest})
        hash_file.assert_not_called()
        self.assertEqual(manifest.read_text(), f"{digest}  video.mkv\n")

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

    def test_declining_preview_does_not_hash_create_recovery_or_change_source(self):
        before = scan_raw_source(self.source)
        args = self.args()
        args.yes = False
        with (
            patch("bd_archive.commands.create_raw.check_deps"),
            patch("bd_archive.commands.create_raw.software_info", return_value=""),
            patch("bd_archive.tools.mkisofs.estimate_size", return_value=100),
            patch("bd_archive.commands.create_raw.prompt_yn", return_value=False) as prompt,
            patch("bd_archive.commands.create_raw.write_readme") as readme,
            patch("bd_archive.archive.raw._hash_file_sha512") as hash_file,
            patch("bd_archive.commands.create_raw.par2.create_disc") as recovery,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cmd_create(args)
        prompt.assert_called_once_with("Create this disc?", default_yes=False)
        readme.assert_not_called()
        hash_file.assert_not_called()
        recovery.assert_not_called()
        self.assertFalse((self.root / "output").exists())
        self.assertEqual(scan_raw_source(self.source), before)

    def test_single_disc_allows_unused_space_but_never_overflow(self):
        iso = self.root / "disc_0001.iso"
        iso.write_bytes(b"x" * 100)
        args = argparse.Namespace(
            skip_fit_check=False,
            no_verify=True,
            speed=None,
            write_timeout=DEFAULT_WRITE_TIMEOUT,
            buffer=DEFAULT_RING_BUFFER,
        )
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
                    drive.burn.assert_called_once_with(
                        iso,
                        None,
                        write_timeout=DEFAULT_WRITE_TIMEOUT,
                        ring_buffer=DEFAULT_RING_BUFFER,
                    )
                else:
                    with self.assertRaises(SystemExit):
                        _burn_one_disc(args, self.root, iso, 1, count, drive, 32768)
                    drive.burn.assert_not_called()


def unchanged(entries):
    """Scan entries without ctime, which hardlinking changes."""
    return [replace(e, ctime_ns=0) for e in entries]


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
        self.info = self.root / "Info [disc].txt"
        self.info.write_text("Attached to every disc")
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

    def args(self, *options):
        return build_parser().parse_args(
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
                *options,
            ]
        )

    def unpack(self, iso, name="restored"):
        restored = self.root / name
        restored.mkdir()
        subprocess.run([shutil.which("bsdtar"), "-xf", str(iso), "-C", str(restored)], check=True)
        # bsdtar's UDF reader restores optical-media read-only modes.
        # Repair needs a writable copy.
        for target in restored.rglob("*"):
            target.chmod(0o755 if target.is_dir() else 0o644)
        return restored

    def test_roundtrip_verify_damage_and_repair_without_dar(self):
        before = scan_raw_source(self.source)
        self.create("--attach", self.info)
        self.assertFalse((self.output / ".bd-archive-work").exists())
        self.assertEqual(list(self.output.iterdir()), [self.output / "images"])
        self.assertEqual(unchanged(scan_raw_source(self.source)), unchanged(before))
        restored = self.unpack(self.output / "images/disc_0001.iso")
        self.assertEqual(
            {p.name for p in restored.iterdir()},
            {"Media", self.info.name, "README.txt", ".bd-archive"},
        )
        metadata = restored / ".bd-archive"
        self.assertEqual(
            {p.name for p in metadata.iterdir()},
            {
                "checksums.sha512",
                "recovery.par2",
                "raw-v3",
                *[p.name for p in metadata.glob("recovery.vol*.par2")],
            },
        )
        self.assertEqual(len(list(metadata.glob("recovery.vol*.par2"))), 1)
        readme = (restored / "README.txt").read_text()
        self.assertIn("ARCHIVE NAME: Media", readme)
        self.assertIn(
            "FILES:\n"
            "  Media/           Archived files\n"
            f"  {self.info.name}  Attached file\n"
            "  .bd-archive/     Checksums and recovery files; hidden folder, "
            "file managers may not show names starting with a dot\n",
            readme,
        )
        self.assertNotIn("par2 repair", readme)
        self.assertIn("Index:      .bd-archive/recovery.par2", readme)
        self.assertIn("File:      .bd-archive/checksums.sha512", readme)
        self.assertEqual((restored / self.info.name).read_text(), "Attached to every disc")
        for original in self.source.rglob("*"):
            target = restored / "Media" / original.relative_to(self.source)
            if original.is_dir():
                self.assertTrue(target.is_dir())
            else:
                self.assertEqual(target.read_bytes(), original.read_bytes())
        self.cli("verify", restored)
        if shutil.which("sha512sum"):
            hashes = subprocess.run(
                ["sha512sum", "-c", ".bd-archive/checksums.sha512"],
                cwd=restored,
                capture_output=True,
            )
            self.assertEqual(hashes.returncode, 0, hashes.stdout + hashes.stderr)
        with (restored / "Media" / self.movie).open("r+b") as damaged:
            damaged.write(b"!" * 64)
        (restored / "Media" / ".hidden").unlink()
        (restored / self.info.name).write_text("Damaged attachment")
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
            (restored / "Media" / self.movie).read_bytes(),
            (self.source / self.movie).read_bytes(),
        )
        self.assertEqual((restored / self.info.name).read_text(), "Attached to every disc")
        self.cli("verify", restored)
        # Remove repair backups, otherwise par2 can recover from those.
        for backup in restored.rglob("*.1"):
            backup.unlink()
        (restored / "Media" / self.movie).unlink()
        self.cli("verify", restored, expected=2)
        (metadata / "recovery.par2").unlink()
        self.cli("verify", restored, expected=2)

    def redundancy(self, iso, name):
        readme = (self.unpack(iso, name) / "README.txt").read_text()
        match = re.search(r"Redundancy: ([\d.]+)%", readme)
        return None if match is None else float(match[1])

    def test_redundancy_is_exact_unless_free_space_is_filled(self):
        result = self.create(auto=True)
        self.assertRegex(
            result.stdout,
            r"Redundancy\s+: 5% of the data \(fixed\)\n.*Fill free space\s+: disabled\n",
        )
        iso = self.output / "images/disc_0001.iso"
        self.assertLess(iso.stat().st_size, 2_000_000)
        exact = self.redundancy(iso, "exact")
        self.assertGreaterEqual(exact, 5)
        self.assertLess(exact, 7)
        for minimum in ("5", "0"):
            self.output = self.root / f"fill-{minimum}"
            result = self.create("-r", minimum, "--fill-recovery", auto=True)
            self.assertRegex(
                result.stdout,
                rf"Redundancy\s+: {minimum}% of the data \(fixed\)\n"
                r".*Fill free space\s+: enabled \(--fill-recovery\)\n",
            )
            iso = self.output / "images/disc_0001.iso"
            self.assertGreater(iso.stat().st_size, 8_900_000)
            self.assertLessEqual(disc_write_bytes(iso.stat().st_size), 10_000_000 - DISC_END_MARGIN)
            # The preview shows exactly the recovery that creation writes.
            shares = [
                float(share)
                for share in re.findall(
                    r"Recovery \((?:fixed|fill)\)\s+: .*\(([\d.]+)%\)", result.stdout
                )
            ]
            self.assertEqual(len(shares), 6)
            created = self.redundancy(iso, f"filled-{minimum}")
            self.assertAlmostEqual(sum(shares[: len(shares) // 3]), created, delta=0.011)
            total = re.search(r"Recovery \(total\)\s+: .*\(([\d.]+)%\)", result.stdout)
            self.assertAlmostEqual(float(total[1]), created, delta=0.006)
            self.assertGreater(created, 5)

    def test_fill_recovery_cannot_be_combined_with_none(self):
        result = self.create("-r", "none", "--fill-recovery", auto=True, expected=1)
        self.assertIn("cannot be combined with -r none", result.stdout + result.stderr)
        self.assertFalse(self.output.exists())

    def test_nearly_full_disc_needs_room_for_minimum_recovery(self):
        with (self.source / self.movie).open("wb") as movie:
            movie.truncate(64 * MiB)
        before = scan_raw_source(self.source)
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
        result = self.create("-b", capacity, auto=True, expected=1)
        self.assertIn("Urlaub [2024]", result.stdout + result.stderr)
        self.assertIn("smaller -r", result.stdout + result.stderr)
        self.assertIn("-m dar", result.stdout + result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(scan_raw_source(self.source), before)
        # Without recovery data, the same disc fits. Filling the little free
        # space left would add too little recovery data and is skipped.
        for options in (["-r", "none"], ["-r", "0", "--fill-recovery"]):
            self.output = self.root / f"fits-{options[1]}"
            result = self.create("-b", capacity, *options)
            iso = self.output / "images/disc_0001.iso"
            self.assertLessEqual(disc_write_bytes(iso.stat().st_size), capacity - DISC_END_MARGIN)
            self.assertGreater(disc_write_bytes(iso.stat().st_size), capacity - 2 * MiB)
            readme = (self.unpack(iso, f"readme-{options[1]}") / "README.txt").read_text()
            self.assertIn("RECOVERY:     None", readme)
        self.assertRegex(
            result.stdout,
            r"Filling free space would add only .* recovery data "
            r"\(below 0\.5% of the disc's data\); skipped\.",
        )

    def test_par2_size_model_bounds_real_packet_sizes(self):
        from bd_archive.tools import par2

        inventory = [
            replace(e, path=f"{self.source.name}/{e.path}") for e in scan_raw_source(self.source)
        ]
        sizing = raw_par2_sizing(inventory, 10_000_000, 9_000_000)
        for count in (1, 2, 3, 16, 255):
            directory = self.root / f"par2-{count}"
            directory.mkdir()
            index = directory / "recovery.par2"
            with patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()):
                par2.create_disc(
                    self.source.parent,
                    index,
                    [self.source],
                    block_size=sizing.block_size,
                    recovery_blocks=count,
                )
            volume = next(directory.glob("recovery.vol*.par2"))
            for actual, bound in zip(
                (index.stat().st_size, volume.stat().st_size), sizing.file_sizes(count), strict=True
            ):
                self.assertLessEqual(actual, bound)
                self.assertLess(bound - actual, 4096)

    def test_too_small_disc_creates_nothing(self):
        result = self.create("-b", "2048", expected=1)
        self.assertIn("Disc capacity is too small", result.stdout + result.stderr)
        self.assertFalse(self.output.exists())

    def test_disc_changes_during_creation_prevent_publication(self):
        from bd_archive.tools import par2

        create_disc = par2.create_disc

        def change_disc(root, *args, **kwargs):
            create_disc(root, *args, **kwargs)
            # With hardlinks this is the source file as well.
            (root / "Media" / self.movie).write_bytes(b"changed")

        for iso in (False, True):
            self.output = self.root / f"output-{iso}"
            with (
                self.subTest(iso=iso),
                patch("bd_archive.commands.create_raw.par2.create_disc", side_effect=change_disc),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                cmd_create(self.args("-y", *(["--iso"] if iso else [])))
            self.assertFalse(self.output.exists())
            # Restore the content for the next run; that run scans the source again.
            (self.source / self.movie).write_bytes(bytes(range(256)) * 256)

    def test_failure_after_staging_keeps_source_and_removes_output(self):
        before = scan_raw_source(self.source)
        with (
            patch(
                "bd_archive.commands.create_raw.par2.create_disc",
                side_effect=subprocess.CalledProcessError(1, "par2"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            cmd_create(self.args("-y"))
        self.assertFalse(self.output.exists())
        self.assertEqual(unchanged(scan_raw_source(self.source)), unchanged(before))

    def test_copies_reuse_their_verification_hashes(self):
        with (
            patch("bd_archive.archive.staging.reflink.try_clone", return_value=False),
            patch("bd_archive.archive.staging.os.link", side_effect=OSError(errno.EXDEV, "xdev")),
            patch("bd_archive.archive.raw._hash_file_sha512") as hash_file,
            contextlib.redirect_stdout(io.StringIO()) as messages,
        ):
            cmd_create(self.args("-y", "--attach", str(self.info)))
        hash_file.assert_not_called()
        self.assertIn("reflinked 0 B; hardlinked 0 B; copied", messages.getvalue())
        disc = self.output / "disc"
        self.assertNotEqual(
            (disc / "Media" / self.movie).stat().st_ino, (self.source / self.movie).stat().st_ino
        )
        self.cli("verify", disc)

    def test_plans_one_disc_and_removes_only_confirmed_units(self):
        for name in ("a", "b", "c"):
            (self.source / name).mkdir()
            (self.source / name / "data").write_bytes(os.urandom(3_000_000))
        before = scan_raw_source(self.source)
        args = self.args("--attach", str(self.info))
        with (
            patch("bd_archive.commands.create_raw.styled_input", return_value=""),
            patch("bd_archive.commands.create_raw.prompt_yn", side_effect=[True, False]),
            contextlib.redirect_stdout(io.StringIO()) as messages,
        ):
            cmd_create(args)
        self.assertIn(
            "Source unchanged: the next run plans these units again.", messages.getvalue()
        )
        self.assertEqual(unchanged(scan_raw_source(self.source)), unchanged(before))
        disc = self.output / "disc"
        archived = {p.name for p in (disc / "Media").iterdir()}
        self.assertTrue(archived)
        self.assertLess(len(archived), len(list(self.source.iterdir())))
        self.cli("verify", disc)

        self.output = self.root / "second"
        with (
            patch("bd_archive.commands.create_raw.styled_input", return_value=""),
            patch("bd_archive.commands.create_raw.prompt_yn", side_effect=[True, True]),
            contextlib.redirect_stdout(io.StringIO()) as messages,
        ):
            cmd_create(self.args("--attach", str(self.info)))
        disc = self.output / "disc"
        archived = {p.name for p in (disc / "Media").iterdir()}
        self.assertIn(
            f"Removed {len(archived)} archived units from the source", messages.getvalue()
        )
        remaining = {p.name for p in self.source.iterdir()}
        self.assertFalse(archived & remaining)
        self.assertEqual(archived | remaining, {Path(e.path).parts[0] for e in before})
        self.assertTrue(self.info.exists())
        self.assertEqual((disc / self.info.name).read_text(), "Attached to every disc")
        self.cli("verify", disc)


if __name__ == "__main__":
    unittest.main()
