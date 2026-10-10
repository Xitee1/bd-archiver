"""Post-burn disc detection in any drive: udev label lookup, burner label
reads and the --no-close-tray option."""

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

LABEL = "Archive_G01_0001"


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


def write_iso(path: Path, label: str):
    pvd = bytearray(2048)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[40:72] = label.encode().ljust(32)
    path.write_bytes(bytes(16 * 2048) + bytes(pvd))


class LabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.udev = FakeUdev(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_udev_label_requires_a_medium_and_decodes_escapes(self):
        self.udev.set("sr0", 0, medium(LABEL))
        self.udev.set("sr1", 1, {"ID_CDROM_MEDIA": "1", "ID_FS_LABEL_ENC": "a\\x20b"})
        self.udev.set("sr2", 2, {"ID_FS_LABEL": "stale"})
        self.udev.set("sr3", 3, None)
        with self.udev.patch():
            self.assertEqual(udev.disc_label("/dev/sr0"), LABEL)
            self.assertEqual(udev.disc_label("/dev/sr1"), "a b")
            self.assertIsNone(udev.disc_label("/dev/sr2"))
            self.assertIsNone(udev.disc_label("/dev/sr3"))
            self.assertIsNone(udev.disc_label("/dev/sr9"))

    def test_iso_volume_label_reads_the_primary_volume_descriptor(self):
        iso = self.root / "disc_0001.iso"
        write_iso(iso, LABEL)
        self.assertEqual(disc.iso_volume_label(iso), LABEL)
        iso.write_bytes(bytes(17 * 2048))
        self.assertIsNone(disc.iso_volume_label(iso))
        self.assertIsNone(disc.iso_volume_label(self.root / "missing"))


class WaitForBurnedDiscTests(unittest.TestCase):
    """The burner is simulated by a file whose PVD the test rewrites."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.udev = FakeUdev(self.root)
        self.udev.set("sr1", 1, medium(LABEL))
        self.burner = self.root / "burner"
        self.burner.write_bytes(b"")
        self.burner_status = eject_tool.CDS_TRAY_OPEN

    def tearDown(self):
        self.tmp.cleanup()

    def status(self, device):
        if device == str(self.burner):
            return self.burner_status
        return eject_tool.CDS_DISC_OK

    def run_wait(self, steps, close_tray=False, ignored=frozenset()):
        """Apply one change per poll; return (found drive, closed drives, label reads)."""
        steps = list(steps)
        closed = []
        reads = []
        read_label = disc.iso_volume_label

        def sleep(seconds):
            if seconds == 1 and steps:
                steps.pop(0)()

        def iso_volume_label(path):
            reads.append(path)
            return read_label(path)

        with (
            self.udev.patch(),
            patch.object(disc.time, "sleep", side_effect=sleep),
            patch.object(disc.eject_tool, "drive_status", side_effect=self.status),
            patch.object(disc.eject_tool, "close_tray", side_effect=closed.append),
            patch.object(disc, "iso_volume_label", side_effect=iso_volume_label),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            found = disc.wait_for_burned_disc(
                str(self.burner), ["/dev/sr1"], LABEL.lower(), set(ignored), close_tray
            )
        return found, closed, reads

    def set_burner(self, status, label=None):
        self.burner_status = status
        if label is None:
            self.burner.write_bytes(b"")
        else:
            write_iso(self.burner, label)

    def test_burned_disc_back_in_the_burner_is_found_by_its_label(self):
        found, closed, _ = self.run_wait(
            [lambda: self.set_burner(eject_tool.CDS_DISC_OK, LABEL)],
            close_tray=True,
            ignored={"/dev/sr1"},
        )
        self.assertEqual(found, str(self.burner))
        self.assertEqual(set(closed), {str(self.burner)})

    def test_blank_in_the_burner_is_read_once_and_the_moved_disc_is_found(self):
        found, closed, reads = self.run_wait(
            [
                lambda: self.set_burner(eject_tool.CDS_DISC_OK),
                lambda: None,
                lambda: self.udev.set("sr1", 1, {}),
                lambda: self.udev.set("sr1", 1, medium(LABEL)),
            ],
            ignored={"/dev/sr1"},
        )
        self.assertEqual(found, "/dev/sr1")
        self.assertEqual(reads, [str(self.burner)])
        self.assertEqual(closed, [])

    def test_stale_drive_is_ignored_until_its_disc_was_replaced(self):
        found, _, _ = self.run_wait(
            [
                lambda: None,
                lambda: self.set_burner(eject_tool.CDS_DISC_OK, LABEL),
            ],
            ignored={"/dev/sr1"},
        )
        self.assertEqual(found, str(self.burner))


class BurnVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.iso = self.root / "disc_0001.iso"
        write_iso(self.iso, LABEL)

    def tearDown(self):
        self.tmp.cleanup()

    def burn(self, found, no_close_tray=False):
        args = argparse.Namespace(
            skip_fit_check=True,
            no_verify=False,
            no_close_tray=no_close_tray,
            speed=None,
            write_timeout=DEFAULT_WRITE_TIMEOUT,
            buffer=DEFAULT_RING_BUFFER,
        )
        burner = Mock(device="/dev/sr0")
        burner.mount_with_retry.return_value = (self.root, "")
        other = Mock(device="/dev/sr1")
        other.mount_with_retry.return_value = (self.root, "")
        wait = Mock(return_value=found)
        with (
            patch("bd_archive.commands.burn.prompt_disc"),
            patch("bd_archive.commands.burn.time.sleep"),
            patch("bd_archive.commands.burn.udev.is_available", return_value=True),
            patch(
                "bd_archive.commands.burn.list_drives",
                return_value=[Mock(path="/dev/sr0"), Mock(path="/dev/sr1")],
            ),
            patch("bd_archive.commands.burn.drives_with_label", return_value={"/dev/sr1"}),
            patch("bd_archive.commands.burn.wait_for_burned_disc", wait),
            patch("bd_archive.commands.burn.DiscIO", return_value=other) as disc_io,
            patch("bd_archive.commands.burn.verify_disc", return_value=burn.VerifyResult.OK),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            burn._burn_one_disc(args, self.root, self.iso, 1, 1, burner, 100)
        return wait, burner, other, disc_io

    def test_disc_in_the_burner_is_verified_and_ejected_there(self):
        wait, burner, other, disc_io = self.burn("/dev/sr0")
        wait.assert_called_once_with("/dev/sr0", ["/dev/sr1"], LABEL, {"/dev/sr1"}, close_tray=True)
        burner.mount_with_retry.assert_called_once()
        burner.eject.assert_called_once()
        disc_io.assert_not_called()

    def test_disc_in_another_drive_is_verified_and_ejected_there(self):
        wait, burner, other, disc_io = self.burn("/dev/sr1", no_close_tray=True)
        self.assertFalse(wait.call_args.kwargs["close_tray"])
        disc_io.assert_called_once_with("/dev/sr1")
        other.mount_with_retry.assert_called_once()
        other.eject.assert_called_once()
        burner.mount_with_retry.assert_not_called()
        burner.eject.assert_not_called()


class BurnOptionTests(unittest.TestCase):
    def test_cli_default_and_value(self):
        parser = build_parser()
        self.assertFalse(parser.parse_args(["burn", "-i", "out"]).no_close_tray)
        self.assertTrue(parser.parse_args(["burn", "-i", "out", "--no-close-tray"]).no_close_tray)

    def test_no_close_tray_requires_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            images = Path(tmp) / "images"
            images.mkdir()
            (images / "disc_0001.iso").write_bytes(b"x")
            args = build_parser().parse_args(["burn", "-i", tmp, "--no-verify", "--no-close-tray"])
            with (
                patch("bd_archive.commands.burn.check_deps"),
                patch("bd_archive.commands.burn.resolve_device") as resolve,
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                burn.cmd_burn(args)
            resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
