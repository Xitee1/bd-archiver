"""Measure proposed raw discs using sparse stand-ins, without reading payloads."""

import contextlib
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from bd_archive.archive.plan import Unit
from bd_archive.archive.raw import (
    MAX_PAR2_BLOCKS,
    RAW_CHECKSUMS,
    RawEntry,
    RawPar2Sizing,
    fixed_raw_recovery,
    raw_par2_sizing,
    write_raw_checksums,
)
from bd_archive.archive.share import Share, fill_worthwhile
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


@contextlib.contextmanager
def _sparse_disc(group: tuple[int, ...], units: list[Unit], layout: DiscLayout):
    """A temporary disc tree with sparse stand-ins and placeholder checksums."""
    inventory = layout.disc_entries(group, units)
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
        payload_size = mkisofs.estimate_size(
            [("", root)], layout.name, layout.publisher, rock_ridge=True
        )
        metadata = root / RAW_METADATA_DIR
        metadata.mkdir()
        (metadata / RAW_V3_MARKER).write_text(RAW_V3_MARKER_TEXT, encoding="utf-8")
        write_raw_checksums(root, inventory, metadata / RAW_CHECKSUMS, placeholder=True)
        yield root, inventory, payload_size


def _write_placeholders(metadata: Path, sizing: RawPar2Sizing, blocks: int) -> list[Path]:
    paths = [metadata / RAW_PAR2_INDEX, metadata / "recovery.vol00000+32768.par2"]
    for path, length in zip(paths, sizing.file_sizes(blocks), strict=True):
        with path.open("wb") as stream:
            stream.truncate(length)
    return paths


def measure(
    group: tuple[int, ...], units: list[Unit], capacity: int, layout: DiscLayout
) -> Measurement:
    """Size one disc: payload, attachments, checksums, README allowance and -r recovery.

    ``payload`` counts only the units' data. A disc without non-empty files
    gets no recovery stand-ins.
    """
    with _sparse_disc(group, units, layout) as (root, inventory, payload_size):
        with (root / RAW_README).open("wb") as stream:
            stream.truncate(layout.readme_bytes)
        data = sum(e.size for e in inventory if stat.S_ISREG(e.mode))
        if layout.recovery is not None and data:
            sizing, blocks = fixed_raw_recovery(inventory, capacity, payload_size, layout.recovery)
            _write_placeholders(root / RAW_METADATA_DIR, sizing, blocks)
        required = (
            disc_write_bytes(
                mkisofs.estimate_size([("", root)], layout.name, layout.publisher, rock_ridge=True)
            )
            + DISC_END_MARGIN
        )
        return Measurement(required, sum(units[i].size for i in group))


def fill_recovery_blocks(metadata, entries, label, publisher, sizing, capacity, minimum) -> int:
    """Most recovery blocks that fit, measured with sparse stand-ins.

    mkisofs accounts for sector rounding, directory records and multi-extent
    files. No payload or recovery bytes are read/written during this search;
    only the two temporary placeholder files are created and removed.
    ``capacity`` already excludes the README allowance.
    """
    index = metadata / RAW_PAR2_INDEX
    volume = metadata / "recovery.vol00000+32768.par2"
    target = capacity - DISC_END_MARGIN
    best = 0
    low, high = 1, MAX_PAR2_BLOCKS
    try:
        while low <= high:
            count = (low + high) // 2
            _write_placeholders(metadata, sizing, count)
            size = disc_write_bytes(
                mkisofs.estimate_size(entries, label, publisher, rock_ridge=True)
            )
            if size <= target:
                best = count
                low = count + 1
            else:
                high = count - 1
    finally:
        index.unlink(missing_ok=True)
        volume.unlink(missing_ok=True)
    if best < minimum:
        raise ValueError(
            "Not enough free disc capacity for checksums, the minimum PAR2 recovery data and "
            "the safety margin; use a larger disc or a smaller -r."
        )
    return best


@dataclass(frozen=True)
class RecoveryChoice:
    """The PAR2 recovery of one disc: the -r minimum, extended by --fill-recovery."""

    sizing: RawPar2Sizing | None
    minimum_blocks: int
    blocks: int
    extra: int  # bytes filling would add beyond the minimum, also when skipped
    data: int  # non-empty file data the recovery protects

    @property
    def minimum_bytes(self) -> int:
        return 0 if self.sizing is None else self.minimum_blocks * self.sizing.block_size

    @property
    def fill_bytes(self) -> int:
        return (
            0
            if self.sizing is None
            else (self.blocks - self.minimum_blocks) * self.sizing.block_size
        )

    @property
    def bytes(self) -> int:
        return 0 if self.sizing is None else self.blocks * self.sizing.block_size


def choose_recovery(
    metadata: Path,
    entries,
    inventory: list[RawEntry],
    capacity: int,
    payload_size: int,
    layout: DiscLayout,
    fill: bool,
) -> RecoveryChoice:
    """Decide the recovery blocks of a disc whose payload and checksums exist below metadata.

    Planning and creation call this with the same inputs, so the preview shows
    the recovery that creation writes.
    """
    data = sum(e.size for e in inventory if stat.S_ISREG(e.mode))
    if (layout.recovery is None and not fill) or not data:
        return RecoveryChoice(None, 0, 0, 0, data)
    if layout.recovery is not None:
        sizing, minimum = fixed_raw_recovery(inventory, capacity, payload_size, layout.recovery)
    else:
        sizing, minimum = raw_par2_sizing(inventory, capacity, max(1, capacity - payload_size)), 0
    blocks, extra = minimum, 0
    if fill:
        best = fill_recovery_blocks(
            metadata,
            entries,
            layout.name,
            layout.publisher,
            sizing,
            capacity - layout.readme_bytes,
            minimum,
        )
        extra = (best - minimum) * sizing.block_size
        if fill_worthwhile(extra, data):
            blocks = best
    return RecoveryChoice(sizing, minimum, blocks, extra, data)


def plan_recovery(
    group: tuple[int, ...], units: list[Unit], capacity: int, layout: DiscLayout, fill: bool
) -> tuple[RecoveryChoice, int]:
    """Preview one disc's recovery and its estimated write size, including the end margin."""
    with _sparse_disc(group, units, layout) as (root, inventory, payload_size):
        entries = [("", root)]
        choice = choose_recovery(
            root / RAW_METADATA_DIR, entries, inventory, capacity, payload_size, layout, fill
        )
        if choice.blocks:
            _write_placeholders(root / RAW_METADATA_DIR, choice.sizing, choice.blocks)
        with (root / RAW_README).open("wb") as stream:
            stream.truncate(layout.readme_bytes)
        size = disc_write_bytes(
            mkisofs.estimate_size(entries, layout.name, layout.publisher, rock_ridge=True)
        )
        return choice, size + DISC_END_MARGIN
