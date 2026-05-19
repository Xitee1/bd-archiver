from pathlib import Path

from bd_archive.shell.runner import run


def mount(device: str, mount_dir: Path, readonly: bool = True) -> tuple[bool, str]:
    """Plain mount; never invokes sudo.

    Returns (success, error_message). error_message is empty on success
    and contains the captured stderr/stdout on failure — exposed so
    callers can log *why* the mount failed instead of just "no".
    """
    flags = ["-o", "ro"] if readonly else []
    r = run(["mount", *flags, device, str(mount_dir)], capture=True, check=False)
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or r.stdout or f"mount exited {r.returncode}").strip()


def umount(mount_path: Path) -> bool:
    """Plain umount; returns True on success."""
    return run(["umount", str(mount_path)], capture=True, check=False).returncode == 0
