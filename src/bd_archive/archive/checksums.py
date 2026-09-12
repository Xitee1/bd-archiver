import hashlib
import re
from collections.abc import Callable
from pathlib import Path

from bd_archive.ui.progress import Progress

HASH_CHUNK_SIZE = 65536


def _unescape_checksum_name(match: re.Match) -> str:
    escapes = {"\\": "\\", "n": "\n", "r": "\r"}
    if match[1] not in escapes:
        raise ValueError("Invalid filename escape in SHA-512 manifest")
    return escapes[match[1]]


def verify_manifest(
    manifest: Path,
    base_dir: Path,
    *,
    expected_files: set[Path] | None = None,
) -> None:
    """Verify GNU SHA-512 records, raising on missing, malformed or damaged data.

    Paths are relative to base_dir (the disc root for raw manifests).
    expected_files requires complete coverage when the payload is known.
    """
    records: dict[Path, str] = {}
    root = base_dir.resolve()
    with manifest.open(encoding="utf-8", errors="surrogateescape") as source:
        for number, line in enumerate(source, 1):
            line = line.rstrip("\n")
            escaped = line.startswith("\\")
            if escaped:
                line = line[1:]
            match = re.fullmatch(r"([0-9a-fA-F]{128}) [ *](.+)", line)
            if match is None:
                raise ValueError(f"Malformed SHA-512 record: {manifest}:{number}")
            digest, name = match.groups()
            if escaped:
                name = re.sub(r"\\(.)|\\$", _unescape_checksum_name, name)
            relative = Path(name)
            target = base_dir / relative
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not target.resolve().is_relative_to(root)
            ):
                raise ValueError(f"Checksum path escapes its base directory: {name!r}")
            if target in records:
                raise ValueError(f"Duplicate checksum entry: {target}")
            if not target.is_file():
                raise ValueError(f"Checksum target missing or not a regular file: {target}")
            records[target] = digest.lower()
    if not records:
        raise ValueError(f"No SHA-512 records found: {manifest}")
    if expected_files is not None and records.keys() != expected_files:
        raise ValueError(f"SHA-512 manifest does not cover the expected files: {manifest}")
    total = sum(path.stat().st_size for path in records)
    with Progress("SHA-512", total) as progress:
        for target, digest in records.items():
            if _hash_file_sha512(target, progress.advance) != digest:
                raise ValueError(f"SHA-512 mismatch: {target}")


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
