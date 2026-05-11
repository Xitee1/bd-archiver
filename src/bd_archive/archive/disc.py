import time
from pathlib import Path

from bd_archive.constants import POST_BURN_MOUNT_TIMEOUT
from bd_archive.tools import eject as eject_tool
from bd_archive.tools import growisofs, udisks
from bd_archive.tools import mount as mount_tool
from bd_archive.ui.logger import log


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

    def mount(self, preferred_dir: Path) -> Path | None:
        """Mount the disc read-only. Returns the actual mount path, or
        None on failure.

        Tries plain `mount` first (works if the user has permission via
        fstab or sudoers NOPASSWD). Falls back to `udisksctl mount`,
        which uses Polkit and works for the active desktop user without
        a password — but picks its own mount path under /run/media/...
        so the returned path may differ from preferred_dir.

        Never uses interactive sudo: an unattended verify pass shouldn't
        block on a password prompt.
        """
        preferred_dir.mkdir(parents=True, exist_ok=True)
        if mount_tool.mount(self.device, preferred_dir):
            return preferred_dir

        if udisks.is_available():
            mount_path = udisks.mount(self.device)
            if mount_path is not None:
                return Path(mount_path)
        return None

    def mount_with_retry(
        self, preferred_dir: Path, timeout: int = POST_BURN_MOUNT_TIMEOUT
    ) -> Path | None:
        """Poll the device until it is mountable or timeout expires.

        Useful right after a burn, where the drive needs a few seconds
        to finalise the disc and re-read the TOC.
        """
        deadline = time.monotonic() + timeout
        while True:
            mounted = self.mount(preferred_dir)
            if mounted is not None:
                return mounted
            if time.monotonic() >= deadline:
                return None
            time.sleep(1)

    def umount(self, mount_path: Path):
        if mount_tool.umount(mount_path):
            return
        if udisks.is_available() and udisks.unmount(self.device):
            return
        log.warn(f"Could not unmount {mount_path}")

    def eject(self):
        eject_tool.eject(self.device)

    def close_tray_if_open(self) -> bool:
        """If the drive reports an open tray, send the close command.
        Returns True if a close was attempted. Some drives auto-eject
        after burn finalisation — this pulls the tray back in so the
        post-burn verify can mount the disc."""
        if eject_tool.drive_status(self.device) != eject_tool.CDS_TRAY_OPEN:
            return False
        log.info("Tray ejected by drive — closing it again")
        eject_tool.close_tray(self.device)
        return True

    def burn(self, iso_path: Path, speed: str | None = None):
        growisofs.burn(self.device, iso_path, speed)
