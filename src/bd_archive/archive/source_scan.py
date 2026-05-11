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
