import re
import shutil

from bd_archive.shell.runner import run


def is_available() -> bool:
    return shutil.which("udisksctl") is not None


def mount(device: str) -> str | None:
    """Mount via udisksctl. Returns the mount path, or None on failure.

    udisksctl uses Polkit and works for the active desktop user without
    a password, but picks its own mount path under /run/media/...
    """
    r = run(["udisksctl", "mount", "-b", device, "--no-user-interaction"],
            capture=True, check=False)
    if r.returncode != 0:
        return None
    # "Mounted /dev/sr0 at /run/media/.../LABEL."
    m = re.search(r"^Mounted .+? at (.+?)\.?\s*$",
                  (r.stdout or "").strip(), re.MULTILINE)
    return m.group(1) if m else None


def unmount(device: str) -> bool:
    return run(["udisksctl", "unmount", "-b", device, "--no-user-interaction"],
               capture=True, check=False).returncode == 0


def loop_setup(iso_path: str) -> tuple[bool, str | None, str]:
    """Set up a loop device for iso_path. Returns (ok, loop_dev, message)."""
    r = run(["udisksctl", "loop-setup", "-f", iso_path],
            capture=True, check=False)
    if r.returncode != 0:
        return False, None, (r.stdout or r.stderr or "").strip()
    m = re.search(r"as (/dev/loop\d+)", r.stdout or "")
    if not m:
        return False, None, "Could not parse loop device from udisksctl output"
    return True, m.group(1), ""


def loop_delete(loop_dev: str):
    run(["udisksctl", "loop-delete", "-b", loop_dev],
        capture=True, check=False)
