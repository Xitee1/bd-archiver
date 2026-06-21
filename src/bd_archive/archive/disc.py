import time
from pathlib import Path

from bd_archive.constants import POST_BURN_MOUNT_TIMEOUT
from bd_archive.tools import eject as eject_tool
from bd_archive.tools import mount as mount_tool
from bd_archive.tools import udisks, xorriso
from bd_archive.ui.logger import log

# Close-tray attempt schedule (cumulative seconds from start of wait):
# first attempt fires immediately, subsequent attempts space out 5/10/
# 15/20s after the previous one. Five attempts over ~50s gives slow
# tray-load drives time to actually start moving and absorbs flaky
# motors; after that we fall through to passive polling, leaving the
# user to push a slim-drive disc back in by hand.
_CLOSE_TRAY_SCHEDULE_S = (0, 5, 15, 30, 50)


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

        Called right after a burn: xorriso ejects the tray on finish (we
        pass `-eject`), which is the only reliable way on Linux to
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

    def burn(self, iso_path: Path, speed: str | None = None):
        xorriso.burn(self.device, iso_path, speed)
