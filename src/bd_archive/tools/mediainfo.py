import re

from bd_archive.shell.runner import run


def detect_disc_capacity(device: str) -> int | None:
    """Read writable optical capacity from dvd+rw-mediainfo, accounting
    for the drive's format / Outer Spare Area reservation.

    Two relevant fields:
      - `Free Blocks`: writable remainder. For write-once media (BD-R,
        DVD-R) this is the per-burn capacity. For rewritable media
        (DVD+RW, BD-RE) it reads 0 after format, since the concept
        doesn't apply to overwriteable discs.
      - `Track Size`: the full track extent. Used as fallback when
        Free Blocks is 0 (rewritable formatted media).

    On top of that, `READ FORMAT CAPACITIES` lists per-format-type
    descriptors (MMC-6 codes — 26h DVD+RW, 2Ah DVD-RW, 30h BD-RE,
    32h BD-R, etc.) giving the actual writable extent for each
    spare-area configuration. The largest descriptor ≤ upper bound is
    what the drive will accept in its default sequential/format mode.
    For BD-R this is critical: Free Blocks reports the nominal
    capacity but the drive enforces ~256 MiB less (default Outer
    Spare Area) — only the 32h descriptor reflects the real limit.

    Returns None if no disc, command fails, or output unparseable.
    """
    try:
        r = run(["dvd+rw-mediainfo", device], capture=True, check=False)
    except FileNotFoundError:
        return None
    if r.returncode != 0:
        return None

    free_match = re.search(r"Free Blocks:\s+(\d+)\*2KB", r.stdout)
    track_match = re.search(r"Track Size:\s+(\d+)\*2KB", r.stdout)
    free_bytes = int(free_match.group(1)) * 2048 if free_match else 0
    track_bytes = int(track_match.group(1)) * 2048 if track_match else 0

    # Prefer Free Blocks when non-zero (write-once partial state).
    # For rewritable formatted media (DVD+RW, BD-RE), Free Blocks ≡ 0
    # and Track Size carries the writable extent.
    upper_bound = free_bytes if free_bytes > 0 else track_bytes
    if upper_bound == 0:
        return None

    # All format-type descriptors except 00h (= current capacity, which
    # is already covered by Free Blocks/Track Size).
    fmt_caps = []
    for m in re.finditer(r"([0-9A-Fa-f]{2})h\(\d+\):\s+(\d+)\*2048", r.stdout):
        if m.group(1).upper() == "00":
            continue
        fmt_caps.append(int(m.group(2)) * 2048)
    candidates = [c for c in fmt_caps if 0 < c <= upper_bound]
    if candidates:
        return max(candidates)

    return upper_bound
