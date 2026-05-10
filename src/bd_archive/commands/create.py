import shutil
import sys
from pathlib import Path

from bd_archive import __version__
from bd_archive.archive.config import ArchiveConfig, write_readme
from bd_archive.archive.dar_archive import DarArchive
from bd_archive.archive.sizing import compute_slice_bytes
from bd_archive.archive.source_scan import scan_source
from bd_archive.constants import (
    DISC_END_MARGIN,
    ISO9660_VOLUME_LABEL_MAX,
    PAR2_AND_MISC_OVERHEAD,
)
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools import mkisofs, par2
from bd_archive.tools.mediainfo import detect_disc_capacity
from bd_archive.ui.logger import log


def cmd_create(args):
    check_deps("dar", "par2", "mkisofs", "dvd+rw-mediainfo")

    max_name_len = ISO9660_VOLUME_LABEL_MAX - 5  # "_NNNN" suffix
    if len(args.name) > max_name_len:
        log.error(f"--name '{args.name}' is {len(args.name)} chars; "
                  f"max {max_name_len} (ISO9660 volume label limit "
                  f"{ISO9660_VOLUME_LABEL_MAX} minus 5-char disc suffix)")
        sys.exit(1)

    source = Path(args.source).resolve()
    if not source.is_dir():
        log.error(f"Does not exist: {source}")
        sys.exit(1)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    workdir_is_default = args.workdir is None
    work_dir = (Path(args.workdir) if args.workdir
                else output_dir / ".bd-archive-work")
    work_dir.mkdir(parents=True, exist_ok=True)

    if args.bytes is not None:
        raw_capacity = args.bytes
        log.info(f"Using manual capacity: {human_bytes(raw_capacity)}")
    else:
        raw_capacity = detect_disc_capacity(args.device)
        if raw_capacity is None:
            log.error(f"No disc detected at {args.device}.")
            log.info("Insert a blank disc, or specify capacity manually "
                     "with -b/--bytes <int>.")
            sys.exit(1)
        log.info(f"Detected {human_bytes(raw_capacity)} writable, "
                 f"sizing ISOs accordingly")

    # raw_capacity is the format-aware writable extent (post-OSA
    # reservation). DISC_END_MARGIN reserves a tiny bit more to absorb
    # ISO9660+UDF metadata growth that exceeds compute_slice_bytes's
    # estimate; the ISO file size is then re-checked against the full
    # raw_capacity below as the hard limit.
    sizing_target = raw_capacity - DISC_END_MARGIN

    log.info("Scanning source...")
    scan = scan_source(source)

    slice_bytes = compute_slice_bytes(sizing_target, scan.catalog_est,
                                      args.redundancy)
    if slice_bytes == 0:
        log.error(f"Per-disc overhead "
                  f"({human_bytes(scan.catalog_est + PAR2_AND_MISC_OVERHEAD)}) "
                  f"exceeds disc capacity ({human_bytes(raw_capacity)})")
        sys.exit(1)

    par2_est = slice_bytes * args.redundancy // 100
    cfg = ArchiveConfig(
        name=args.name,
        disc_bytes=raw_capacity,
        redundancy=args.redundancy,
        compression=args.compression,
        comp_level=args.level,
    )

    log.step("Configuration")
    log.info(f"Disc capacity: {human_bytes(raw_capacity)} (writable)")
    log.info(f"Slice size:    {human_bytes(slice_bytes)}")
    log.info(f"PAR2:          {cfg.redundancy}% (~{human_bytes(par2_est)})")
    log.info(f"Catalog:       ~{human_bytes(scan.catalog_est)} "
             f"({scan.entry_count} entries, estimated)")
    log.info(f"Compression:   {cfg.comp_str}")
    log.info(f"Source:        {source}")
    log.info(f"Output:        {output_dir}")
    log.info(f"Workdir:       {work_dir}"
             f"{' (default)' if workdir_is_default else ' (custom)'}")

    dar_archive = DarArchive(cfg.name, work_dir)
    tmp_dir = dar_archive.tmp_dir

    # ── Create dar archive ──────────────────────────────────────────────
    log.step("Creating dar archive")
    dar_archive.create(source, slice_bytes, cfg.compression, cfg.comp_level)

    slices = dar_archive.slices
    slice_count = len(slices)
    log.ok(f"{slice_count} slice(s) created")

    total_archive = 0
    for s in slices:
        sz = s.stat().st_size
        total_archive += sz
        log.info(f"  {s.name}: {human_bytes(sz)}")
    log.info(f"Total: {human_bytes(total_archive)}")

    log.info("Isolating catalog...")
    dar_archive.isolate_catalog()
    catalog_actual = sum(c.stat().st_size for c in dar_archive.catalog_files)
    log.ok(f"Catalog isolated ({human_bytes(catalog_actual)})")
    if catalog_actual > scan.catalog_est:
        log.warn(f"Catalog exceeds estimate by "
                 f"{human_bytes(catalog_actual - scan.catalog_est)} — "
                 f"per-disc fit check may fail")

    # ── Build per-disc ISOs (sequential, deletes raw files as we go) ────
    log.step("Building disc images")

    publisher = f"bd-archive v{__version__}"

    for i, slice_file in enumerate(slices, 1):
        slice_name = slice_file.name
        slice_size = slice_file.stat().st_size
        log.info(f"Disc {i}/{slice_count}: {slice_name} "
                 f"({human_bytes(slice_size)})")

        # PAR2 writes recovery files alongside the slice in tmp_dir
        log.info(f"  par2 ({cfg.redundancy}% redundancy)...")
        par2.create(slice_file, cfg.redundancy)
        par2_files = sorted(tmp_dir.glob(f"{slice_name}.*par2"))

        # README, regenerated per disc with current disc_num/total
        readme_path = tmp_dir / "README.txt"
        write_readme(readme_path, cfg, i, slice_count, slice_name)

        # Files to include in this disc's ISO
        slice_hash = Path(str(slice_file) + ".sha512")
        sources = [slice_file]
        if slice_hash.exists():
            sources.append(slice_hash)
        for cat in dar_archive.catalog_files:
            sources.append(cat)
            cat_hash = Path(str(cat) + ".sha512")
            if cat_hash.exists():
                sources.append(cat_hash)
        sources.extend(par2_files)
        sources.append(readme_path)

        # Build ISO directly from in-place files (no staging copies)
        volume_label = f"{cfg.name}_{i:04d}"
        iso_path = images_dir / f"disc_{i:04d}.iso"
        log.info(f"  building {iso_path.name}...")
        mkisofs.build(iso_path, sources, volume_label, publisher)

        # Hard fit check — the ISO file IS what gets written to disc.
        # raw_capacity is the format-aware writable extent.
        iso_size = iso_path.stat().st_size
        pct = iso_size * 100 // raw_capacity
        log.ok(f"  Disc {i}/{slice_count}: ISO {human_bytes(iso_size)} "
               f"({pct}% of {human_bytes(raw_capacity)})")
        if iso_size > raw_capacity:
            log.error(f"Disc {i} ISO ({human_bytes(iso_size)}) exceeds "
                      f"writable capacity ({human_bytes(raw_capacity)})")
            iso_path.unlink()
            sys.exit(1)

        # Cleanup this disc's intermediate files. Catalog + dar's
        # remaining files are dropped by the rmtree below.
        slice_file.unlink()
        if slice_hash.exists():
            slice_hash.unlink()
        for pf in par2_files:
            pf.unlink()
        readme_path.unlink(missing_ok=True)

    # Final cleanup: drop the entire tmp/ tree (catalog, dar internals).
    # If workdir is the default hidden one, also remove it — the only
    # thing inside was tmp/, so leaving it would just be cruft. A
    # user-supplied workdir is left alone so they can keep tmpfs mounts
    # etc. exactly as configured.
    shutil.rmtree(tmp_dir)
    if workdir_is_default:
        try:
            work_dir.rmdir()
        except OSError:
            pass

    # ── Summary ─────────────────────────────────────────────────────────
    ratio = total_archive * 100 // max(scan.total_bytes, 1)

    log.step("Summary")
    print(f"\n  Source:       {human_bytes(scan.total_bytes)}")
    print(f"  Archive:      {human_bytes(total_archive)} ({ratio}%)")
    print(f"  Discs:        {slice_count} x {human_bytes(raw_capacity)}")
    print(f"  PAR2:         {cfg.redundancy}% per disc")
    print(f"  Compression:  {cfg.comp_str}")
    print(f"  Images:       {images_dir}")
    print(f"\n  Next step:    bd-archive burn -i {output_dir}")
    print(f"  Cleanup:      rm -rf {output_dir}\n")
