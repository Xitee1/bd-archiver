import hashlib
from collections.abc import Callable
from pathlib import Path

HASH_CHUNK_SIZE = 65536


def _hash_file_sha512(path: Path, progress: Callable[[int], None] | None = None) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
            h.update(chunk)
            if progress is not None:
                progress(len(chunk))
    return h.hexdigest()


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
