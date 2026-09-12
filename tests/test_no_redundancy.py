"""Creation without PAR2 must retain payloads, checksums and capacity checks."""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bd_archive.cli import build_parser


class RedundancyParsingTests(unittest.TestCase):
    def test_disable_aliases_in_both_modes(self):
        for mode in ("raw", "dar"):
            for value in ("0", "none", "NONE"):
                with self.subTest(mode=mode, value=value):
                    args = build_parser().parse_args(
                        ["create", "-s", ".", "-n", "Test", "-o", "out", "-m", mode, "-r", value]
                    )
                    self.assertEqual(args.redundancy, 0)

    def test_invalid_text_is_an_argument_error(self):
        for value in ("off", "1.5"):
            with (
                self.subTest(value=value),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as exc,
            ):
                build_parser().parse_args(
                    ["create", "-s", ".", "-n", "Test", "-o", "out", "-r", value]
                )
            self.assertEqual(exc.exception.code, 2)


@unittest.skipUnless(
    all(shutil.which(tool) for tool in ("mkisofs", "bsdtar", "sha512sum")),
    "requires mkisofs, bsdtar and sha512sum",
)
class NoRedundancyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bd-no-recovery-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "payload").write_bytes(bytes(range(256)) * 256)
        (self.source / "empty").touch()
        (self.source / "empty directory").mkdir()
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        # Some DAR builds initialize GPGME even for unencrypted archives.
        for tool in ("mkisofs", "dar", "dvd+rw-mediainfo", "gpg", "gpgconf"):
            if executable := shutil.which(tool):
                (self.bindir / tool).symlink_to(executable)
        self.env = {**os.environ, "PATH": str(self.bindir)}

    def create(self, mode, value, capacity=10_000_000, expected=0):
        output = self.root / f"{mode}-{value}"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "bd_archive",
                "create",
                "-s",
                str(self.source),
                "-n",
                "Test",
                "-o",
                str(output),
                "-m",
                mode,
                "-r",
                value,
                "-b",
                str(capacity),
                "-c",
                "none",
                "-y",
            ],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return output, result.stdout

    def unpack(self, output, *, udf=False):
        restored = output / "unpacked"
        restored.mkdir()
        # DAR uses UDF for original filenames; bsdtar reads its ISO9660
        # bridge instead. Raw images also carry Rock Ridge names.
        command = (
            [
                shutil.which("7z"),
                "x",
                "-tudf",
                f"-o{restored}",
                str(output / "images/disc_0001.iso"),
            ]
            if udf
            else [
                shutil.which("bsdtar"),
                "-xf",
                str(output / "images/disc_0001.iso"),
                "-C",
                str(restored),
            ]
        )
        subprocess.run(command, check=True, capture_output=True)
        self.assertFalse(list(restored.rglob("*.par2")))
        self.assertFalse((output / ".bd-archive-work").exists())
        return restored

    def check_hashes(self, manifest, cwd):
        result = subprocess.run(
            [shutil.which("sha512sum"), "-c", str(manifest)],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def verify(self, target, expected=0):
        result = subprocess.run(
            [sys.executable, "-m", "bd_archive", "verify", str(target)],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def assert_payload(self, restored):
        for original in self.source.iterdir():
            target = restored / original.name
            if original.is_dir():
                self.assertTrue(target.is_dir())
            else:
                self.assertEqual(target.read_bytes(), original.read_bytes())

    def test_raw_both_aliases_without_par2(self):
        for value in ("0", "none"):
            with self.subTest(value=value):
                output, messages = self.create("raw", value)
                self.assertNotIn("--no-verify", messages)
                restored = self.unpack(output)
                self.verify(restored)
                self.assert_payload(restored / self.source.name)
                self.check_hashes(restored / "checksums.sha512", restored)
                readme = (restored / "README.txt").read_text()
                self.assertIn("PAR2 is disabled", readme)
                self.assertNotIn("par2 repair", readme)
                (restored / self.source.name / "payload").chmod(0o600)
                (restored / self.source.name / "payload").write_bytes(b"damaged")
                self.verify(restored, expected=2)

    def test_raw_empty_files_need_no_recovery_blocks(self):
        (self.source / "payload").write_bytes(b"")
        output, _ = self.create("raw", "0")
        restored = self.unpack(output)
        self.assert_payload(restored / self.source.name)
        self.check_hashes(restored / "checksums.sha512", restored)

    def test_raw_capacity_limit_still_applies(self):
        output, _ = self.create("raw", "none", capacity=1000, expected=1)
        self.assertFalse(list(output.rglob("disc_*.iso")))

    @unittest.skipUnless(
        all(shutil.which(tool) for tool in ("dar", "dvd+rw-mediainfo", "7z")),
        "requires dar, dvd+rw-mediainfo and 7z for UDF extraction",
    )
    def test_dar_both_aliases_without_par2(self):
        for value in ("0", "none"):
            with self.subTest(value=value):
                output, messages = self.create("dar", value)
                self.assertNotIn("--no-verify", messages)
                archive = self.unpack(output, udf=True) / "Test-gen1"
                self.verify(archive.parent)
                manifests = list(archive.glob("*.sha512"))
                self.assertGreaterEqual(len(manifests), 2, list(archive.parent.rglob("*")))
                for manifest in manifests:
                    self.check_hashes(manifest, archive)
                readme = (archive / "README.txt").read_text()
                self.assertIn("PAR2 disabled", readme)
                self.assertNotIn("par2 repair", readme)
                restored = output / "restored"
                restored.mkdir()
                result = subprocess.run(
                    [
                        shutil.which("dar"),
                        "-x",
                        str(archive / "Test-gen1"),
                        "-R",
                        str(restored),
                        "-O",
                        "-Q",
                    ],
                    env=self.env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_payload(restored)
                (archive / "Test-gen1.0001.dar").write_bytes(b"damaged")
                self.verify(archive.parent, expected=2)
