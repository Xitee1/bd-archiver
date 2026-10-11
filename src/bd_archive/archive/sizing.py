import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from bd_archive.archive.share import Share, fill_worthwhile, fixed_recovery_layout
from bd_archive.constants import DISC_WRITE_BLOCK, PAR2_AND_MISC_OVERHEAD, MiB
from bd_archive.shell.format import human_bytes
from bd_archive.tools import dar
from bd_archive.ui.logger import log


def disc_write_bytes(image_bytes: int) -> int:
    """Bytes growisofs writes, including zero padding to a full 32-KiB block."""
    return ((image_bytes + DISC_WRITE_BLOCK - 1) // DISC_WRITE_BLOCK) * DISC_WRITE_BLOCK


def slice_available(disc_bytes: int, catalog_est: int) -> int:
    """Disc bytes for a DAR slice and its recovery data, after the per-disc overhead."""
    return disc_bytes - catalog_est - PAR2_AND_MISC_OVERHEAD


def dar_recovery_bytes(slice_size: int, available: int, minimum: Share, fill: bool) -> int:
    """Recovery bytes for one finished slice: the -r minimum, or with fill the free space.

    The same rule applies to every slice; only the slice's actual size differs.
    """
    base = minimum.bytes_of(slice_size)
    if not fill:
        return base
    space = available - slice_size
    if space > base and fill_worthwhile(space - base, slice_size):
        return space
    return base


def compute_slice_bytes(disc_bytes: int, catalog_est: int, redundancy: Share) -> int:
    """Largest slice that fits on a disc with overhead. Returns 0 if it doesn't fit."""
    available = slice_available(disc_bytes, catalog_est)
    if available <= 0:
        return 0
    if redundancy.percent:
        slice_bytes = int(available * 100 / (100 + redundancy.value))
    else:
        slice_bytes = available - redundancy.bytes_of(available)
    return max(0, (slice_bytes // MiB) * MiB)


def measure_compression_ratio(
    sample: Path, compression: str, level: str | None, tmpdir: Path | None = None
) -> float:
    """Run dar on sample with the given compression; return output/input ratio.

    Uses a temp directory for the test archive (cleaned up automatically).
    The user picks a representative subset — the ratio is only meaningful
    if the sample's file-type mix matches the full source. Small samples
    (<50 MiB) inflate the ratio because the embedded dar catalog +
    per-archive overhead become a noticeable fraction of the output.
    """
    if not sample.is_dir():
        log.error(f"Sample must be a directory: {sample}")
        sys.exit(1)

    sample_size = sum(
        p.stat().st_size for p in sample.rglob("*") if p.is_file() and not p.is_symlink()
    )
    if sample_size == 0:
        log.error(f"Sample {sample} contains no files")
        sys.exit(1)
    if sample_size < 50 * MiB:
        log.warn(
            f"Small sample ({human_bytes(sample_size)}) — ratio "
            f"likely inflated by per-archive overhead"
        )

    label = compression + (f":{level}" if level else "")
    log.info(f"Test-compressing {human_bytes(sample_size)} sample with {label}...")

    if tmpdir is not None:
        tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="bd-sample-", dir=str(tmpdir) if tmpdir else None
    ) as tmp:
        archive = Path(tmp) / "sample"
        dar.compress(archive, sample, compression, level)
        output_size = sum(p.stat().st_size for p in Path(tmp).glob("*.dar"))

    ratio = output_size / sample_size
    log.ok(f"Measured ratio {ratio:.3f}: {human_bytes(sample_size)} → {human_bytes(output_size)}")
    return ratio


def dar_fill_layout(size: int, slice_size: int) -> tuple[int, int]:
    """PAR2 block size and count filling ``size`` bytes for one slice.

    Each recovery block also carries a 68-byte packet header; counting it keeps
    many small blocks within the space the slice sizing reserved.
    """
    block_size, count = fixed_recovery_layout(Share(Decimal(size), False, f"{size}"), slice_size)
    return block_size, min(count, size // (block_size + 68))
