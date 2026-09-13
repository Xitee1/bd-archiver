"""Prepare and validate the process-local Linux write timeout interposer."""

import contextlib
import errno
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

DEFAULT_WRITE_TIMEOUT = 600
MAX_WRITE_TIMEOUT = 86400
LIBRARY = Path(__file__).resolve().parents[1] / "_native/burn_timeout.so"


def write_timeout(value):
    """Validate a finite timeout before passing seconds to the native helper."""
    try:
        seconds = int(value)
    except (ValueError, TypeError):
        raise ValueError("Write timeout must be an integer number of seconds") from None
    if not 1 <= seconds <= MAX_WRITE_TIMEOUT:
        raise ValueError(f"Write timeout must be between 1 and {MAX_WRITE_TIMEOUT} seconds")
    return seconds


@contextlib.contextmanager
def prepared_burn(device: str, seconds: int, env: dict[str, str]):
    """Pin the executable/library, then probe loading without accessing the drive.

    The actual burn uses the same open files and preload settings as the probe.
    Set-id/capability binaries and conflicting loader overrides are rejected.
    """
    seconds = write_timeout(seconds)
    if sys.platform != "linux":
        raise ValueError("The burn timeout helper requires Linux")
    if env.get("LD_PRELOAD") or env.get("LD_AUDIT"):
        raise ValueError("Unset LD_PRELOAD and LD_AUDIT before burning with the timeout helper")
    executable = shutil.which("growisofs")
    if executable is None:
        raise FileNotFoundError("growisofs")
    if not LIBRARY.is_file():
        raise ValueError("Burn timeout helper is missing; reinstall bd-archive with a C compiler")
    target = os.stat(device)
    if not stat.S_ISBLK(target.st_mode):
        raise ValueError(f"Burn target is not a block device: {device}")

    with contextlib.ExitStack() as stack:
        executable_fd = os.open(executable, os.O_RDONLY | os.O_CLOEXEC)
        stack.callback(os.close, executable_fd)
        library_fd = os.open(LIBRARY, os.O_RDONLY | os.O_CLOEXEC)
        stack.callback(os.close, library_fd)
        executable_path = f"/proc/self/fd/{executable_fd}"
        info = os.fstat(executable_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & (stat.S_ISUID | stat.S_ISGID):
            raise ValueError("The timeout helper requires a regular, non-setuid/setgid growisofs")
        if os.pread(executable_fd, 4, 0) != b"\x7fELF":
            raise ValueError("The timeout helper requires a native ELF growisofs executable")
        try:
            capabilities = os.getxattr(executable_fd, "security.capability")
        except OSError as exc:
            if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
                raise
            capabilities = b""
        if capabilities:
            raise ValueError("The timeout helper does not support growisofs with file capabilities")
        prepared_env = {
            **env,
            "LD_PRELOAD": f"/proc/self/fd/{library_fd}",
            "BD_BURN_TIMEOUT_SECONDS": str(seconds),
            "BD_BURN_DEVICE_MAJOR": str(os.major(target.st_rdev)),
            "BD_BURN_DEVICE_MINOR": str(os.minor(target.st_rdev)),
            "BD_BURN_LIBRARY_FD": str(library_fd),
            "BD_BURN_EXECUTABLE_FD": str(executable_fd),
        }
        prepared_env.pop("BD_BURN_TIMEOUT_PROBE", None)
        kwargs = {
            "executable": executable_path,
            "env": prepared_env,
            "pass_fds": (executable_fd, library_fd),
        }
        try:
            probe = subprocess.run(
                [executable, "-version"],
                **{**kwargs, "env": {**prepared_env, "BD_BURN_TIMEOUT_PROBE": "1"}},
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            raise ValueError(
                "Burn timeout helper startup check timed out; refusing to burn"
            ) from None
        expected = (
            f"bd-archive-timeout-v1:{seconds * 1000}:"
            f"{os.major(target.st_rdev)}:{os.minor(target.st_rdev)}\n"
        )
        if probe.returncode or probe.stdout != expected or probe.stderr:
            raise ValueError(
                "Burn timeout helper could not be activated; refusing to burn. "
                "Reinstall bd-archive and check that growisofs is dynamically linked. "
                + probe.stderr.strip()
            )
        yield kwargs
