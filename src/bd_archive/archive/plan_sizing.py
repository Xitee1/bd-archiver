"""Measure proposed raw discs using sparse stand-ins, without reading payloads."""

import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from bd_archive.archive.plan import Unit
from bd_archive.archive.raw import (
    RAW_CHECKSUMS,
    RawEntry,
    fixed_raw_recovery,
    write_raw_checksums,
)
from bd_archive.archive.share import Share
from bd_archive.archive.sizing import disc_write_bytes
from bd_archive.constants import (
    DISC_END_MARGIN,
    DISC_WRITE_BLOCK,
    RAW_METADATA_DIR,
    RAW_PAR2_INDEX,
    RAW_README,
    RAW_V3_MARKER,
)
from bd_archive.tools import mkisofs

RAW_V3_MARKER_TEXT = "bd-archive raw disc format 3\n"


def readme_allowance(description: str) -> int:
    """Write blocks reserved for the README, which is written after recovery creation."""
    return DISC_WRITE_BLOCK + disc_write_bytes(
        len(description.encode("utf-8")) + 14 * description.count("\n")
    )


@dataclass(frozen=True)
class DiscLayout:
    """Everything besides the selected units that determines a raw disc's size."""

    name: str  # payload folder name and volume label
    publisher: str
    attachments: tuple[RawEntry, ...]  # disc-relative entries
    recovery: Share | None  # minimum recovery; None disables PAR2
    readme_bytes: int

    def disc_entries(self, group: tuple[int, ...], units: list[Unit]) -> list[RawEntry]:
        """Disc-relative entries of the payload folder contents and the attachments."""
        payload = [e for i in group for e in _with_ancestors(units[i], self.name)] + list(
            self.attachments
        )
        unique = {e.path: e for e in payload}
        return sorted(unique.values(), key=lambda e: e.path)


def _with_ancestors(unit: Unit, name: str) -> list[RawEntry]:
    """Prefix the unit with the payload folder; grouping ancestors become directories."""
    entries = [replace(e, path=f"{name}/{e.path}") for e in unit.entries]
    parts = unit.path.split("/")[:-1]
    for depth in range(1, len(parts) + 1):
        ancestor = "/".join(parts[:depth])
        entries.append(RawEntry(f"{name}/{ancestor}", stat.S_IFDIR | 0o755, 0, 0, 0, 0, 0))
    return entries


@dataclass(frozen=True)
class Measurement:
    required: int
    payload: int

    def free(self, capacity: int) -> int:
        return max(0, capacity - self.required)

    def budget(self, capacity: int) -> int:
        return max(0, capacity - (self.required - self.payload))


def measure(
    group: tuple[int, ...], units: list[Unit], capacity: int, layout: DiscLayout
) -> Measurement:
    """Size one disc: payload, attachments, checksums, README allowance and minimum recovery.

    ``payload`` counts only the units' data. Without -r, creation fills the
    remaining space with more recovery data than the minimum reserved here.
    A disc without non-empty files gets no recovery stand-ins.
    """
    inventory = layout.disc_entries(group, units)
    data = sum(e.size for e in inventory if stat.S_ISREG(e.mode))
    with tempfile.TemporaryDirectory(prefix="bd-plan-size-") as scratch:
        root = Path(scratch) / "disc"
        root.mkdir()
        for entry in inventory:
            target = root / entry.path
            if stat.S_ISDIR(entry.mode):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.truncate(entry.size)
        (root / layout.name).mkdir(exist_ok=True)
        entries = [("", root)]
        payload_size = mkisofs.estimate_size(
            entries, layout.name, layout.publisher, rock_ridge=True
        )
        metadata = root / RAW_METADATA_DIR
        metadata.mkdir()
        (metadata / RAW_V3_MARKER).write_text(RAW_V3_MARKER_TEXT, encoding="utf-8")
        write_raw_checksums(root, inventory, metadata / RAW_CHECKSUMS, placeholder=True)
        with (root / RAW_README).open("wb") as stream:
            stream.truncate(layout.readme_bytes)
        if layout.recovery is not None and data:
            sizing, blocks = fixed_raw_recovery(inventory, capacity, payload_size, layout.recovery)
            for name, length in zip(
                (RAW_PAR2_INDEX, "recovery.vol00000+32768.par2"),
                sizing.file_sizes(blocks),
                strict=True,
            ):
                with (metadata / name).open("wb") as stream:
                    stream.truncate(length)
        required = (
            disc_write_bytes(
                mkisofs.estimate_size(entries, layout.name, layout.publisher, rock_ridge=True)
            )
            + DISC_END_MARGIN
        )
        return Measurement(required, sum(units[i].size for i in group))
