"""Build a single directly readable ISO with whole-tree PAR2 protection."""

import contextlib
import shlex
import stat
import sys
import tempfile
from pathlib import Path

from bd_archive import __version__
from bd_archive.archive.raw import (
    MAX_PAR2_BLOCKS,
    RAW_CHECKSUMS,
    raw_par2_sizing,
    scan_raw_source,
    write_raw_checksums,
)
from bd_archive.constants import (
    DISC_END_MARGIN,
    PAR2_AND_MISC_OVERHEAD,
    RAW_MARKER,
    RAW_METADATA_DIR,
    RAW_PAR2_INDEX,
)
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools import mkisofs, par2
from bd_archive.tools.mediainfo import detect_disc_capacity
from bd_archive.tools.optical import resolve_device
from bd_archive.ui.logger import log
from bd_archive.ui.prompts import prompt_yn

_DAR_MODE_HINT = "Use -m dar (or --mode dar) to split the archive across multiple discs."


def cmd_create_raw(args):
    try:
        _create_raw(args)
    except ValueError as exc:
        log.error(str(exc))
        sys.exit(1)


def _create_raw(args):
    incompatible = [
        flag
        for flag, active in (
            ("--compression", args.compression not in (None, "none")),
            ("--level", args.level is not None),
            ("--base", args.base is not None),
            ("--pack-with", args.pack_with is not None),
            ("--min-last-disc-fill", args.min_last_disc_fill != 0),
            ("--ratio", args.ratio is not None),
            ("--sample", args.sample is not None),
        )
        if active
    ]
    if incompatible:
        raise ValueError(
            f"Raw mode cannot be combined with {', '.join(incompatible)}; "
            "use -m dar for these options"
        )
    if args.redundancy is not None and not 1 <= args.redundancy <= 100:
        raise ValueError(f"--redundancy must be 1-100, got {args.redundancy}")
    if args.bytes is not None and args.bytes <= 0:
        raise ValueError("--bytes must be positive")

    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    work = Path(args.workdir).resolve() if args.workdir else output / ".bd-archive-work"
    if not source.is_dir():
        raise ValueError(f"Source directory does not exist: {source}")
    for path in (output, work):
        if path.is_relative_to(source) or source.is_relative_to(path):
            raise ValueError(f"Source and output/workdir must not overlap: {source} / {path}")
    images = output / "images"
    if list(images.glob("disc_*.iso")):
        raise ValueError(f"{images} already contains disc images; choose another output directory")

    deps = ["par2", "mkisofs"]
    if args.bytes is None:
        deps.append("dvd+rw-mediainfo")
    check_deps(*deps)
    capacity = args.bytes
    if capacity is None:
        device = resolve_device(args.device)
        capacity = detect_disc_capacity(device)
        if capacity is None or capacity <= 0:
            raise ValueError(f"No writable disc detected at {device}; insert one or use --bytes")

    log.step("Scanning source for a raw data disc")
    inventory = scan_raw_source(source)
    files = [entry for entry in inventory if stat.S_ISREG(entry.mode)]
    total = sum(entry.size for entry in files)
    if total == 0:
        raise ValueError("Raw mode needs at least one non-empty file for PAR2 protection")

    publisher = f"bd-archive v{__version__}"
    label = f"{args.name}_RAW"
    # Count the real directory/filesystem overhead before expensive PAR2 work.
    payload_iso_size = mkisofs.estimate_size([("", source)], label, publisher, rock_ridge=True)
    if payload_iso_size > capacity:
        raise ValueError(
            "Source does not fit on one disc even without PAR2; use a larger disc. "
            + _DAR_MODE_HINT
        )
    redundancy = (
        "automatic (remaining disc capacity)" if args.redundancy is None else f"{args.redundancy}%"
    )
    work.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="raw-", dir=work) as scratch:
            metadata = Path(scratch) / RAW_METADATA_DIR
            metadata.mkdir()
            (metadata / RAW_MARKER).write_text("bd-archive raw disc format 1\n", encoding="utf-8")
            (metadata / "README.txt").write_text(
                f"{args.name} — directly readable data disc\n"
                f"Created by {publisher}; PAR2 redundancy: {redundancy}\n\n"
                "Open/play files directly from the disc. No dar or extraction is needed.\n"
                "Verify with: bd-archive verify <disc-mountpoint-or-iso>\n"
                "checksums.sha512 covers every source file, including empty files.\n"
                "To check hashes, change into the disc root and run:\n"
                "  sha512sum -c .bd-archive/checksums.sha512\n"
                "PAR2 protects non-empty file contents, not filesystem metadata or empty files.\n"
                "To repair, copy the entire disc (including .bd-archive/) to a writable\n"
                "directory, change into that directory and run:\n"
                "  chmod -R u+rwX .  # if copied files are still read-only\n"
                "  par2 repair -B. .bd-archive/recovery.par2\n"
                "The read-only disc itself cannot be repaired in place.\n",
                encoding="utf-8",
            )
            manifest = metadata / RAW_CHECKSUMS
            # Include checksum text and metadata in capacity planning without
            # reading all payload bytes before the confirmation prompt.
            write_raw_checksums(source, inventory, manifest, placeholder=True)
            entries = [("", source), (RAW_METADATA_DIR, metadata)]
            sizing = None
            recovery_blocks = None
            if args.redundancy is None:
                sizing = raw_par2_sizing(inventory, capacity, capacity - payload_iso_size)
                recovery_blocks, estimate = _plan_auto_recovery(
                    metadata, entries, label, publisher, sizing, capacity
                )
                recovery_bytes = recovery_blocks * sizing.block_size
                redundancy = (
                    f"automatic: {human_bytes(recovery_bytes)} "
                    f"({100 * recovery_bytes / total:.2f}%)"
                )
            else:
                estimate = (
                    mkisofs.estimate_size(entries, label, publisher, rock_ridge=True)
                    + (total * args.redundancy + 99) // 100
                    + PAR2_AND_MISC_OVERHEAD
                    + DISC_END_MARGIN
                )
            log.info(f"Source:          {source}")
            log.info(f"Files:           {len(files)} ({human_bytes(total)})")
            log.info(f"Disc capacity:   {human_bytes(capacity)}")
            log.info(f"PAR2 redundancy: {redundancy} across all non-empty files")
            log.info(f"Estimated ISO:   {human_bytes(estimate)} (including overhead allowance)")
            log.info(
                "Layout: original paths at disc root; "
                "recovery data and checksums.sha512 in .bd-archive/"
            )
            log.info("Keep the source unchanged until creation finishes.")
            if estimate > capacity:
                log.warn(
                    "Estimated size exceeds capacity; the exact size will be checked after PAR2. "
                    + _DAR_MODE_HINT
                )
            if not args.yes and not prompt_yn("Create directly readable disc image?"):
                log.warn("Cancelled by user")
                return

            log.step("Creating SHA-512 checksums for all source files")
            write_raw_checksums(source, inventory, manifest)
            if scan_raw_source(source) != inventory:
                raise ValueError("Source changed while hashing; retry with an unchanged source")
            index = metadata / RAW_PAR2_INDEX
            log.step("Creating PAR2 recovery data")
            if sizing is not None:
                par2.create_tree(
                    source, index, block_size=sizing.block_size, recovery_blocks=recovery_blocks
                )
            else:
                par2.create_tree(source, index, args.redundancy)
            if not index.is_file() or not list(metadata.glob("recovery.vol*.par2")):
                raise ValueError("PAR2 did not produce both an index and recovery data")
            if scan_raw_source(source) != inventory:
                raise ValueError(
                    "Source changed while creating PAR2; retry with an unchanged source"
                )

            iso_size = mkisofs.estimate_size(entries, label, publisher, rock_ridge=True)
            if iso_size > capacity:
                raise ValueError(
                    f"Data + PAR2 ISO ({human_bytes(iso_size)}) exceeds disc capacity "
                    f"({human_bytes(capacity)}); use a larger disc or reduce -r. " + _DAR_MODE_HINT
                )
            images.mkdir(parents=True, exist_ok=True)
            # Only publish an image burn can discover after all checks passed.
            with tempfile.TemporaryDirectory(prefix=".raw-build-", dir=images) as build_dir:
                pending = Path(build_dir) / "disc.iso"
                log.step("Building directly readable disc image")
                mkisofs.build(pending, entries, label, publisher, rock_ridge=True)
                if pending.stat().st_size > capacity:
                    raise ValueError(
                        "Built ISO exceeds disc capacity; no burnable image was saved. "
                        + _DAR_MODE_HINT
                    )
                if scan_raw_source(source) != inventory:
                    raise ValueError(
                        "Source changed while building the ISO; retry with an unchanged source"
                    )
                iso_size = pending.stat().st_size
                pending.rename(images / "disc_0001.iso")
    finally:
        if args.workdir is None:
            with contextlib.suppress(OSError):
                work.rmdir()

    log.ok(f"Raw disc ready: {images / 'disc_0001.iso'} ({human_bytes(iso_size)})")
    log.info(f"Next step: bd-archive burn -i {shlex.quote(str(output))}")


def _plan_auto_recovery(metadata, entries, label, publisher, sizing, capacity):
    """Size sparse stand-ins, then remove them before real PAR2 creation.

    mkisofs accounts for sector rounding, directory records and multi-extent
    files. No payload or recovery bytes are read/written during this search.
    Only our two temporary placeholder files are removed.
    """
    index = metadata / RAW_PAR2_INDEX
    volume = metadata / "recovery.vol00000+32768.par2"
    target = capacity - DISC_END_MARGIN
    best = 0
    estimate = 0
    low, high = 1, MAX_PAR2_BLOCKS
    try:
        while low <= high:
            count = (low + high) // 2
            for path, size in zip((index, volume), sizing.file_sizes(count), strict=True):
                with path.open("wb") as placeholder:
                    placeholder.truncate(size)
            size = mkisofs.estimate_size(entries, label, publisher, rock_ridge=True)
            if size <= target:
                best, estimate = count, size
                low = count + 1
            else:
                high = count - 1
    finally:
        index.unlink(missing_ok=True)
        volume.unlink(missing_ok=True)
    if not best:
        raise ValueError(
            "Not enough free disc capacity for checksums, PAR2 recovery and the safety margin; "
            "reduce the source size or use a larger disc. " + _DAR_MODE_HINT
        )
    return best, estimate
