import signal
import subprocess
import time
from pathlib import Path

# Window during which a second Ctrl+C is treated as a confirmed force-abort.
# 5s is long enough for a deliberate double-press but short enough that an
# accidental Ctrl+C plus a later real one don't compound.
BURN_ABORT_GRACE_S = 5.0


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
    -use-the-force-luke=spare=none: skip the BD-R format step that
    growisofs otherwise does unconditionally. Without that format,
    BD-R defect management is off: no read-after-write verify, no
    Outer Spare Area reservation. The drive writes at full rated
    speed (~2x → ~4x on a 4x disc) and the full nominal Free Blocks
    capacity is usable. We have par2 FEC + sha512 + a post-burn
    verify pass, so drive-firmware DM is redundant defence in depth
    that costs half the write time and ~256 MiB per disc.

    ⚠️ This flag is COUPLED to `tools.mediainfo.detect_disc_capacity`,
    which returns `Free Blocks` directly (nominal capacity). If you
    remove `spare=none`, growisofs will format the BD-R and reserve
    an Outer Spare Area (~256 MiB on 25 GB SL), and writes past that
    LBA will fail with SK=5h/LBA OUT OF RANGE — Free Blocks then
    over-reports the writable extent. To re-enable DM you MUST also
    revert detect_disc_capacity to read the MMC-6 32h format-type
    descriptor from `READ FORMAT CAPACITIES` (see commit 43fce62).

    Ctrl+C during a burn would coaster a BD-R, so we trap SIGINT
    here: first press warns; a second press within BURN_ABORT_GRACE_S
    terminates growisofs and raises KeyboardInterrupt. growisofs runs
    in its own session (start_new_session=True) so it does NOT get
    SIGINT from the user's tty — only we decide when it dies.

    Raises DeviceBusyError if growisofs reports the sg device is
    locked; CalledProcessError on any other non-zero exit;
    KeyboardInterrupt if the user confirmed a mid-burn abort.
    """
    cmd = [
        "growisofs",
        "-use-the-force-luke=notray",
        "-use-the-force-luke=spare=none",
        "-dvd-compat",
        "-Z",
        f"{device}={iso_path}",
    ]
    if speed:
        cmd += [f"-speed={speed}"]

    # start_new_session=True isolates growisofs from the user's SIGINT
    # so the burn only dies when WE call terminate() — see handler below.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    state = {"first_press_at": None, "aborted": False}

    def handler(_signum, _frame):
        now = time.monotonic()
        prev = state["first_press_at"]
        if prev is not None and now - prev <= BURN_ABORT_GRACE_S:
            # Confirmed force-abort.
            print()
            print(
                "  [burn] Aborting growisofs — this disc will be unusable.",
                flush=True,
            )
            state["aborted"] = True
            proc.terminate()
            return
        state["first_press_at"] = now
        print()
        print(
            "  [burn] Burn in progress — Ctrl+C ignored to protect the disc.",
            flush=True,
        )
        print(
            f"  [burn] Press Ctrl+C again within {int(BURN_ABORT_GRACE_S)}s "
            f"to force-abort and waste this disc.",
            flush=True,
        )

    prev_handler = signal.signal(signal.SIGINT, handler)
    try:
        assert proc.stdout is not None
        sg_locked = False
        for line in proc.stdout:
            print(f"  [burn] {line}", end="")
            if "failed to grab associated sg device" in line:
                sg_locked = True
        proc.wait()
    finally:
        signal.signal(signal.SIGINT, prev_handler)

    if state["aborted"]:
        # User confirmed mid-burn cancel: surface as KeyboardInterrupt so
        # cmd_burn's per-disc handler prints the resume hint and the
        # top-level handler exits 130.
        raise KeyboardInterrupt
    if proc.returncode != 0:
        if sg_locked:
            raise DeviceBusyError(device)
        raise subprocess.CalledProcessError(proc.returncode, cmd)
