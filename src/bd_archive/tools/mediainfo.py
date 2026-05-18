import re

from bd_archive.shell.runner import run


def detect_disc_capacity(device: str) -> int | None:
    """Read writable optical capacity from dvd+rw-mediainfo.

    Two relevant fields:
      - `Free Blocks`: writable remainder. For write-once media (BD-R,
        DVD-R) this is the per-burn capacity in the disc's *current*
        format state. On a blank, unformatted BD-R this is the full
        nominal capacity (e.g. 12219392 blocks ≈ 25.03 GB on SL).
      - `Track Size`: full track extent. Used as fallback when Free
        Blocks reads 0 (rewritable formatted media — DVD+RW, BD-RE).

    No format-descriptor walk: we burn with `-use-the-force-luke=spare=none`
    (see `tools.growisofs.burn`), which keeps blank BD-R unformatted —
    no Outer Spare Area reservation, no defect management. The drive
    then accepts writes up to Free Blocks. If `spare=none` were
    removed, the drive would format with default OSA (~256 MiB on a
    25 GB SL BD-R) and Free Blocks would over-report by that amount;
    in that case capacity would have to come from the MMC-6 32h
    format-type descriptor in `READ FORMAT CAPACITIES` instead.

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

    capacity = free_bytes if free_bytes > 0 else track_bytes
    return capacity if capacity > 0 else None
