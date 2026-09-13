"""Prepare a directly readable disc folder or ISO with whole-tree PAR2 protection."""

import contextlib
import shlex
import stat
import sys
import tempfile
from pathlib import Path

from bd_archive import __version__
from bd_archive.archive.config import raw_readme
from bd_archive.archive.disc_folder import check_output_available, prepare_folder, save_disc_set
from bd_archive.archive.raw import (
    MAX_PAR2_BLOCKS,
    RAW_CHECKSUMS,
    raw_par2_sizing,
    scan_raw_source,
    validate_raw_source_name,
    write_raw_checksums,
)
from bd_archive.archive.sizing import disc_write_bytes
from bd_archive.constants import (
    DISC_END_MARGIN,
    PAR2_AND_MISC_OVERHEAD,
    RAW_PAR2_INDEX,
)
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools import mkisofs, par2
from bd_archive.tools.mediainfo import detect_disc_capacity
from bd_archive.tools.optical import resolve_device
from bd_archive.tools.software import software_info
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
    if args.redundancy is not None and not 0 <= args.redundancy <= 100:
        raise ValueError(f"--redundancy must be 0-100 or none, got {args.redundancy}")
    recovery_enabled = args.redundancy != 0
    if args.bytes is not None and args.bytes <= 0:
        raise ValueError("--bytes must be positive")

    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    work = Path(args.workdir).resolve() if args.workdir else output / ".bd-archive-work"
    if not source.is_dir():
        raise ValueError(f"Source directory does not exist: {source}")
    validate_raw_source_name(source)
    for path in (output, work):
        if path.is_relative_to(source) or source.is_relative_to(path):
            raise ValueError(f"Source and output/workdir must not overlap: {source} / {path}")
    check_output_available(output)
    images = output / ("images" if args.iso else "discs")

    deps = ["mkisofs"]
    if recovery_enabled:
        deps.append("par2")
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
    if total == 0 and recovery_enabled:
        raise ValueError("Raw mode needs at least one non-empty file for PAR2 protection")

    publisher = f"bd-archive v{__version__}"
    label = f"{args.name}_RAW"
    # Count the real directory/filesystem overhead before expensive PAR2 work.
    payload_entries = [(source.name, source)]
    payload_iso_size = mkisofs.estimate_size(payload_entries, label, publisher, rock_ridge=True)
    if disc_write_bytes(payload_iso_size) > capacity:
        raise ValueError(
            "Source does not fit on one disc even without PAR2; use a larger disc. "
            + _DAR_MODE_HINT
        )
    redundancy = (
        "automatic (remaining disc capacity)" if args.redundancy is None else f"{args.redundancy}%"
    )
    software = software_info(deps)
    work.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="raw-", dir=work) as scratch:
            metadata = Path(scratch) / "metadata"
            metadata.mkdir()
            (metadata / "README.txt").write_text(
                raw_readme(args.name, redundancy, recovery_enabled, software),
                encoding="utf-8",
            )
            manifest = metadata / RAW_CHECKSUMS
            # Include checksum text and metadata in capacity planning without
            # reading all payload bytes before the confirmation prompt.
            write_raw_checksums(
                source, inventory, manifest, placeholder=True, path_prefix=source.name
            )
            entries = [*payload_entries, ("", metadata)]
            sizing = None
            recovery_blocks = None
            if args.redundancy is None:
                sizing = raw_par2_sizing(
                    inventory, capacity, capacity - payload_iso_size, path_prefix=source.name
                )
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
                    disc_write_bytes(
                        mkisofs.estimate_size(entries, label, publisher, rock_ridge=True)
                        + (total * args.redundancy + 99) // 100
                        + (PAR2_AND_MISC_OVERHEAD if recovery_enabled else 0)
                    )
                    + DISC_END_MARGIN
                )
            log.info(f"Source:          {source}")
            log.info(f"Files:           {len(files)} ({human_bytes(total)})")
            log.info(f"Disc capacity:   {human_bytes(capacity)}")
            if recovery_enabled:
                log.info(f"PAR2 redundancy: {redundancy} across all non-empty files")
            else:
                log.info("PAR2 disabled; verification uses SHA-512 checksums.")
            log.info(f"Estimated disc:  {human_bytes(estimate)} (including overhead allowance)")
            log.info(
                f"Layout: source folder {source.name}/ at disc root; "
                "README.txt, checksums.sha512 and recovery files beside it"
            )
            log.info("Keep the source unchanged until creation finishes.")
            if estimate > capacity:
                log.warn(
                    "Estimated size exceeds capacity; the exact size will be checked before build. "
                    + _DAR_MODE_HINT
                )
            if not args.yes and not prompt_yn("Prepare directly readable disc?"):
                log.warn("Cancelled by user")
                return

            log.step("Creating SHA-512 checksums for all source files")
            write_raw_checksums(source, inventory, manifest, path_prefix=source.name)
            if scan_raw_source(source) != inventory:
                raise ValueError("Source changed while hashing; retry with an unchanged source")
            if recovery_enabled:
                index = metadata / RAW_PAR2_INDEX
                log.step("Creating PAR2 recovery data")
                if sizing is not None:
                    par2.create_tree(
                        source,
                        index,
                        block_size=sizing.block_size,
                        recovery_blocks=recovery_blocks,
                        base_dir=source.parent,
                    )
                else:
                    par2.create_tree(source, index, args.redundancy, base_dir=source.parent)
                if not index.is_file() or not list(metadata.glob("recovery.vol*.par2")):
                    raise ValueError("PAR2 did not produce both an index and recovery data")
                if scan_raw_source(source) != inventory:
                    raise ValueError(
                        "Source changed while creating PAR2; retry with an unchanged source"
                    )

            (metadata / "README.txt").write_text(
                raw_readme(args.name, redundancy, recovery_enabled, software), encoding="utf-8"
            )
            iso_size = mkisofs.estimate_size(entries, label, publisher, rock_ridge=True)
            if disc_write_bytes(iso_size) > capacity:
                raise ValueError(
                    f"Required write size ({disc_write_bytes(iso_size)} bytes including "
                    f"32-KiB write padding) exceeds disc capacity ({capacity} bytes); "
                    "use a larger disc or reduce -r. " + _DAR_MODE_HINT
                )
            images.mkdir(parents=True, exist_ok=True)
            if not args.iso:
                log.step("Preparing directly readable disc folder")
                try:
                    folder = prepare_folder(
                        images / "disc_0001",
                        entries,
                        label,
                        publisher,
                        capacity,
                        rock_ridge=True,
                        move_sources=set(metadata.iterdir()),
                    )
                except ValueError as exc:
                    raise ValueError(f"{exc}. {_DAR_MODE_HINT}") from exc
                if scan_raw_source(source) != inventory:
                    raise ValueError("Source changed while copying; retry with an unchanged source")
                iso_size = folder.image_bytes
                save_disc_set(images, [folder])
            else:
                # Only publish an image burn can discover after all checks passed.
                with tempfile.TemporaryDirectory(prefix=".raw-build-", dir=images) as build_dir:
                    pending = Path(build_dir) / "disc.iso"
                    log.step("Building directly readable disc image")
                    mkisofs.build(pending, entries, label, publisher, rock_ridge=True)
                    if disc_write_bytes(pending.stat().st_size) > capacity:
                        raise ValueError(
                            "Built ISO exceeds disc capacity including 32-KiB write padding; "
                            "no burnable image was saved. " + _DAR_MODE_HINT
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

    disc_path = images / ("disc_0001.iso" if args.iso else "disc_0001")
    log.ok(f"Raw disc ready: {disc_path} ({human_bytes(iso_size)})")
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
            size = disc_write_bytes(
                mkisofs.estimate_size(entries, label, publisher, rock_ridge=True)
            )
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
