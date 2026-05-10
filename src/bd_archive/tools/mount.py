from pathlib import Path

from bd_archive.shell.runner import run


def mount(device: str, mount_dir: Path, readonly: bool = True) -> bool:
    """Plain mount; returns True on success. Never invokes sudo."""
    flags = ["-o", "ro"] if readonly else []
    return run(["mount", *flags, device, str(mount_dir)],
               capture=True, check=False).returncode == 0


def umount(mount_path: Path) -> bool:
    """Plain umount; returns True on success."""
    return run(["umount", str(mount_path)],
               capture=True, check=False).returncode == 0
