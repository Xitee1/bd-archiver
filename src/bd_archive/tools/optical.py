import sys
from dataclasses import dataclass
from pathlib import Path

from bd_archive.ui.logger import log


@dataclass(frozen=True)
class OpticalDrive:
    path: str
    vendor: str
    model: str

    @property
    def label(self) -> str:
        parts = [p for p in (self.vendor, self.model) if p]
        return " ".join(parts) if parts else "(unknown)"


def _read_sysfs(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def list_drives() -> list[OpticalDrive]:
    """Enumerate optical drives via /sys/block/sr*."""
    drives: list[OpticalDrive] = []
    for sr_dir in sorted(Path("/sys/block").glob("sr*")):
        device_path = f"/dev/{sr_dir.name}"
        if not Path(device_path).exists():
            continue
        vendor = _read_sysfs(sr_dir / "device" / "vendor")
        model = _read_sysfs(sr_dir / "device" / "model")
        drives.append(OpticalDrive(device_path, vendor, model))
    return drives


def resolve_device(explicit: str | None) -> str:
    """Pick a block-device path. With explicit=None: 0 drives errors,
    1 drive is used silently-ish, 2+ drives prompt. With explicit set:
    validate it exists and is a block device, then return as-is."""
    if explicit is not None:
        p = Path(explicit)
        if not p.exists():
            log.error(f"Device does not exist: {explicit}")
            sys.exit(1)
        if not p.is_block_device():
            log.error(f"Not a block device: {explicit}")
            sys.exit(1)
        return explicit

    drives = list_drives()
    if not drives:
        log.error("No optical drives found.")
        log.info("Connect a Blu-ray drive, or specify one with -D/--device.")
        sys.exit(1)
    if len(drives) == 1:
        d = drives[0]
        log.info(f"Using optical drive: {d.path} ({d.label})")
        return d.path

    log.step("Multiple optical drives detected")
    for i, d in enumerate(drives, 1):
        log.info(f"  [{i}] {d.path}  {d.label}")
    while True:
        resp = (
            input(f"\033[1;33mSelect drive [1-{len(drives)}] (q = cancel): \033[0m").strip().lower()
        )
        if resp == "q":
            log.warn("Cancelled by user")
            sys.exit(0)
        try:
            idx = int(resp)
        except ValueError:
            log.warn("Please enter a number.")
            continue
        if 1 <= idx <= len(drives):
            chosen = drives[idx - 1]
            log.info(f"Using: {chosen.path} ({chosen.label})")
            return chosen.path
        log.warn(f"Out of range — pick 1..{len(drives)}.")
