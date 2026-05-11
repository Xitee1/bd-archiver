import sys
import tempfile
from pathlib import Path

from bd_archive.constants import PAR2_AND_MISC_OVERHEAD, MiB
from bd_archive.shell.format import human_bytes
from bd_archive.tools import dar
from bd_archive.ui.logger import log


def compute_slice_bytes(disc_bytes: int, catalog_est: int, redundancy: int) -> int:
    """Largest slice that fits on a disc with overhead. Returns 0 if it doesn't fit."""
    per_disc_overhead = catalog_est + PAR2_AND_MISC_OVERHEAD
    if per_disc_overhead >= disc_bytes:
        return 0
    available = disc_bytes - per_disc_overhead
    slice_bytes = available * 100 // (100 + redundancy)
    return (slice_bytes // MiB) * MiB


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
