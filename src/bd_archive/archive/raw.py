"""Inventory and format helpers for directly readable, single-disc archives."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from bd_archive.constants import RAW_METADATA_DIR


@dataclass(frozen=True)
class RawEntry:
    path: str
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int
    device: int


def scan_raw_source(source: Path) -> list[RawEntry]:
    """Inventory all files/dirs without silently skipping unsupported entries.

    Keep stat signatures to catch edits between PAR2 creation and ISO build.
    A raw data disc is not a Unix metadata backup: links and special files
    are rejected rather than followed or silently omitted.
    """
    entries = []

    def visit(directory):
        with os.scandir(directory) as children:
            for child in children:
                path = Path(child.path)
                rel = path.relative_to(source).as_posix()
                if rel.split("/")[0].casefold() == RAW_METADATA_DIR.casefold():
                    raise ValueError(f"{RAW_METADATA_DIR}/ is reserved for raw-disc recovery data")
                if "\n" in rel or "\r" in rel:
                    raise ValueError(f"--raw does not support line breaks in filenames: {rel!r}")
                st = child.stat(follow_symlinks=False)
                if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                    raise ValueError(f"--raw supports regular files and directories only: {rel}")
                entries.append(
                    RawEntry(
                        rel,
                        st.st_mode,
                        st.st_size,
                        st.st_mtime_ns,
                        st.st_ctime_ns,
                        st.st_ino,
                        st.st_dev,
                    )
                )
                if stat.S_ISDIR(st.st_mode):
                    visit(path)

    visit(source)
    return sorted(entries, key=lambda entry: entry.path)
