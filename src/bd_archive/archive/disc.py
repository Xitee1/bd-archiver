import contextlib
import tempfile
import time
from pathlib import Path

from bd_archive.constants import POST_BURN_MOUNT_TIMEOUT
from bd_archive.tools import eject as eject_tool
from bd_archive.tools import growisofs, mkisofs, udev, udisks
from bd_archive.tools import mount as mount_tool
from bd_archive.tools.burn_timeout import DEFAULT_WRITE_TIMEOUT
from bd_archive.tools.growisofs import DEFAULT_RING_BUFFER
from bd_archive.ui.logger import log

# Close-tray attempt schedule (cumulative seconds from start of wait):
# first attempt fires immediately, subsequent attempts space out 5/10/
# 15/20s after the previous one. Five attempts over ~50s gives slow
# tray-load drives time to actually start moving and absorbs flaky
# motors; after that we fall through to passive polling, leaving the
# user to push a slim-drive disc back in by hand.
_CLOSE_TRAY_SCHEDULE_S = (0, 5, 15, 30, 50)


def _close_tray_when_due(device: str, elapsed: float, attempts_done: int) -> int:
    """Send the close-tray attempts that `_CLOSE_TRAY_SCHEDULE_S` has due by
    `elapsed` seconds and return the new number of attempts done."""
    while (
        attempts_done < len(_CLOSE_TRAY_SCHEDULE_S)
        and elapsed >= _CLOSE_TRAY_SCHEDULE_S[attempts_done]
    ):
        # close_tray is silent on success and no-ops on drives that can't
        # motor the tray, so we don't surface every attempt.
        eject_tool.close_tray(device)
        attempts_done += 1
    return attempts_done


class LoopMountError(RuntimeError):
    """An ISO could not be loop-mounted (loop-setup or mount failed)."""


@contextlib.contextmanager
def loop_mounted(iso_path: Path, prefix: str = "bd-iso-"):
    """Loop-mount an ISO read-only via udisksctl and yield the mount path.

    Lets every ISO-reading code path (verify's `.iso` target, create's
    --pack-with, extract's --iso source) treat an image exactly like a
    mounted disc — same `find_disc_archives` / `verify_disc` logic runs
    on top. Requires `udisksctl` (Polkit, no root needed); callers must
    check_deps for it.

    Raises LoopMountError when loop-setup or the mount fails; the caller
    decides how fatal that is.
    """
    ok, loop_dev, message = udisks.loop_setup(str(iso_path.resolve()))
    if not ok:
        raise LoopMountError(f"loop-setup failed for {iso_path}: {message}")
    assert loop_dev is not None

    time.sleep(0.5)  # let udev settle so the loop device is ready
    dio = DiscIO(loop_dev)
    mount_dir = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        mounted, mount_err = dio.mount(mount_dir)
        if mounted is None:
            raise LoopMountError(
                f"Could not mount {iso_path}" + (f": {mount_err}" if mount_err else "")
            )
        try:
            yield mounted
        finally:
            dio.umount(mounted)
    finally:
        # rmdir in the outer finally so the tempdir is also cleaned up
        # when the mount itself failed.
        with contextlib.suppress(OSError):
            mount_dir.rmdir()
        udisks.loop_delete(loop_dev)


def find_sg_device(block_device: str) -> str | None:
    """Map /dev/srX → /dev/sgY via sysfs. Returns None if not found."""
    name = Path(block_device).name
    sg_dir = Path(f"/sys/block/{name}/device/scsi_generic")
    if sg_dir.is_dir():
        for entry in sg_dir.iterdir():
            return f"/dev/{entry.name}"
    return None


# ISO9660 primary volume descriptor: sector 16, volume identifier at
# bytes 40..71 (ECMA-119 8.4.6).
_ISO_PVD_OFFSET = 16 * 2048
_ISO_VOLUME_ID = slice(40, 72)


def iso_volume_label(path: str | Path) -> str | None:
    """Return the ISO9660 volume label of an image or a loaded disc, or None
    if it has no primary volume descriptor or cannot be read (e.g. a blank
    disc)."""
    try:
        with open(path, "rb") as f:
            f.seek(_ISO_PVD_OFFSET)
            pvd = f.read(2048)
    except OSError:
        return None
    if len(pvd) < 2048 or pvd[0] != 1 or pvd[1:6] != b"CD001":
        return None
    return pvd[_ISO_VOLUME_ID].decode("ascii", errors="replace").rstrip(" ") or None


def _same_label(found: str | None, label: str) -> bool:
    return found is not None and found.casefold() == label.casefold()


def drives_with_label(devices: list[str], label: str) -> set[str]:
    """Return the drives whose loaded medium udev reports with `label`.
    Reads only the udev database; no drive receives a command."""
    return {device for device in devices if _same_label(udev.disc_label(device), label)}


def wait_for_burned_disc(
    burner: str, others: list[str], label: str, ignored: set[str], close_tray: bool
) -> str:
    """Block until the burned disc labelled `label` is loaded in the burner
    or in one of `others`, and return that drive.

    growisofs ejects the tray after burning, which is the only reliable way
    on Linux to invalidate the kernel's cached "Blank BD-R" view of the
    medium. With `close_tray`, the burner gets close-tray attempts per
    `_CLOSE_TRAY_SCHEDULE_S`; slim drives without a tray motor ignore them
    and need a manual push.

    The burner is checked directly: whenever it reports a newly loaded disc,
    its volume label is read once, so a blank disc inserted for the next
    burn is not mistaken for the burned one. `others` are watched through
    the udev database only and never receive a command until udev reports
    the label, so drives that are busy burning are not disturbed. Drives in
    `ignored` already held a disc with this label before the burn; they
    count only after udev has reported a different medium (or none). There
    is no hard timeout: a user who walked away can come back later. Ctrl+C
    bubbles up the usual way to abort.
    """
    devices = ", ".join([burner, *others])
    if close_tray:
        log.info(
            f"Waiting for the burned disc {label} in {devices} "
            f"(the tray of {burner} closes in software; slim drives need a manual push)..."
        )
    else:
        log.info(f"Waiting for the burned disc {label} in {devices} (insert it by hand)...")
    ignored = set(ignored)
    for device in sorted(ignored):
        log.info(
            f"Ignoring {device} until its disc is replaced: it already held "
            f"a disc labelled {label} before the burn"
        )

    start = time.monotonic()
    attempts_done = 0
    burner_checked = False
    while True:
        status = eject_tool.drive_status(burner)
        if status == eject_tool.CDS_DISC_OK or status is None:
            # Read the label once per loaded disc. Without the CDROM ioctl
            # (status None) the drive cannot be observed, so keep reading.
            if not burner_checked:
                if status is not None:
                    # Let the drive spin up and read the TOC first.
                    time.sleep(2)
                if _same_label(iso_volume_label(burner), label):
                    log.ok(f"Disc {label} found in {burner}")
                    return burner
                burner_checked = status is not None
        else:
            burner_checked = False

        matches = drives_with_label(others, label)
        ignored &= matches
        for device in others:
            if device not in matches or device in ignored:
                continue
            # udev saw the filesystem; confirm with this one drive that it
            # is ready before the caller mounts it.
            if eject_tool.drive_status(device) in (eject_tool.CDS_DISC_OK, None):
                time.sleep(2)
                log.ok(f"Disc {label} found in {device}")
                return device

        if close_tray:
            attempts_done = _close_tray_when_due(burner, time.monotonic() - start, attempts_done)

        time.sleep(1)


class DiscIO:
    def __init__(self, device: str):
        self.device = device

    def mount(self, preferred_dir: Path) -> tuple[Path | None, str]:
        """Mount the disc read-only. Returns (mount_path, error_message).

        mount_path is None on failure; error_message is empty on success
        and carries diagnostic text from the failing backend(s) on
        failure (both are concatenated when udisksctl fallback also
        fails — useful for telling apart "no permission" vs "no medium"
        vs "wrong fs type").

        Tries plain `mount` first (works if the user has permission via
        fstab or sudoers NOPASSWD). Falls back to `udisksctl mount`,
        which uses Polkit and works for the active desktop user without
        a password — but picks its own mount path under /run/media/...
        so the returned path may differ from preferred_dir.

        Never uses interactive sudo: an unattended verify pass shouldn't
        block on a password prompt.
        """
        preferred_dir.mkdir(parents=True, exist_ok=True)
        ok, err1 = mount_tool.mount(self.device, preferred_dir)
        if ok:
            return preferred_dir, ""

        if udisks.is_available():
            mount_path, err2 = udisks.mount(self.device)
            if mount_path is not None:
                return Path(mount_path), ""
            return None, f"mount: {err1} | udisksctl: {err2}"
        return None, err1

    def mount_with_retry(
        self, preferred_dir: Path, timeout: int = POST_BURN_MOUNT_TIMEOUT
    ) -> tuple[Path | None, str]:
        """Poll the device until it is mountable or timeout expires.

        Useful right after a burn, where the drive needs a few seconds
        to finalise the disc and re-read the TOC. Returns the same
        (mount_path, error_message) tuple as `mount()` — on timeout the
        message is from the last mount attempt.
        """
        deadline = time.monotonic() + timeout
        last_err = ""
        while True:
            mounted, err = self.mount(preferred_dir)
            if mounted is not None:
                return mounted, ""
            last_err = err
            if time.monotonic() >= deadline:
                return None, last_err
            time.sleep(1)

    def umount(self, mount_path: Path):
        if mount_tool.umount(mount_path):
            return
        if udisks.is_available() and udisks.unmount(self.device):
            return
        log.warn(f"Could not unmount {mount_path}")

    def eject(self):
        eject_tool.eject(self.device)

    def burn(
        self,
        iso_path: Path,
        speed: str | None = None,
        *,
        write_timeout: int = DEFAULT_WRITE_TIMEOUT,
        ring_buffer: int = DEFAULT_RING_BUFFER,
    ):
        growisofs.burn(
            self.device,
            iso_path,
            speed,
            write_timeout=write_timeout,
            ring_buffer=ring_buffer,
        )

    def burn_folder(
        self,
        entries,
        volume_label: str,
        publisher: str,
        speed=None,
        *,
        rock_ridge=False,
        write_timeout: int = DEFAULT_WRITE_TIMEOUT,
        ring_buffer: int = DEFAULT_RING_BUFFER,
    ):
        growisofs.burn(
            self.device,
            None,
            speed,
            write_timeout=write_timeout,
            ring_buffer=ring_buffer,
            filesystem_args=mkisofs.filesystem_args(
                entries, volume_label, publisher, rock_ridge=rock_ridge
            ),
        )
