from dataclasses import dataclass
from pathlib import Path


@dataclass
class SourceScan:
    total_bytes: int  # sum of regular file sizes
    entry_count: int  # files + dirs + symlinks + ...
    catalog_est: int  # estimated isolated dar catalog size


def scan_source(source: Path) -> SourceScan:
    """Walk source once; return size, entry count, and catalog estimate.

    Catalog estimate: dar's isolated catalog stores ~256 B per entry
    (metadata + sha512 hash + record framing) plus the relative path
    length. Used to size per-disc overhead and for capacity planning.
    """
    PER_ENTRY = 256
    HEADER = 64 * 1024
    catalog = HEADER
    total = 0
    count = 0
    for p in source.rglob("*"):
        count += 1
        try:
            rel = p.relative_to(source).as_posix()
            catalog += PER_ENTRY + len(rel.encode("utf-8"))
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except (OSError, ValueError):
            catalog += PER_ENTRY + 256
    return SourceScan(total_bytes=total, entry_count=count, catalog_est=catalog)


def scan_delta_bytes(source: Path, known_paths: set[str], base_mtime: float) -> int:
    """Sum sizes of files that are either new or modified vs. a base catalog.

    Approximates the data payload size of an incremental archive for
    preview purposes. A file is counted when either its relative path
    is not in known_paths (truly new) or its mtime exceeds base_mtime
    (likely modified since base). mtime is a heuristic — dar's actual
    diff uses ctime/size/hash and may include or exclude slightly
    different files; the estimate is good enough for disc-count
    planning.
    """
    total = 0
    for p in source.rglob("*"):
        try:
            if not p.is_file() or p.is_symlink():
                continue
            rel = p.relative_to(source).as_posix()
            st = p.stat()
            if rel not in known_paths or st.st_mtime > base_mtime:
                total += st.st_size
        except (OSError, ValueError):
            pass
    return total
