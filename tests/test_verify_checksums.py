"""SHA-512 fallback and mixed PAR2/SHA-512 verification."""

import argparse
import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bd_archive.archive.raw import scan_raw_source, write_raw_checksums
from bd_archive.archive.verify import verify_disc
from bd_archive.commands.burn import _burn_one_disc
from bd_archive.constants import RAW_MARKER, RAW_METADATA_DIR, RAW_ROOT_MARKER
from bd_archive.tools.par2 import VerifyResult


class ChecksumVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.disc = self.root / "disc"
        self.disc.mkdir()

    def slice(self, name="Test-gen1.0001.dar", directory=None):
        target = (directory or self.disc) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"archive data")
        manifest = Path(str(target) + ".sha512")
        manifest.write_text(f"{hashlib.sha512(target.read_bytes()).hexdigest()}  {target.name}\n")
        return target, manifest

    def verify(self, expected):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(verify_disc(self.disc), expected)

    def raw(self):
        for name in (".hidden", "Grüße [2024]", "back\\slash", "empty", "data.par2"):
            (self.disc / name).write_bytes(b"" if name == "empty" else name.encode())
        inventory = scan_raw_source(self.disc)
        metadata = self.disc / RAW_METADATA_DIR
        metadata.mkdir()
        (metadata / RAW_MARKER).touch()
        manifest = metadata / "checksums.sha512"
        write_raw_checksums(self.disc, inventory, manifest)
        return manifest

    def test_raw_manifest_checks_all_files_and_escaped_names_without_par2(self):
        manifest = self.raw()
        with (
            patch("bd_archive.archive.verify.check_deps") as deps,
            patch("bd_archive.archive.verify.par2.verify") as par2,
        ):
            self.verify(VerifyResult.OK)
        deps.assert_not_called()
        par2.assert_not_called()
        lines = manifest.read_text().splitlines(keepends=True)
        manifest.write_text("".join(lines[:-1]))
        self.verify(VerifyResult.BROKEN)

    def test_raw_missing_file_and_missing_manifest_fail(self):
        manifest = self.raw()
        (self.disc / "empty").unlink()
        self.verify(VerifyResult.BROKEN)
        manifest.unlink()
        self.verify(VerifyResult.BROKEN)

    def raw_v2(self):
        # A source named .bd-archive containing raw-v1 must not be mistaken
        # for the legacy metadata directory; no marker is needed.
        source = self.disc / RAW_METADATA_DIR
        source.mkdir()
        for name in (RAW_MARKER, "README.txt", "checksums.sha512", "recovery.par2", "empty"):
            (source / name).write_bytes(b"" if name == "empty" else name.encode())
        inventory = scan_raw_source(source)
        (self.disc / "README.txt").write_text("Instructions")
        manifest = self.disc / "checksums.sha512"
        write_raw_checksums(source, inventory, manifest, path_prefix=source.name)
        return source, manifest

    def test_v2_checksums_cover_payload_but_not_root_metadata(self):
        source, manifest = self.raw_v2()
        with patch("bd_archive.archive.verify.check_deps") as deps:
            self.verify(VerifyResult.OK)
        deps.assert_not_called()
        # Missing recovery index may leave volumes; they are metadata.
        (self.disc / "recovery.vol000+001.par2").touch()
        self.verify(VerifyResult.OK)
        original = manifest.read_text()
        manifest.write_text("".join(original.splitlines(keepends=True)[:-1]))
        self.verify(VerifyResult.BROKEN)
        manifest.write_text(original)
        (source / "empty").unlink()
        self.verify(VerifyResult.BROKEN)
        manifest.unlink()
        self.verify(VerifyResult.BROKEN)

    def test_old_root_marker_is_optional_metadata(self):
        self.raw_v2()
        (self.disc / RAW_ROOT_MARKER).write_text("bd-archive raw disc format 2\n")
        self.verify(VerifyResult.OK)

    def test_root_par2_without_manifest_or_marker_takes_precedence(self):
        _, manifest = self.raw_v2()
        manifest.unlink()
        index = self.disc / "recovery.par2"
        index.touch()
        for result in VerifyResult:
            with (
                patch("bd_archive.archive.verify.check_deps"),
                patch("bd_archive.archive.verify.par2.verify", return_value=result) as verify,
            ):
                self.verify(result)
            verify.assert_called_once_with(index, base_dir=self.disc)

    def test_legacy_sha512_without_marker(self):
        self.raw()
        (self.disc / RAW_METADATA_DIR / RAW_MARKER).unlink()
        self.verify(VerifyResult.OK)

    def test_both_raw_layouts_verify_only_the_disc_recovery_index(self):
        for v2 in (False, True):
            with self.subTest(v2=v2), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                self.disc = Path(tmp)
                if v2:
                    self.raw_v2()
                    index = self.disc / "recovery.par2"
                else:
                    self.raw()
                    index = self.disc / RAW_METADATA_DIR / "recovery.par2"
                index.touch()
                with (
                    patch("bd_archive.archive.verify.check_deps"),
                    patch(
                        "bd_archive.archive.verify.par2.verify", return_value=VerifyResult.OK
                    ) as verify,
                ):
                    self.verify(VerifyResult.OK)
                verify.assert_called_once_with(index, base_dir=self.disc)

    def test_missing_checksums_or_payload_fail(self):
        self.verify(VerifyResult.BROKEN)
        target, manifest = self.slice()
        self.verify(VerifyResult.OK)
        target.unlink()
        self.verify(VerifyResult.BROKEN)
        target.write_bytes(b"archive data")
        manifest.unlink()
        self.verify(VerifyResult.BROKEN)

    def test_invalid_records_fail(self):
        target, manifest = self.slice()
        valid = manifest.read_text()
        for content in (
            "",
            "\n",
            "not a checksum\n",
            valid + valid,
            valid + "invalid\n",
            "g" * 128 + "  " + target.name + "\n",
        ):
            with self.subTest(content=content):
                manifest.write_text(content)
                self.verify(VerifyResult.BROKEN)

    def test_paths_outside_disc_and_wrong_targets_fail(self):
        target, manifest = self.slice()
        outside = self.root / "outside"
        outside.write_bytes(target.read_bytes())
        (self.disc / "link").symlink_to(outside)
        digest = hashlib.sha512(target.read_bytes()).hexdigest()
        for name in (str(outside), "../outside", "link", "wrong-file"):
            with self.subTest(name=name):
                manifest.write_text(f"{digest}  {name}\n")
                self.verify(VerifyResult.BROKEN)

    def test_par2_takes_precedence_even_when_it_fails(self):
        target, manifest = self.slice()
        index = Path(str(target) + ".par2")
        index.touch()
        manifest.write_text("invalid SHA-512, unused when PAR2 is available\n")
        for result in VerifyResult:
            with (
                patch("bd_archive.archive.verify.check_deps"),
                patch("bd_archive.archive.verify.par2.verify", return_value=result) as par2,
            ):
                self.verify(result)
            par2.assert_called_once_with(index)

    def test_mixed_archives_check_unprotected_slices_and_catalogs(self):
        for foldered in (False, True):
            with self.subTest(foldered=foldered), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                self.disc = Path(tmp)
                target, _ = self.slice(directory=self.disc / "protected" if foldered else None)
                Path(str(target) + ".par2").touch()
                directory = self.disc / "plain" if foldered else self.disc
                self.slice("Other-gen1.0001.dar", directory)
                catalog, _ = self.slice("Other-gen1-catalog.0001.dar", directory)
                with (
                    patch("bd_archive.archive.verify.check_deps"),
                    patch(
                        "bd_archive.archive.verify.par2.verify",
                        return_value=VerifyResult.REPAIRABLE,
                    ),
                ):
                    self.verify(VerifyResult.REPAIRABLE)
                    catalog.write_bytes(b"damaged")
                    self.verify(VerifyResult.BROKEN)

    def test_quiet_success_is_silent(self):
        self.slice()
        messages = io.StringIO()
        with contextlib.redirect_stdout(messages):
            self.assertEqual(verify_disc(self.disc, quiet=True), VerifyResult.OK)
        self.assertEqual(messages.getvalue(), "")

    def test_post_burn_uses_sha512_without_hardware(self):
        self.slice()
        iso = self.root / "disc.iso"
        iso.write_bytes(b"fake ISO")
        drive = Mock(device="/dev/mock")
        drive.mount_with_retry.return_value = (self.disc, None)
        args = argparse.Namespace(
            skip_fit_check=True, no_verify=False, speed=None, write_timeout=600
        )
        with (
            patch("bd_archive.commands.burn.prompt_disc"),
            patch("bd_archive.commands.burn.time.sleep"),
            patch("bd_archive.commands.burn.styled_input", side_effect=AssertionError("retry")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            _burn_one_disc(args, self.root, iso, 1, 1, drive, iso.stat().st_size)
        drive.burn.assert_called_once_with(iso, None, write_timeout=600)
        drive.umount.assert_called_once_with(self.disc)
        drive.eject.assert_called_once()
