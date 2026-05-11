import contextlib
import shlex
import shutil
import sys
from pathlib import Path

from bd_archive import __version__
from bd_archive.archive.config import ArchiveConfig, write_readme
from bd_archive.archive.dar_archive import DarArchive
from bd_archive.archive.sizing import compute_slice_bytes, measure_compression_ratio
from bd_archive.archive.source_scan import scan_source
from bd_archive.constants import (
    DISC_END_MARGIN,
    ISO9660_VOLUME_LABEL_MAX,
    PAR2_AND_MISC_OVERHEAD,
)
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools import mkisofs
from bd_archive.tools.mediainfo import detect_disc_capacity
from bd_archive.ui.logger import log
from bd_archive.ui.prompts import prompt_yn


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

    # Workdir must exist before --sample so the sample tempdir lives
    # in the user-chosen location (e.g. tmpfs). Default-pathed workdir
    # also implies output_dir/images_dir creation here, since the
    # default workdir lives inside output_dir.
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    workdir_is_default = args.workdir is None
    work_dir = (Path(args.workdir) if args.workdir
                else output_dir / ".bd-archive-work")
    work_dir.mkdir(parents=True, exist_ok=True)

    # ── Compression-ratio preview ───────────────────────────────────────
    if args.sample:
        ratio = measure_compression_ratio(
            Path(args.sample).resolve(), args.compression, args.level,
            work_dir)
        ratio_source = f"measured from {args.sample}"
    elif args.ratio is not None:
        ratio = args.ratio
        ratio_source = "manual"
    else:
        ratio = 1.0
        ratio_source = "default (no compression assumed)"

    archive_est = int(scan.total_bytes * ratio)
    n_discs = max(1, (archive_est + slice_bytes - 1) // slice_bytes)
    last_slice = archive_est - (n_discs - 1) * slice_bytes
    if last_slice == 0:
        last_slice = slice_bytes
    last_disc_content = (
        last_slice
        + last_slice * args.redundancy // 100
        + scan.catalog_est
        + PAR2_AND_MISC_OVERHEAD
    )
    last_disc_free = max(0, sizing_target - last_disc_content)
    last_disc_free_raw = int(last_disc_free / max(ratio, 0.001))

    par2_est = slice_bytes * args.redundancy // 100
    cfg = ArchiveConfig(
        name=args.name,
        disc_bytes=raw_capacity,
        redundancy=args.redundancy,
        compression=args.compression,
        comp_level=args.level,
    )

    log.step("Source")
    log.info(f"Path:             {source}")
    log.info(f"Size:             {human_bytes(scan.total_bytes)} "
             f"({scan.entry_count} entries)")
    log.info(f"Catalog:          ~{human_bytes(scan.catalog_est)} (estimated)")

    log.step("Disc layout")
    log.info(f"Disc capacity:    {human_bytes(raw_capacity)} (writable)")
    log.info(f"Slice size:       {human_bytes(slice_bytes)}")
    log.info(f"PAR2 redundancy:  {cfg.redundancy}% (~{human_bytes(par2_est)})")
    log.info(f"Compression:      {cfg.comp_str} (ratio {ratio:.3f}, "
             f"{ratio_source})")
    log.info(f"Estimated archive: {human_bytes(archive_est)}")

    log.step("Estimate")
    fill_pct = last_disc_content * 100 // sizing_target
    log.info(f"Discs needed:     {n_discs}")
    log.info(f"Last disc fill:   {human_bytes(last_disc_content)} / "
             f"{human_bytes(sizing_target)}  ({fill_pct}%)")
    log.info(f"Free on last:     {human_bytes(last_disc_free)} archive")
    if abs(ratio - 1.0) > 0.001:
        log.info(f"                  ~{human_bytes(last_disc_free_raw)} raw "
                 f"(at ratio {ratio:.3f})")

    log.step("Configuration")
    log.info(f"Source:        {source}")
    log.info(f"Output:        {output_dir}")
    log.info(f"Workdir:       {work_dir}"
             f"{' (default)' if workdir_is_default else ' (custom)'}")

    if not args.yes and not prompt_yn("Proceed with creation?"):
        log.warn("Cancelled by user")
        if workdir_is_default:
            with contextlib.suppress(OSError):
                work_dir.rmdir()
        with contextlib.suppress(OSError):
            images_dir.rmdir()
        with contextlib.suppress(OSError):
            output_dir.rmdir()
        sys.exit(0)

    dar_archive = DarArchive(cfg.name, work_dir)
    tmp_dir = dar_archive.tmp_dir

    # ── Create dar archive ──────────────────────────────────────────────
    # par2 runs inline via dar's -E hook: par2 reads each slice while
    # its bytes are still hot in the OS page cache, eliminating most
    # SSD read traffic. Phase 3 below skips par2 and just verifies the
    # files are present.
    # %p/%b can contain spaces if the workdir or archive name does;
    # dar substitutes literally before passing to /bin/sh, so we
    # quote the macros here. %N is always digits, no quoting needed.
    log.step("Creating dar archive")
    par2_hook = (
        f'{shlex.quote(sys.executable)} -m bd_archive._par2_helper '
        f'"%p" "%b" %N {cfg.redundancy}'
    )
    dar_archive.create(source, slice_bytes, cfg.compression,
                       cfg.comp_level, par2_hook=par2_hook)

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

        # par2 was already produced via the -E hook during dar create
        # (above). Verify the files are present — a missing file means
        # the helper silently failed on this slice.
        par2_files = sorted(tmp_dir.glob(f"{slice_name}.*par2"))
        if not par2_files:
            log.error(f"par2 files missing for {slice_name} "
                      f"(_par2_helper likely failed during dar create)")
            sys.exit(1)

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
        with contextlib.suppress(OSError):
            work_dir.rmdir()

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
