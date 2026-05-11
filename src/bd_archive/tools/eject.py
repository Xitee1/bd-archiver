import fcntl
import os

from bd_archive.shell.runner import run

# Linux CDROM ioctl + CDS_* status codes (uapi/linux/cdrom.h).
_CDROM_DRIVE_STATUS = 0x5326
CDS_NO_INFO = 0
CDS_NO_DISC = 1
CDS_TRAY_OPEN = 2
CDS_DRIVE_NOT_READY = 3
CDS_DISC_OK = 4


def eject(device: str):
    run(["eject", device], capture=True, check=False)


def close_tray(device: str):
    """Send the close-tray command. No-op on slot-load drives that
    don't physically support it."""
    run(["eject", "-t", device], capture=True, check=False)


def drive_status(device: str) -> int | None:
    """Return one of the CDS_* constants, or None if the ioctl is
    unavailable (non-Linux, missing permission, foreign device)."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        return fcntl.ioctl(fd, _CDROM_DRIVE_STATUS, 0)
    except OSError:
        return None
    finally:
        os.close(fd)
