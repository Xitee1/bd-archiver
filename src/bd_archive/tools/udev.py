"""Read-only access to the udev database for optical drives.

udev already probes every medium change (cdrom_id + blkid) and stores the
result in /run/udev/data/b<major>:<minor>. Reading those files never sends
a command to the drive, so other drives can be watched while one burns.
"""

import os
import re
from pathlib import Path

UDEV_DATA_DIR = Path("/run/udev/data")
SYS_BLOCK_DIR = Path("/sys/class/block")

_HEX_ESCAPE = re.compile(rb"\\x([0-9a-fA-F]{2})")


def is_available() -> bool:
    return UDEV_DATA_DIR.is_dir()


def _properties(device: str) -> dict[str, str] | None:
    name = Path(os.path.realpath(device)).name
    try:
        dev_number = (SYS_BLOCK_DIR / name / "dev").read_text().strip()
        lines = (UDEV_DATA_DIR / f"b{dev_number}").read_text(errors="replace").splitlines()
    except OSError:
        return None
    props: dict[str, str] = {}
    for line in lines:
        if line.startswith("E:") and "=" in line:
            key, value = line[2:].split("=", 1)
            props[key] = value
    return props


def _decode(value: str) -> str:
    raw = _HEX_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), value.encode())
    return raw.decode("utf-8", errors="replace")


def disc_label(device: str) -> str | None:
    """Return the filesystem label udev recorded for the loaded medium, or
    None when no medium, no filesystem or no udev record is present."""
    props = _properties(device)
    if props is None or props.get("ID_CDROM_MEDIA") != "1":
        return None
    if "ID_FS_LABEL_ENC" in props:
        return _decode(props["ID_FS_LABEL_ENC"])
    return props.get("ID_FS_LABEL")
