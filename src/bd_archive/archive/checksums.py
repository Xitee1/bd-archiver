import hashlib
from pathlib import Path

from bd_archive.ui.logger import log

HASH_CHUNK_SIZE = 65536


def _hash_file_sha512(path: Path) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_dar_hashes(directory: Path) -> tuple[int, int]:
    """Verify every *.sha512 file in directory against its target.

    dar writes one sha512sum-format line per slice ("<hex>  <filename>")
    into a sibling file named "<slice>.sha512". Returns (ok, fail);
    missing or empty hash files count as fail.
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
            actual = _hash_file_sha512(target)
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
