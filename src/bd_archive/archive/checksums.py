import hashlib
from collections.abc import Callable
from pathlib import Path

from bd_archive.ui.logger import log
from bd_archive.ui.progress import Progress

HASH_CHUNK_SIZE = 65536


def _hash_file_sha512(path: Path, progress: Callable[[int], None] | None = None) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
            h.update(chunk)
            if progress is not None:
                progress(len(chunk))
    return h.hexdigest()


def verify_dar_hashes(directory: Path) -> tuple[int, int]:
    """Verify every *.sha512 file in directory against its target.

    dar writes one sha512sum-format line per slice ("<hex>  <filename>")
    into a sibling file named "<slice>.sha512". Returns (ok, fail);
    missing or empty hash files count as fail. Emits a per-file Progress
    line so users see read speed on large slices.
    """
    ok = fail = 0
    for hash_file in sorted(directory.glob("*.sha512")):
        text = hash_file.read_text().strip()
        if not text:
            log.error(f"  Empty hash file: {hash_file.name}")
            fail += 1
            continue
        expected, filename = text.splitlines()[0].split("  ", 1)
        target = directory / filename
        if not target.exists():
            log.error(f"  Missing: {filename}")
            fail += 1
            continue
        try:
            with Progress(f"sha512 {filename}", target.stat().st_size) as p:
                actual = _hash_file_sha512(target, progress=p.advance)
        except OSError as e:
            # Truncated/unreadable disc sectors raise OSError mid-read.
            # Treat as a verification failure rather than crashing.
            log.error(f"  Read error: {filename} ({e})")
            fail += 1
            continue
        if actual == expected:
            ok += 1
        else:
            log.error(f"  Corrupted: {filename}")
            fail += 1
    return ok, fail


def verify_slice(slice_path: Path, progress: Callable[[int], None] | None = None) -> bool:
    """Verify a single file against its sibling .sha512 sidecar.

    Returns False if the sidecar is missing/empty, the target read fails,
    or the hash mismatches. Optional `progress` callback receives the
    number of bytes hashed in each chunk (typically wired to
    `Progress.advance`).
    """
    hash_file = slice_path.parent / f"{slice_path.name}.sha512"
    if not hash_file.exists():
        return False
    try:
        text = hash_file.read_text().strip()
    except OSError:
        return False
    if not text:
        return False
    expected = text.splitlines()[0].split("  ", 1)[0]
    try:
        actual = _hash_file_sha512(slice_path, progress=progress)
    except OSError:
        return False
    return actual == expected
