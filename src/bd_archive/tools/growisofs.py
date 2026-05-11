import subprocess
from pathlib import Path


class DeviceBusyError(Exception):
    """growisofs couldn't grab the associated sg device — typically held
    by a tool like MakeMKV, K3b, or a desktop auto-mount probe."""

    def __init__(self, device: str):
        super().__init__(device)
        self.device = device


def burn(device: str, iso_path: Path, speed: str | None = None):
    """Burn a pre-built ISO file via growisofs.

    growisofs's -Z dev=image syntax writes the ISO byte-for-byte to
    the disc — no on-the-fly mkisofs invocation, so what's in the
    ISO file is exactly what ends up on the disc. Volume label,
    publisher, file layout are all already in the file from the
    build step.

    -dvd-compat: pad the lead-out to make the disc readable by
    standalone players + older drives. Negligible space cost.
    -use-the-force-luke=notray: skip post-burn tray eject/reload
    (some drives physically pop the tray, requiring re-insert
    before verify can run).

    Raises DeviceBusyError if growisofs reports the sg device is
    locked; CalledProcessError on any other non-zero exit.
    """
    cmd = ["growisofs", "-use-the-force-luke=notray", "-dvd-compat", "-Z", f"{device}={iso_path}"]
    if speed:
        cmd += [f"-speed={speed}"]

    # Stream output while watching for the "device busy" marker so
    # the caller can retry without re-running the whole script.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    sg_locked = False
    for line in proc.stdout:
        print(f"  [burn] {line}", end="")
        if "failed to grab associated sg device" in line:
            sg_locked = True
    proc.wait()
    if proc.returncode != 0:
        if sg_locked:
            raise DeviceBusyError(device)
        raise subprocess.CalledProcessError(proc.returncode, cmd)
