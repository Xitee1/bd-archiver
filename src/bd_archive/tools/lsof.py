import shutil
from pathlib import Path

from bd_archive.shell.runner import run


def find_device_holders(*devices: str) -> list[str]:
    """Return 'PID COMMAND' lines for processes holding any of the given
    devices open. Empty if lsof is unavailable or finds nothing."""
    if shutil.which("lsof") is None:
        return []
    paths = [d for d in devices if d and Path(d).exists()]
    if not paths:
        return []
    r = run(["lsof", "-Fpc", "--", *paths], capture=True, check=False)
    if r.returncode != 0 or not r.stdout:
        return []
    holders = []
    pid = None
    for line in r.stdout.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("c") and pid:
            holders.append(f"{pid} {line[1:]}")
            pid = None
    return holders
