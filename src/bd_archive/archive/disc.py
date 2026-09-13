import contextlib
import tempfile
import time
from pathlib import Path

from bd_archive.constants import POST_BURN_MOUNT_TIMEOUT
from bd_archive.tools import eject as eject_tool
from bd_archive.tools import growisofs, mkisofs, udisks
from bd_archive.tools import mount as mount_tool
from bd_archive.tools.burn_timeout import DEFAULT_WRITE_TIMEOUT
from bd_archive.ui.logger import log

# Close-tray attempt schedule (cumulative seconds from start of wait):
# first attempt fires immediately, subsequent attempts space out 5/10/
# 15/20s after the previous one. Five attempts over ~50s gives slow
# tray-load drives time to actually start moving and absorbs flaky
# motors; after that we fall through to passive polling, leaving the
# user to push a slim-drive disc back in by hand.
_CLOSE_TRAY_SCHEDULE_S = (0, 5, 15, 30, 50)


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

    def wait_for_disc_ready(self) -> None:
        """Block until the drive reports a loaded, ready disc.

        Called right after a burn: growisofs's default post-burn behaviour
        is to eject the tray, which is the only reliable way on Linux to
        invalidate the kernel's cached "Blank BD-R" view of the medium
        (without that media-change event, mount sees the pre-burn blank
        state forever and udisks2 reports the disc as not-mountable).

        On tray-load drives the disc needs to come back in before the
        post-burn verify can mount it. We retry `eject -t` (close-tray)
        per `_CLOSE_TRAY_SCHEDULE_S`; if the drive doesn't honour it —
        slim/laptop drives have no tray motor — the user has to push the
        disc in by hand. Either way we keep polling `drive_status` until
        it reports CDS_DISC_OK, with no hard timeout: a user who walked
        away from the burn can come back later and still see it complete.
        Ctrl+C bubbles up the usual way to abort.
        """
        log.info(
            "Waiting for the disc to be loaded "
            "(tray-load drives close in software; "
            "slim drives need a manual push)..."
        )

        start = time.monotonic()
        attempts_done = 0

        while True:
            status = eject_tool.drive_status(self.device)
            if status == eject_tool.CDS_DISC_OK:
                # Drive sees a disc but may still be spinning up + reading
                # the TOC. Give it a moment before the caller tries to
                # mount, so we don't burn the first mount attempt on a
                # not-quite-ready drive.
                time.sleep(2)
                log.ok("Disc loaded")
                return
            if status is None:
                # CDROM ioctl unavailable (odd device, missing permission)
                # — we can't observe the tray, so hand off to the caller's
                # mount-with-retry polling instead of looping forever.
                log.warn(
                    "Cannot read drive status — proceeding to mount attempts; "
                    "make sure the disc is loaded"
                )
                return

            elapsed = time.monotonic() - start
            while (
                attempts_done < len(_CLOSE_TRAY_SCHEDULE_S)
                and elapsed >= _CLOSE_TRAY_SCHEDULE_S[attempts_done]
            ):
                # close_tray is silent on success and no-ops on drives
                # that can't motor the tray, so we don't surface every
                # attempt — just count them out internally.
                eject_tool.close_tray(self.device)
                attempts_done += 1

            time.sleep(1)

    def burn(
        self,
        iso_path: Path,
        speed: str | None = None,
        *,
        write_timeout: int = DEFAULT_WRITE_TIMEOUT,
    ):
        growisofs.burn(self.device, iso_path, speed, write_timeout=write_timeout)

    def burn_folder(
        self,
        entries,
        volume_label: str,
        publisher: str,
        speed=None,
        *,
        rock_ridge=False,
        write_timeout: int = DEFAULT_WRITE_TIMEOUT,
    ):
        growisofs.burn(
            self.device,
            None,
            speed,
            write_timeout=write_timeout,
            filesystem_args=mkisofs.filesystem_args(
                entries, volume_label, publisher, rock_ridge=rock_ridge
            ),
        )
