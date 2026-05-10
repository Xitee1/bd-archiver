import sys
from pathlib import Path

from bd_archive.archive.sizing import compute_slice_bytes, measure_compression_ratio
from bd_archive.archive.source_scan import scan_source
from bd_archive.constants import MiB, PAR2_AND_MISC_OVERHEAD
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools.mediainfo import detect_disc_capacity
from bd_archive.ui.logger import log


def cmd_estimate(args):
    """Preview disc count and per-disc fill without running dar/par2.

    Compression ratio comes from one of three sources, in order of
    accuracy: --sample <path> runs dar with the given compression on a
    representative subset and measures the actual ratio; --ratio <float>
    uses a manually supplied ratio; otherwise 1.0 (worst case, no
    compression). Disc count and last-disc fill are computed with the
    same slice-sizing math as cmd_create.
    """
    if args.sample:
        check_deps("dar")

    source = Path(args.source).resolve()
    if not source.is_dir():
        log.error(f"Does not exist: {source}")
        sys.exit(1)

    if args.bytes is not None:
        raw_capacity = args.bytes
    else:
        check_deps("dvd+rw-mediainfo")
        raw_capacity = detect_disc_capacity(args.device)
        if raw_capacity is None:
            log.error(f"No disc detected at {args.device}.")
            log.info("Insert a blank disc, or specify capacity manually "
                     "with -b/--bytes <int>.")
            sys.exit(1)
    disc_bytes = raw_capacity - 2 * MiB

    log.info("Scanning source...")
    scan = scan_source(source)

    slice_bytes = compute_slice_bytes(disc_bytes, scan.catalog_est,
                                      args.redundancy)
    if slice_bytes == 0:
        log.error(f"Per-disc overhead "
                  f"({human_bytes(scan.catalog_est + PAR2_AND_MISC_OVERHEAD)}) "
                  f"exceeds disc capacity ({human_bytes(disc_bytes)})")
        sys.exit(1)

    if args.sample:
        ratio = measure_compression_ratio(
            Path(args.sample).resolve(), args.compression, args.level,
            Path(args.workdir).resolve() if args.workdir else None)
        ratio_source = f"measured from {args.sample}"
    elif args.ratio is not None:
        ratio = args.ratio
        ratio_source = "manual"
    else:
        ratio = 1.0
        ratio_source = "default (no compression assumed)"

    archive_est = int(scan.total_bytes * ratio)
    n_discs = max(1, (archive_est + slice_bytes - 1) // slice_bytes)

    # Slices 1..N-1 are exactly slice_bytes; the last slice is whatever
    # remains. If the archive is an exact multiple, last_slice = slice_bytes.
    last_slice = archive_est - (n_discs - 1) * slice_bytes
    if last_slice == 0:
        last_slice = slice_bytes

    last_disc_content = (
        last_slice
        + last_slice * args.redundancy // 100
        + scan.catalog_est
        + PAR2_AND_MISC_OVERHEAD
    )
    last_disc_free = max(0, disc_bytes - last_disc_content)
    # Convert archive-byte headroom back to raw source bytes via ratio.
    last_disc_free_raw = int(last_disc_free / max(ratio, 0.001))

    log.step("Source")
    log.info(f"Path:             {source}")
    log.info(f"Size:             {human_bytes(scan.total_bytes)} "
             f"({scan.entry_count} entries)")
    log.info(f"Catalog:          ~{human_bytes(scan.catalog_est)} (estimated)")

    log.step("Disc layout")
    log.info(f"Disc capacity:    {human_bytes(disc_bytes)}")
    log.info(f"Slice size:       {human_bytes(slice_bytes)}")
    log.info(f"PAR2 redundancy:  {args.redundancy}%")
    log.info(f"Compression:      ratio {ratio:.3f} ({ratio_source})")
    log.info(f"Estimated archive: {human_bytes(archive_est)}")

    log.step("Result")
    fill_pct = last_disc_content * 100 // disc_bytes
    print(f"\n  Discs needed:    {n_discs}")
    print(f"  Last disc fill:  {human_bytes(last_disc_content)} / "
          f"{human_bytes(disc_bytes)}  ({fill_pct}%)")
    print(f"  Free on last:    {human_bytes(last_disc_free)} archive")
    if abs(ratio - 1.0) > 0.001:
        print(f"                   ~{human_bytes(last_disc_free_raw)} raw "
              f"(at ratio {ratio:.3f})")
    print()
