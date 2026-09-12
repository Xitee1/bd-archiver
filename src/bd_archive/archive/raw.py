"""Inventory and format helpers for directly readable, single-disc archives."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from bd_archive.archive.checksums import _hash_file_sha512
from bd_archive.constants import RAW_METADATA_DIR
from bd_archive.ui.progress import Progress

RAW_CHECKSUMS = "checksums.sha512"
# Stay within par2cmdline's source-block limit and a supported recovery count.
MAX_PAR2_BLOCKS = 32768


def write_raw_checksums(
    source: Path,
    inventory: list["RawEntry"],
    destination: Path,
    *,
    placeholder: bool = False,
) -> None:
    """Write a GNU sha512sum manifest; placeholders have the same encoded size."""
    files = [entry for entry in inventory if stat.S_ISREG(entry.mode)]
    with (
        destination.open("w", encoding="utf-8", errors="surrogateescape", newline="\n") as out,
        Progress("SHA-512", 0 if placeholder else sum(entry.size for entry in files)) as progress,
    ):
        for entry in files:
            digest = (
                "0" * 128
                if placeholder
                else _hash_file_sha512(source / entry.path, progress.advance)
            )
            # GNU checksum tools prefix escaped records with a backslash.
            escaped = "\\" in entry.path
            name = entry.path.replace("\\", "\\\\")
            prefix = "\\" if escaped else ""
            out.write(f"{prefix}{digest}  {name}\n")


@dataclass(frozen=True)
class RawPar2Sizing:
    block_size: int
    critical_bytes: int

    def file_sizes(self, recovery_blocks: int) -> tuple[int, int]:
        """Upper bounds for par2cmdline's index and single recovery volume.

        Critical packets repeat bit_length(count) times in the volume; each
        recovery packet adds 68 bytes. Reserve 4 KiB per creator packet.
        See par2cmdline Par2Creator::InitialiseOutputFiles.
        """
        return (
            self.critical_bytes + 4096,
            recovery_blocks * (self.block_size + 68)
            + recovery_blocks.bit_length() * self.critical_bytes
            + 4096,
        )


def raw_par2_sizing(inventory: list["RawEntry"], capacity: int, free: int) -> RawPar2Sizing:
    files = [entry for entry in inventory if stat.S_ISREG(entry.mode) and entry.size]
    if len(files) > MAX_PAR2_BLOCKS:
        raise ValueError("PAR2 supports at most 32768 non-empty files in one recovery set")
    total = sum(entry.size for entry in files)
    # Normally use about 2000 source blocks, like par2cmdline. Near capacity,
    # use finer blocks so even less than 1% free space can provide recovery.
    target = max(2000, min(MAX_PAR2_BLOCKS, (total * 16) // max(free, 1)))
    block_size = max(
        4, (total + target - 1) // target, (capacity + MAX_PAR2_BLOCKS - 1) // MAX_PAR2_BLOCKS
    )
    block_size = (block_size + 3) // 4 * 4
    while sum((entry.size + block_size - 1) // block_size for entry in files) > MAX_PAR2_BLOCKS:
        block_size *= 2
    # Main, File Description and Input File Slice Checksum packet lengths.
    critical = 76 + 16 * len(files)
    for entry in files:
        name_bytes = len(os.fsencode(entry.path))
        blocks = (entry.size + block_size - 1) // block_size
        critical += 120 + (name_bytes + 3) // 4 * 4 + 80 + 20 * blocks
    return RawPar2Sizing(block_size, critical)


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
