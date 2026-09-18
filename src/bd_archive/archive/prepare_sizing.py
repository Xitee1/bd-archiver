"""Measure proposed raw sources using sparse stand-ins, without reading payloads."""

import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bd_archive import __version__
from bd_archive.archive.prepare import Unit
from bd_archive.archive.raw import (
    RAW_CHECKSUMS,
    fixed_raw_recovery,
    raw_par2_sizing,
    write_raw_checksums,
)
from bd_archive.archive.sizing import disc_write_bytes
from bd_archive.constants import DISC_END_MARGIN, DISC_WRITE_BLOCK
from bd_archive.tools import mkisofs


@dataclass(frozen=True)
class Measurement:
    required: int
    payload: int

    def free(self, capacity: int) -> int:
        return max(0, capacity - self.required)

    def budget(self, capacity: int) -> int:
        return max(0, capacity - (self.required - self.payload))


def measure(
    group: tuple[int, ...], units: list[Unit], capacity: int, redundancy: int | None
) -> Measurement:
    inventory = sorted((e for i in group for e in units[i].entries), key=lambda e: e.path)
    total = sum(units[i].size for i in group)
    if redundancy != 0 and total == 0:
        raise ValueError("A disc containing only empty files/directories requires -r none")
    with tempfile.TemporaryDirectory(prefix="bd-prepare-size-") as scratch:
        root = Path(scratch)
        # Reserve enough path width even if the number needs more than four digits.
        source = root / f"disc_{len(units):04d}"
        source.mkdir()
        for entry in inventory:
            target = source / entry.path
            if stat.S_ISDIR(entry.mode):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.truncate(entry.size)
        metadata = root / "metadata"
        metadata.mkdir()
        publisher = f"bd-archive v{__version__}"
        label = "Prepared_RAW"
        entries = [(source.name, source)]
        payload_size = mkisofs.estimate_size(entries, label, publisher, rock_ridge=True)
        write_raw_checksums(
            source, inventory, metadata / RAW_CHECKSUMS, placeholder=True, path_prefix=source.name
        )
        # A bounded README allowance; follow-up commands do not add a description.
        with (metadata / "README.txt").open("wb") as stream:
            stream.truncate(DISC_WRITE_BLOCK)
        if redundancy != 0:
            if redundancy is None:
                sizing = raw_par2_sizing(
                    inventory, capacity, capacity - payload_size, path_prefix=source.name
                )
                blocks = 1  # Automatic recovery needs at least one block.
            else:
                sizing, blocks = fixed_raw_recovery(
                    inventory, capacity, payload_size, redundancy, path_prefix=source.name
                )
            for name, length in zip(
                ("recovery.par2", "recovery.vol00000+32768.par2"),
                sizing.file_sizes(blocks),
                strict=True,
            ):
                with (metadata / name).open("wb") as stream:
                    stream.truncate(length)
        entries.append(("", metadata))
        required = (
            disc_write_bytes(mkisofs.estimate_size(entries, label, publisher, rock_ridge=True))
            + DISC_END_MARGIN
        )
        return Measurement(required, total)
