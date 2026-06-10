import contextlib
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path

from bd_archive import __version__
from bd_archive.archive.config import ArchiveConfig, write_readme
from bd_archive.archive.dar_archive import DarArchive, dar_basename, parse_dar_filename
from bd_archive.archive.sizing import compute_slice_bytes, measure_compression_ratio
from bd_archive.archive.source_scan import (
    SourceFile,
    list_source_files,
    scan_delta_bytes,
    scan_source,
)
from bd_archive.constants import (
    DISC_END_MARGIN,
    ISO9660_LABEL_NAME_MAX,
    ISO9660_VOLUME_LABEL_MAX,
    PAR2_AND_MISC_OVERHEAD,
)
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools import mkisofs
from bd_archive.tools.dar import list_catalog_paths
from bd_archive.tools.mediainfo import detect_disc_capacity
from bd_archive.tools.optical import resolve_device
from bd_archive.ui.logger import log
from bd_archive.ui.prompts import prompt_yn


def _resolve_base(base_arg: str, archive_name: str) -> tuple[Path, int]:
    """Validate and unpack a --base argument.

    Returns ``(catalog_basename_path, base_generation)``. Raises
    SystemExit with a user-readable error if the path is missing, the
    filename doesn't look like a dar catalog slice, or the embedded
    archive name disagrees with ``-n``.
    """
    base_path = Path(base_arg).resolve()
    if not base_path.is_file():
        log.error(f"--base path does not exist: {base_path}")
        sys.exit(1)
    parsed = parse_dar_filename(base_path.name)
    if parsed is None or not parsed[2]:
        log.error(
            f"--base must point to a dar catalog slice "
            f"(<name>[-gen<N>]-catalog.NNNN.dar); got '{base_path.name}'"
        )
        sys.exit(1)
    base_name, base_gen, _ = parsed
    if base_name != archive_name:
        log.error(
            f"--base belongs to archive '{base_name}' but -n is '{archive_name}'. "
            f"Chain identity is the archive name; keep it consistent across generations."
        )
        sys.exit(1)
    # Strip the ".NNNN.dar" slice suffix to get the catalog basename
    # suitable for `-A`. dar resolves the actual slice file(s) from the
    # basename, so we never hand it the raw filename.
    return base_path.parent / dar_basename(base_path.name), base_gen


def cmd_create(args):
    check_deps("dar", "par2", "mkisofs", "dvd+rw-mediainfo")

    if not 0 <= args.min_last_disc_fill <= 100:
        log.error(f"--min-last-disc-fill must be 0-100, got {args.min_last_disc_fill}")
        sys.exit(1)

    # Hard cap matches the pre-Phase-2 label format (32 - 5) so existing
    # archive names that lived right up against the old limit still work.
    # Names longer than ISO9660_LABEL_NAME_MAX (23) get truncated in the
    # volume label only; filenames inside the ISO keep the full name.
    legacy_max_name_len = ISO9660_VOLUME_LABEL_MAX - 5
    if len(args.name) > legacy_max_name_len:
        log.error(f"--name '{args.name}' is {len(args.name)} chars; max {legacy_max_name_len}")
        sys.exit(1)
    if len(args.name) > ISO9660_LABEL_NAME_MAX:
        log.warn(
            f"--name '{args.name}' is {len(args.name)} chars; "
            f"volume labels will be truncated to {ISO9660_LABEL_NAME_MAX} chars "
            f"('{args.name[:ISO9660_LABEL_NAME_MAX]}'). Filenames on disc keep the full name."
        )

    # --base: parse and validate. Sets `ref_catalog` (dar -A argument)
    # and `generation` (current run's gen number = base_gen + 1).
    ref_catalog: Path | None = None
    generation = 1
    if args.base is not None:
        ref_catalog, base_gen = _resolve_base(args.base, args.name)
        generation = base_gen + 1
        log.info(f"Incremental against: {ref_catalog.name} (Gen {base_gen}) → new Gen {generation}")

    source = Path(args.source).resolve()
    if not source.is_dir():
        log.error(f"Does not exist: {source}")
        sys.exit(1)

    if args.bytes is not None:
        raw_capacity = args.bytes
        log.info(f"Using manual capacity: {human_bytes(raw_capacity)}")
    else:
        device = resolve_device(args.device)
        raw_capacity = detect_disc_capacity(device)
        if raw_capacity is None:
            log.error(f"No disc detected at {device}.")
            log.info("Insert a blank disc, or specify capacity manually with -b/--bytes <int>.")
            sys.exit(1)
        log.info(f"Detected {human_bytes(raw_capacity)} writable, sizing ISOs accordingly")

    # raw_capacity is the format-aware writable extent (post-OSA
    # reservation). DISC_END_MARGIN reserves a tiny bit more to absorb
    # ISO9660+UDF metadata growth that exceeds compute_slice_bytes's
    # estimate; the ISO file size is then re-checked against the full
    # raw_capacity below as the hard limit.
    sizing_target = raw_capacity - DISC_END_MARGIN

    log.info("Scanning source...")
    scan = scan_source(source)

    slice_bytes = compute_slice_bytes(sizing_target, scan.catalog_est, args.redundancy)
    if slice_bytes == 0:
        log.error(
            f"Per-disc overhead "
            f"({human_bytes(scan.catalog_est + PAR2_AND_MISC_OVERHEAD)}) "
            f"exceeds disc capacity ({human_bytes(raw_capacity)})"
        )
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
    work_dir = Path(args.workdir) if args.workdir else output_dir / ".bd-archive-work"
    work_dir.mkdir(parents=True, exist_ok=True)

    # ── Compression-ratio preview ───────────────────────────────────────
    if args.sample:
        ratio = measure_compression_ratio(
            Path(args.sample).resolve(), args.compression, args.level, work_dir
        )
        ratio_source = f"measured from {args.sample}"
    elif args.ratio is not None:
        ratio = args.ratio
        ratio_source = "manual"
    else:
        ratio = 1.0
        ratio_source = "default (no compression assumed)"

    # For an incremental, the data payload is only new/changed files;
    # estimating against the full source overstates disc count and
    # last-disc fill. Re-scan the source against the base catalog to
    # get a delta-aware payload size. mtime is a heuristic — see
    # scan_delta_bytes for why it's good enough for previews.
    base_paths: set[str] = set()
    if ref_catalog is not None:
        base_paths = list_catalog_paths(ref_catalog)
        # Stat the user-supplied catalog slice file directly — its mtime
        # is the timestamp dar wrote the catalog at, which we use as the
        # cutoff for "modified since base".
        base_mtime = Path(args.base).resolve().stat().st_mtime
        delta_bytes = scan_delta_bytes(source, base_paths, base_mtime)
        archive_est = int(delta_bytes * ratio)
    else:
        archive_est = int(scan.total_bytes * ratio)

    def _layout(est: int) -> tuple[int, int, int]:
        """(n_discs, last_disc_content, last_fill_pct) for a given archive size."""
        if est == 0:
            # Incremental with no new file data — catalog + par2 overhead
            # still take up a disc, but the data portion is empty.
            overhead = scan.catalog_est + PAR2_AND_MISC_OVERHEAD
            return 1, overhead, overhead * 100 // sizing_target
        n = max(1, (est + slice_bytes - 1) // slice_bytes)
        last_sl = est - (n - 1) * slice_bytes
        if last_sl == 0:
            last_sl = slice_bytes
        last_content = (
            last_sl + last_sl * args.redundancy // 100 + scan.catalog_est + PAR2_AND_MISC_OVERHEAD
        )
        return n, last_content, last_content * 100 // sizing_target

    n_discs, last_disc_content, fill_pct = _layout(archive_est)

    # ── Auto-defer (--min-last-disc-fill) ───────────────────────────────
    # When the last disc would be too empty, push newest files to a
    # future generation so this set "rounds down" to fewer discs with
    # higher fill. Pool is "files truly new vs. base catalog" when
    # incremental, "all files" when full (with warning — those files
    # won't be archived anywhere until a later incremental run picks
    # them up).
    deferred_files: list[SourceFile] = []
    if args.min_last_disc_fill > 0 and fill_pct < args.min_last_disc_fill:
        if ref_catalog is not None:
            pool = [f for f in list_source_files(source) if f.rel_path not in base_paths]
            pool_kind = "files not in base catalog"
        else:
            pool = list_source_files(source)
            pool_kind = "all source files"
            log.warn(
                "--min-last-disc-fill on a Full archive defers files that will "
                "NOT be archived until a future incremental run picks them up."
            )
        pool.sort(key=lambda f: f.mtime, reverse=True)

        # Initialise loop-mutated state to the pre-defer layout so the
        # "pool exhausted / threshold unreachable" fallback below has
        # values to read even when the pool is empty (all source files
        # are already in the base catalog).
        cum_size = 0
        reached = False
        new_n, new_last, new_fill = n_discs, last_disc_content, fill_pct
        for f in pool:
            cum_size += f.size
            new_est = max(0, archive_est - int(cum_size * ratio))
            new_n, new_last, new_fill = _layout(new_est) if new_est > 0 else (0, 0, 0)
            deferred_files.append(f)
            if new_est == 0:
                # Pool would empty the archive entirely — stop here.
                break
            if new_fill >= args.min_last_disc_fill:
                archive_est, n_discs, last_disc_content, fill_pct = (
                    new_est,
                    new_n,
                    new_last,
                    new_fill,
                )
                reached = True
                break

        if not reached:
            if not pool:
                # Nothing was deferrable (incremental + base already
                # contains every source file). Keep original layout
                # and let dar handle the delta-empty run — its
                # archive will contain only deletion markers if any.
                log.info(
                    "Auto-defer pool empty (nothing new vs base); "
                    "proceeding with the original layout."
                )
            else:
                log.warn(
                    f"--min-last-disc-fill {args.min_last_disc_fill}% not reachable; "
                    f"pool ({len(pool)} candidate file(s), {pool_kind}) exhausted "
                    f"after deferring {human_bytes(cum_size)}. Proceeding with "
                    f"what we have."
                )
                new_est = archive_est - int(cum_size * ratio)
                if new_est > 0:
                    archive_est, n_discs, last_disc_content, fill_pct = (
                        new_est,
                        new_n,
                        new_last,
                        new_fill,
                    )
                else:
                    log.error(
                        "Deferring all candidates would leave 0 bytes to archive. "
                        "Lower --min-last-disc-fill or skip the run."
                    )
                    sys.exit(1)

    last_disc_free = max(0, sizing_target - last_disc_content)
    last_disc_free_raw = int(last_disc_free / max(ratio, 0.001))

    par2_est = slice_bytes * args.redundancy // 100
    cfg = ArchiveConfig(
        name=args.name,
        disc_bytes=raw_capacity,
        redundancy=args.redundancy,
        compression=args.compression,
        comp_level=args.level,
        generation=generation,
    )

    log.step("Source")
    log.info(f"Path:             {source}")
    log.info(f"Size:             {human_bytes(scan.total_bytes)} ({scan.entry_count} entries)")
    log.info(f"Catalog:          ~{human_bytes(scan.catalog_est)} (estimated)")

    log.step("Disc layout")
    log.info(f"Disc capacity:    {human_bytes(raw_capacity)} (writable)")
    log.info(f"Slice size:       {human_bytes(slice_bytes)}")
    log.info(f"PAR2 redundancy:  {cfg.redundancy}% (~{human_bytes(par2_est)})")
    log.info(f"Compression:      {cfg.comp_str} (ratio {ratio:.3f}, {ratio_source})")
    archive_kind = "delta vs base" if ref_catalog is not None else "full source"
    log.info(f"Estimated archive: {human_bytes(archive_est)} ({archive_kind})")

    log.step("Estimate")
    log.info(f"Discs needed:     {n_discs}")
    log.info(
        f"Last disc fill:   {human_bytes(last_disc_content)} / "
        f"{human_bytes(sizing_target)}  ({fill_pct}%)"
    )
    log.info(f"Free on last:     {human_bytes(last_disc_free)} archive")
    if abs(ratio - 1.0) > 0.001:
        log.info(f"                  ~{human_bytes(last_disc_free_raw)} raw (at ratio {ratio:.3f})")

    if deferred_files:
        defer_bytes = sum(f.size for f in deferred_files)
        oldest_deferred = min(f.mtime for f in deferred_files)
        log.step(f"Auto-defer (--min-last-disc-fill {args.min_last_disc_fill}%)")
        log.info(f"Files deferred:   {len(deferred_files)}")
        log.info(f"Bytes deferred:   {human_bytes(defer_bytes)} (raw)")
        oldest_dt = datetime.fromtimestamp(oldest_deferred)
        log.info(f"Oldest deferred:  mtime {oldest_dt:%Y-%m-%d %H:%M}")
        sample = deferred_files[:3]
        for f in sample:
            log.info(f"  - {f.rel_path}")
        if len(deferred_files) > len(sample):
            log.info(f"  - ... and {len(deferred_files) - len(sample)} more")

    log.step("Configuration")
    log.info(f"Source:        {source}")
    log.info(f"Output:        {output_dir}")
    log.info(f"Workdir:       {work_dir}{' (default)' if workdir_is_default else ' (custom)'}")
    log.info(f"Generation:    {cfg.generation} ({'incremental' if ref_catalog else 'full'})")
    if ref_catalog is not None:
        log.info(f"Base catalog:  {args.base}")

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

    dar_archive = DarArchive(cfg.dar_name, work_dir)
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
        f'{shlex.quote(sys.executable)} -m bd_archive._par2_helper "%p" "%b" %N {cfg.redundancy}'
    )
    dar_archive.create(
        source,
        slice_bytes,
        cfg.compression,
        cfg.comp_level,
        par2_hook=par2_hook,
        ref_catalog=ref_catalog,
        excludes=[f.rel_path for f in deferred_files] if deferred_files else None,
    )

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
        log.warn(
            f"Catalog exceeds estimate by "
            f"{human_bytes(catalog_actual - scan.catalog_est)} — "
            f"per-disc fit check may fail"
        )

    # ── Build per-disc ISOs (sequential, deletes raw files as we go) ────
    log.step("Building disc images")

    publisher = f"bd-archive v{__version__}"

    for i, slice_file in enumerate(slices, 1):
        slice_name = slice_file.name
        slice_size = slice_file.stat().st_size
        log.info(f"Disc {i}/{slice_count}: {slice_name} ({human_bytes(slice_size)})")

        # par2 was already produced via the -E hook during dar create
        # (above). Verify the files are present — a missing file means
        # the helper silently failed on this slice.
        par2_files = sorted(tmp_dir.glob(f"{slice_name}.*par2"))
        if not par2_files:
            log.error(
                f"par2 files missing for {slice_name} "
                f"(_par2_helper likely failed during dar create)"
            )
            sys.exit(1)

        # README, regenerated per disc with current disc_num/total
        readme_path = tmp_dir / "README.txt"
        write_readme(readme_path, cfg, i, slice_count, slice_name)

        # Files to include in this disc's ISO
        slice_hash = Path(str(slice_file) + ".sha512")
        sources = [slice_file]
        if slice_hash.exists():
            sources.append(slice_hash)
        # Catalog goes onto Disc 1 only. The master catalog at the end of
        # the last slice (dar default) plus this isolated copy on Disc 1
        # gives two spatially separated copies per archive set. Replicating
        # on every disc was redundant and grew unboundedly with file count.
        if i == 1:
            for cat in dar_archive.catalog_files:
                sources.append(cat)
                cat_hash = Path(str(cat) + ".sha512")
                if cat_hash.exists():
                    sources.append(cat_hash)
        sources.extend(par2_files)
        sources.append(readme_path)

        # Everything lives inside the archive's top-level folder, named
        # after the dar basename so extract can resolve it directly.
        # The per-archive README sits in there too — no top-level files.
        entries = [(f"{cfg.dar_name}/{p.name}", p) for p in sources]

        # Build ISO directly from in-place files (no staging copies).
        # Label is "<truncated_name>_G<NN>_<NNNN>" — name budget derived
        # from the actual suffix so variants (e.g. the packed-disc "+"
        # marker) always fit the 32-byte ISO9660 limit.
        label_suffix = f"_G{cfg.generation:02d}_{i:04d}"
        name_budget = ISO9660_VOLUME_LABEL_MAX - len(label_suffix)
        volume_label = f"{cfg.name[:name_budget]}{label_suffix}"
        iso_path = images_dir / f"disc_{i:04d}.iso"
        log.info(f"  building {iso_path.name}...")
        mkisofs.build(iso_path, entries, volume_label, publisher)

        # Hard fit check — the ISO file IS what gets written to disc.
        # raw_capacity is the format-aware writable extent.
        iso_size = iso_path.stat().st_size
        pct = iso_size * 100 // raw_capacity
        log.ok(
            f"  Disc {i}/{slice_count}: ISO {human_bytes(iso_size)} "
            f"({pct}% of {human_bytes(raw_capacity)})"
        )
        if iso_size > raw_capacity:
            log.error(
                f"Disc {i} ISO ({human_bytes(iso_size)}) exceeds "
                f"writable capacity ({human_bytes(raw_capacity)})"
            )
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

    # Persist the isolated catalog alongside images/ for two reasons:
    #   1. It survives `output_dir` being burned + the local images/
    #      being deleted — user keeps the catalog as part of their
    #      regular backup.
    #   2. Future incremental generations will reference this file via
    #      `--base` (not implemented yet in this phase, but the artifact
    #      needs to exist from this phase onward).
    for cat in dar_archive.catalog_files:
        shutil.copy2(cat, output_dir / cat.name)
        cat_hash = Path(str(cat) + ".sha512")
        if cat_hash.exists():
            shutil.copy2(cat_hash, output_dir / cat_hash.name)
    catalog_persisted = sorted(output_dir.glob(f"{cfg.dar_name}-catalog.*.dar"))
    if catalog_persisted:
        log.info(f"Catalog persisted: {catalog_persisted[0].parent}/{cfg.dar_name}-catalog.*.dar")

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
    print(f"  Catalog:      {output_dir}/{cfg.dar_name}-catalog.*.dar")
    print(f"\n  Next step:    bd-archive burn -i {output_dir}")
    print(f"  Cleanup:      rm -rf {output_dir}\n")
