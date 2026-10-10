"""burn --verify-device and --no-close-tray: udev label lookup and drive selection."""

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bd_archive.archive import disc
from bd_archive.cli import build_parser
from bd_archive.commands import burn
from bd_archive.tools import eject as eject_tool
from bd_archive.tools import udev
from bd_archive.tools.burn_timeout import DEFAULT_WRITE_TIMEOUT
from bd_archive.tools.growisofs import DEFAULT_RING_BUFFER


class FakeUdev:
    """A temporary sysfs + udev database with one entry per drive."""

    def __init__(self, root: Path):
        self.sys = root / "sys"
        self.data = root / "data"
        self.data.mkdir(parents=True)

    def set(self, name: str, minor: int, props: dict[str, str] | None):
        (self.sys / name).mkdir(parents=True, exist_ok=True)
        (self.sys / name / "dev").write_text(f"11:{minor}\n")
        record = self.data / f"b11:{minor}"
        if props is None:
            record.unlink(missing_ok=True)
            return
        record.write_text("".join(f"E:{key}={value}\n" for key, value in props.items()))

    def patch(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(udev, "SYS_BLOCK_DIR", self.sys))
        stack.enter_context(patch.object(udev, "UDEV_DATA_DIR", self.data))
        return stack


def medium(label: str) -> dict[str, str]:
    return {"ID_CDROM_MEDIA": "1", "ID_FS_LABEL": label, "ID_FS_LABEL_ENC": label}


class UdevTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.udev = FakeUdev(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_disc_label_requires_a_medium_and_decodes_escapes(self):
        self.udev.set("sr0", 0, medium("Archive_G01_0001"))
        self.udev.set("sr1", 1, {"ID_CDROM_MEDIA": "1", "ID_FS_LABEL_ENC": "a\\x20b"})
        self.udev.set("sr2", 2, {"ID_FS_LABEL": "stale"})
        self.udev.set("sr3", 3, None)
        with self.udev.patch():
            self.assertEqual(udev.disc_label("/dev/sr0"), "Archive_G01_0001")
            self.assertEqual(udev.disc_label("/dev/sr1"), "a b")
            self.assertIsNone(udev.disc_label("/dev/sr2"))
            self.assertIsNone(udev.disc_label("/dev/sr3"))
            self.assertIsNone(udev.disc_label("/dev/sr9"))

    def test_iso_volume_label_reads_the_primary_volume_descriptor(self):
        iso = Path(self.tmp.name) / "disc_0001.iso"
        pvd = bytearray(2048)
        pvd[0] = 1
        pvd[1:6] = b"CD001"
        pvd[40:72] = b"Archive_G01_0001".ljust(32)
        iso.write_bytes(bytes(16 * 2048) + bytes(pvd))
        self.assertEqual(disc.iso_volume_label(iso), "Archive_G01_0001")
        iso.write_bytes(bytes(17 * 2048))
        self.assertIsNone(disc.iso_volume_label(iso))


class WaitForLabelledDiscTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.udev = FakeUdev(Path(self.tmp.name))
        self.udev.set("sr0", 0, {"ID_CDROM_MEDIA": "1"})
        self.udev.set("sr1", 1, medium("Archive_G01_0001"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_wait(self, steps, close_device=None, ignored=frozenset()):
        """Apply one udev change per poll; return (found drive, closed drives)."""
        steps = list(steps)
        closed = []

        def sleep(_seconds):
            if steps:
                steps.pop(0)()

        with (
            self.udev.patch(),
            patch.object(disc.time, "sleep", side_effect=sleep),
            patch.object(disc.eject_tool, "drive_status", return_value=eject_tool.CDS_DISC_OK),
            patch.object(disc.eject_tool, "close_tray", side_effect=closed.append),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            found = disc.wait_for_labelled_disc(
                ["/dev/sr0", "/dev/sr1"], "archive_g01_0001", set(ignored), close_device
            )
        return found, closed

    def test_stale_drive_counts_only_after_its_disc_was_replaced(self):
        found, closed = self.run_wait(
            [
                lambda: self.udev.set("sr1", 1, {}),
                lambda: self.udev.set("sr1", 1, medium("Archive_G01_0001")),
            ],
            ignored={"/dev/sr1"},
        )
        self.assertEqual(found, "/dev/sr1")
        self.assertEqual(closed, [])

    def test_burner_disc_is_found_and_only_the_burner_tray_closes(self):
        found, closed = self.run_wait(
            [lambda: self.udev.set("sr0", 0, medium("Archive_G01_0001"))],
            close_device="/dev/sr0",
            ignored={"/dev/sr1"},
        )
        self.assertEqual(found, "/dev/sr0")
        self.assertEqual(set(closed), {"/dev/sr0"})


class BurnVerifyDeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.iso = self.root / "disc_0001.iso"
        self.iso.write_bytes(b"x" * 100)

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, no_close_tray=False):
        return argparse.Namespace(
            skip_fit_check=True,
            no_verify=False,
            no_close_tray=no_close_tray,
            speed=None,
            write_timeout=DEFAULT_WRITE_TIMEOUT,
            buffer=DEFAULT_RING_BUFFER,
        )

    def burn(self, args, verify_device, **patches):
        burner = Mock(device="/dev/sr0")
        burner.mount_with_retry.return_value = (self.root, "")
        verifier = Mock(device="/dev/sr1")
        verifier.mount_with_retry.return_value = (self.root, "")
        with (
            patch("bd_archive.commands.burn.prompt_disc"),
            patch("bd_archive.commands.burn.time.sleep"),
            patch("bd_archive.commands.burn.DiscIO", return_value=verifier) as disc_io,
            patch("bd_archive.commands.burn.verify_disc", return_value=burn.VerifyResult.OK),
            contextlib.ExitStack() as stack,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            for target, value in patches.items():
                stack.enter_context(patch(f"bd_archive.commands.burn.{target}", value))
            burn._burn_one_disc(args, self.root, self.iso, 1, 1, burner, 100, verify_device)
        return burner, verifier, disc_io

    def test_default_verifies_in_the_burner_and_honours_no_close_tray(self):
        for no_close_tray in (False, True):
            with self.subTest(no_close_tray=no_close_tray):
                burner, verifier, _ = self.burn(self.args(no_close_tray), None)
                burner.wait_for_disc_ready.assert_called_once_with(close_tray=not no_close_tray)
                burner.mount_with_retry.assert_called_once()
                burner.eject.assert_called_once()
                verifier.mount_with_retry.assert_not_called()

    def test_separate_verify_device_waits_passively_and_leaves_the_burner_alone(self):
        burner, verifier, disc_io = self.burn(self.args(), "/dev/sr1")
        disc_io.assert_called_once_with("/dev/sr1")
        verifier.wait_for_disc_ready.assert_called_once_with(close_tray=False)
        verifier.mount_with_retry.assert_called_once()
        verifier.eject.assert_called_once()
        burner.wait_for_disc_ready.assert_not_called()
        burner.mount_with_retry.assert_not_called()
        burner.eject.assert_not_called()

    def test_auto_verifies_in_the_drive_that_received_the_labelled_disc(self):
        wait = Mock(return_value="/dev/sr1")
        burner, verifier, _ = self.burn(
            self.args(),
            burn.VERIFY_DEVICE_AUTO,
            iso_volume_label=Mock(return_value="Archive_G01_0001"),
            list_drives=Mock(return_value=[Mock(path="/dev/sr0"), Mock(path="/dev/sr1")]),
            drives_with_label=Mock(return_value={"/dev/sr1"}),
            wait_for_labelled_disc=wait,
        )
        wait.assert_called_once_with(
            ["/dev/sr0", "/dev/sr1"], "Archive_G01_0001", {"/dev/sr1"}, close_device="/dev/sr0"
        )
        verifier.mount_with_retry.assert_called_once()
        verifier.eject.assert_called_once()
        burner.eject.assert_not_called()

    def test_auto_with_no_close_tray_closes_no_tray(self):
        wait = Mock(return_value="/dev/sr0")
        burner, verifier, _ = self.burn(
            self.args(no_close_tray=True),
            burn.VERIFY_DEVICE_AUTO,
            iso_volume_label=Mock(return_value="Archive_G01_0001"),
            list_drives=Mock(return_value=[Mock(path="/dev/sr0")]),
            drives_with_label=Mock(return_value=set()),
            wait_for_labelled_disc=wait,
        )
        self.assertIsNone(wait.call_args.kwargs["close_device"])
        burner.mount_with_retry.assert_called_once()
        verifier.mount_with_retry.assert_not_called()


class BurnOptionTests(unittest.TestCase):
    def test_cli_defaults_and_values(self):
        parser = build_parser()
        args = parser.parse_args(["burn", "-i", "out"])
        self.assertIsNone(args.verify_device)
        self.assertFalse(args.no_close_tray)
        args = parser.parse_args(
            ["burn", "-i", "out", "--verify-device", "auto", "--no-close-tray"]
        )
        self.assertEqual(args.verify_device, "auto")
        self.assertTrue(args.no_close_tray)

    def test_verify_options_require_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            images = Path(tmp) / "images"
            images.mkdir()
            (images / "disc_0001.iso").write_bytes(b"x")
            for extra in (["--verify-device", "auto"], ["--no-close-tray"]):
                args = build_parser().parse_args(["burn", "-i", tmp, "--no-verify", *extra])
                with (
                    self.subTest(extra=extra),
                    patch("bd_archive.commands.burn.check_deps"),
                    patch("bd_archive.commands.burn.resolve_device") as resolve,
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    burn.cmd_burn(args)
                resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
