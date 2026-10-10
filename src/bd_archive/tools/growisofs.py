import functools
import os
import re
import resource
import shutil
import signal
import subprocess
import time
from pathlib import Path

from bd_archive.constants import MiB
from bd_archive.tools.burn_timeout import DEFAULT_WRITE_TIMEOUT, prepared_burn
from bd_archive.ui.logger import log

# Window during which a second Ctrl+C is treated as a confirmed force-abort.
# 5s is long enough for a deliberate double-press but short enough that an
# accidental Ctrl+C plus a later real one don't compound.
BURN_ABORT_GRACE_S = 5.0

# growisofs's own ring buffer default is 32 MiB: about two seconds at 4x BD
# speed, enough for jitter but not for a source that pauses for seconds. A
# source that stops delivering lets the drive's buffer run empty, and after a
# longer stall the drive can refuse to continue (INVALID ADDRESS FOR WRITE),
# which wastes the disc. 512 MiB bridges roughly 30 s at 4x and 20 s at 6x.
DEFAULT_RING_BUFFER = 512 * MiB
MIN_RING_BUFFER = 1 * MiB  # growisofs's floor
MAX_RING_BUFFER = 64 * 1024 * MiB

# growisofs raises its soft RLIMIT_MEMLOCK to its compiled-in default buffer
# size plus 16 MiB, then locks all current and future memory, then maps the
# ring buffer. It skips the locking entirely when the raise fails, i.e. when
# the hard limit is below this value.
_GROWISOFS_MEMLOCK_RAISE = 48 * MiB
# Code, heap and thread stacks that growisofs locks beside the ring buffer.
_MEMLOCK_HEADROOM = 64 * MiB


def ring_buffer(value) -> int:
    """Parse `512` or `512M` (MiB) or `1G` (GiB) into bytes, rounded up to a power of two.

    growisofs allocates ring buffers in powers of two, so the returned size is
    the one it will actually use.
    """
    match = re.fullmatch(r"(\d+)([MG]?)", str(value).strip(), re.IGNORECASE)
    if not match:
        raise ValueError("expected MiB (512), a size in MiB (512M) or GiB (1G)")
    size = int(match[1]) * (1024 * MiB if match[2].upper() == "G" else MiB)
    if not MIN_RING_BUFFER <= size <= MAX_RING_BUFFER:
        raise ValueError("expected 1M-64G")
    return 1 << (size - 1).bit_length()


def memlock_limits(limits: tuple[int, int], ring_buffer: int) -> tuple[int, int]:
    """RLIMIT_MEMLOCK for the growisofs process so memory locking cannot refuse the buffer.

    A hard limit of 48 MiB or more that is still below the ring buffer lets
    growisofs lock its memory and then fail to map the buffer, before any
    write. Where the hard limit covers the buffer plus headroom (or is
    unlimited), the soft limit is raised to it so the whole buffer is locked.
    Otherwise the hard limit is lowered below growisofs's raise target so it
    skips locking and the buffer stays pageable.
    """
    soft, hard = limits
    if hard == resource.RLIM_INFINITY or hard >= ring_buffer + _MEMLOCK_HEADROOM:
        return hard, hard
    cap = min(hard, _GROWISOFS_MEMLOCK_RAISE - MiB)
    return min(soft, cap), cap


def _apply_memlock_limits(ring_buffer: int):
    """Child-side hook: adjust RLIMIT_MEMLOCK right before growisofs starts."""
    limits = resource.getrlimit(resource.RLIMIT_MEMLOCK)
    wanted = memlock_limits(limits, ring_buffer)
    if wanted != limits:
        resource.setrlimit(resource.RLIMIT_MEMLOCK, wanted)


class DeviceBusyError(Exception):
    """growisofs couldn't grab the associated sg device — typically held
    by a tool like MakeMKV, K3b, or a desktop auto-mount probe."""

    def __init__(self, device: str):
        super().__init__(device)
        self.device = device


def burn(
    device: str,
    iso_path: Path | None,
    speed: str | None = None,
    *,
    filesystem_args: list[str] | None = None,
    write_timeout: int = DEFAULT_WRITE_TIMEOUT,
    ring_buffer: int = DEFAULT_RING_BUFFER,
):
    """Burn an ISO or stream a prepared filesystem through growisofs.

    ring_buffer is the growisofs ring buffer in bytes (a power of two, see
    `ring_buffer()`), passed as -use-the-force-luke=bufsize. It is filled
    completely before the first write and bridges pauses of the source.
    RLIMIT_MEMLOCK of the child is adjusted by `memlock_limits` so growisofs's
    memory locking cannot refuse the buffer.

    With filesystem_args, -Z dev invokes mkisofs directly. The caller
    must first size the unchanged inputs with the same filesystem options.
    No ISO is saved to local storage in this mode.
    growisofs reports write progress, speed and buffer utilization for
    folder streams as it does for existing ISOs.

    growisofs's -Z dev=image syntax writes the ISO byte-for-byte to
    the disc — no on-the-fly mkisofs invocation, so what's in the
    ISO file is exactly what ends up on the disc. Volume label,
    publisher, file layout are all already in the file from the
    build step.

    -dvd-compat: pad the lead-out to make the disc readable by
    standalone players + older drives. Negligible space cost.
    Note: we deliberately do NOT pass `-use-the-force-luke=notray`.
    growisofs's default post-burn eject is the only reliable way to
    trigger a kernel media-change event on Linux — without it, the
    OS keeps the disc cached as "Blank BD-R" (since that's what it
    was at burn-start) and udisks2 reports it as not-mountable. The
    eject is then handled by `disc.wait_for_burned_disc` after burn:
    tray-load drives get re-closed in software unless --no-close-tray;
    slim drives need the user to push the disc back in.
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
    if (iso_path is None) == (filesystem_args is None):
        raise ValueError("Provide either an ISO or filesystem arguments")
    cmd = [
        "growisofs",
        "-use-the-force-luke=spare=none",
        f"-use-the-force-luke=bufsize:{ring_buffer // MiB}m",
        "-dvd-compat",
        "-Z",
        f"{device}={iso_path}" if iso_path is not None else device,
    ]
    if speed:
        cmd += [f"-speed={speed}"]
    if filesystem_args is not None:
        # Use growisofs's write/buffer status and silence mkisofs's progress.
        cmd += ["-use-the-force-luke=moi", *filesystem_args]

    # growisofs supports MKISOFS as a backend override. Pin it to the
    # same executable used for our size calculation, ignoring inherited
    # overrides that could otherwise change the filesystem or its size.
    env = dict(os.environ)
    if filesystem_args is not None:
        backend = shutil.which("mkisofs")
        if backend is None:
            raise FileNotFoundError("mkisofs")
        env = {**os.environ, "MKISOFS": str(Path(backend).absolute())}

    # start_new_session=True isolates growisofs from the user's SIGINT
    # so the burn only dies when WE call terminate() — see handler below.
    with prepared_burn(device, write_timeout, env) as launch:
        log.info(f"Write command timeout: at least {write_timeout} seconds")
        log.info(f"Ring buffer: {ring_buffer // MiB} MiB")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            preexec_fn=functools.partial(_apply_memlock_limits, ring_buffer),
            **launch,
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
