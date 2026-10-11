"""Plan one directly readable disc from a source and build it with whole-disc PAR2 protection."""

import contextlib
import shlex
import shutil
import stat
import sys
import tempfile
import textwrap
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from bd_archive import __version__
from bd_archive.archive.content_dates import ContentDate, scan_dates
from bd_archive.archive.disc_folder import (
    DiscFolder,
    check_output_available,
    save_disc_set,
    tree_signature,
)
from bd_archive.archive.hardlinks import HardlinkTracker
from bd_archive.archive.plan import STRATEGIES, Plan, disorder, make_units, proposals
from bd_archive.archive.plan_sizing import (
    RAW_V3_MARKER_TEXT,
    DiscLayout,
    choose_recovery,
    measure,
    plan_recovery,
    readme_allowance,
)
from bd_archive.archive.raw import (
    RAW_CHECKSUMS,
    RawEntry,
    is_reserved_root_name,
    scan_attachment,
    scan_raw_source,
    write_raw_checksums,
)
from bd_archive.archive.readme import write_readme
from bd_archive.archive.share import FILL_MIN_PERMILLE, fill_skipped_message
from bd_archive.archive.sizing import disc_write_bytes
from bd_archive.archive.staging import StageItem, check_space, check_unit, remove_units, stage
from bd_archive.constants import (
    DISC_END_MARGIN,
    RAW_METADATA_DIR,
    RAW_PAR2_INDEX,
    RAW_README,
    RAW_V3_MARKER,
    MiB,
)
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools import mkisofs, par2
from bd_archive.tools.mediainfo import detect_disc_capacity
from bd_archive.tools.optical import resolve_device
from bd_archive.tools.software import software_info
from bd_archive.ui.logger import log
from bd_archive.ui.prompts import prompt_yn, styled_input

PACKING_TEXT = {
    "efficient": "by size",
    "balanced": "in order, backfilling gaps",
    "ordered": "strictly in order",
}

MAX_MEASURED_PLANS = 64  # per strategy


def date_text(value: int) -> str:
    try:
        return datetime.fromtimestamp(value // 10**9, UTC).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "out-of-range date"


def section(title: str):
    """Start a preview block: a blank line, then its title."""
    log.blank()
    log.info(title)


def field(key: str, value):
    """One aligned key/value line of a preview block."""
    log.info(f"  {key:<20} : {value}")


def order_consistency(score: float) -> int:
    """Map the disorder score (0 = strictly in fill order) to a whole percentage.

    Only a perfectly ordered plan reports 100%; any departure stays below it.
    """
    if score <= 0:
        return 100
    return max(0, min(99, int((1 - score) * 100)))


def checked_proposals(units, candidates, capacity, limit, layout: DiscLayout):
    """Measure each strategy's candidates, longest prefix first; keep the first that qualifies.

    A plan qualifies when every disc fits and, with a free-space limit, no disc
    exceeds it. Optimistic search weights are refined without splitting a unit.
    """
    cache = {}
    # A disc needs non-empty data for PAR2; attachments with data provide it.
    protected = layout.recovery is not None and not any(
        e.size for e in layout.attachments if stat.S_ISREG(e.mode)
    )

    def measured(group):
        if group not in cache:
            cache[group] = measure(group, units, capacity, layout)
        return cache[group]

    plans: dict[str, Plan | None] = {}
    for strategy, proposals_for_strategy in candidates.items():
        plans[strategy] = None
        for candidate in proposals_for_strategy[:MAX_MEASURED_PLANS]:
            pending = list(candidate)
            result = []
            while pending:
                group = pending.pop(0)
                if protected and not sum(units[i].size for i in group):
                    # Empty units must travel with non-empty data under PAR2.
                    result = []
                    break
                if measured(group).required <= capacity:
                    result.append(group)
                elif len(group) > 1:
                    # Refine an optimistic search weight without splitting any unit.
                    pending[0:0] = [group[:-1], (group[-1],)]
                else:
                    raise ValueError(
                        f"Unit does not fit including filesystem/checksums/recovery: "
                        f"{units[group[0]].path}. "
                        "Choose finer --group-by grouping, larger media, or a smaller -r; "
                        "use -m dar for a sliced archive across multiple discs."
                    )
            plan: Plan = tuple(sorted(result, key=lambda g: g[0]))
            if not plan:
                continue
            if limit is not None and not all(
                limit.allows(measured(g).free(capacity), measured(g).budget(capacity)) for g in plan
            ):
                continue
            plans[strategy] = plan
            break
    return plans, cache


def cmd_create_raw(args):
    try:
        _create_raw(args)
    except ValueError as exc:
        log.error(str(exc))
        sys.exit(1)


def _attachments(paths, source: Path, others: tuple[Path, ...], name: str):
    """Validate --attach paths; return each resolved path with its disc-relative entries."""
    result: list[tuple[Path, list[RawEntry]]] = []
    seen = set()
    for raw in paths:
        path = Path(raw).absolute()
        if path.is_symlink():
            raise ValueError(f"--attach does not support symlinks: {raw}")
        if not path.exists():
            raise ValueError(f"Attachment does not exist: {raw}")
        path = path.resolve()
        if not path.name:
            raise ValueError(f"Attachment needs a file or folder name: {raw}")
        if path.is_relative_to(source) or source.is_relative_to(path):
            raise ValueError(f"Attachment must be outside the source: {path}")
        for other in others:
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise ValueError(f"Attachment and output/workdir must not overlap: {path}")
        for previous, _ in result:
            if path.is_relative_to(previous) or previous.is_relative_to(path):
                raise ValueError(f"Attachments must not overlap: {previous} / {path}")
        key = path.name.casefold()
        if is_reserved_root_name(path.name) or key == name.casefold():
            raise ValueError(f"Attachment name is reserved at the disc root: {path.name}")
        if key in seen:
            raise ValueError(f"Attachment names must be unique (ignoring case): {path.name}")
        seen.add(key)
        result.append((path, scan_attachment(path)))
    return result


def _weight(entry: RawEntry) -> int:
    """Fast search weight of one entry, as for units (see plan.make_units)."""
    if stat.S_ISREG(entry.mode):
        return ((entry.size + 2047) // 2048) * 2048 + 512 + 2 * len(entry.path.encode())
    return 4096


def _layout_lines(name: str, attachments, recovery_enabled: bool) -> list[str]:
    top = [f"{name}/"]
    top += [f"{path.name}/" if path.is_dir() else path.name for path, _ in attachments]
    top += [RAW_README, f"{RAW_METADATA_DIR}/"]
    metadata = [RAW_CHECKSUMS]
    if recovery_enabled:
        metadata += [RAW_PAR2_INDEX, "recovery.vol*.par2"]
    metadata.append(RAW_V3_MARKER)
    lines = []
    for position, entry in enumerate(top):
        last = position == len(top) - 1
        lines.append(f"{'└──' if last else '├──'} {entry}")
    for position, entry in enumerate(metadata):
        last = position == len(metadata) - 1
        lines.append(f"    {'└──' if last else '├──'} {entry}")
    return lines


def _recovery_text(size: int, data: int) -> str:
    """Recovery bytes and their share of the disc's data, as written to the README."""
    return f"{human_bytes(size)} ({100 * size / data:.2f}%)" if data else human_bytes(size)


def _readme_notes(name: str, recovery: bool) -> str:
    """Point readers to the payload folder and the hidden metadata folder."""
    contents = "checksums and recovery files" if recovery else "checksums"
    return textwrap.fill(
        f"{name}/ holds the archived files. The hidden folder {RAW_METADATA_DIR}/ next to "
        f"this README holds the {contents}. File managers may hide it because its name "
        "starts with a dot.",
        76,
        initial_indent=f"{'FILES:':14}",
        subsequent_indent=" " * 14,
        # Never split folder names such as .bd-archive/ across lines.
        break_on_hyphens=False,
        break_long_words=False,
    )


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
    group_by = getattr(args, "group_by", 1)
    order_by = getattr(args, "order_by", "name")
    max_free = getattr(args, "max_free", None)
    # -r is the recovery every plan reserves; --fill-recovery may add more.
    minimum = None if args.redundancy.disabled else args.redundancy
    fill = getattr(args, "fill_recovery", False)
    recovery_enabled = minimum is not None or fill
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
    attachments = _attachments(
        getattr(args, "attach", None) or [], source, (output, work), args.name
    )
    check_output_available(output)
    images = output / ("images" if args.iso else "discs")

    by_content = order_by == "content-date"
    deps = ["mkisofs"]
    if recovery_enabled:
        deps.append("par2")
    if args.bytes is None:
        deps.append("dvd+rw-mediainfo")
    check_deps(*deps, *(["exiftool"] if by_content else []))
    software = software_info(deps)
    capacity = args.bytes
    if capacity is None:
        device = resolve_device(args.device)
        capacity = detect_disc_capacity(device)
        if capacity is None or capacity <= 0:
            raise ValueError(f"No writable disc detected at {device}; insert one or use --bytes")

    log.step("Scanning source and planning one raw data disc")
    inventory = scan_raw_source(source)
    if by_content:
        log.info("Reading content dates with ExifTool...")
        dates = scan_dates(source, inventory)
    else:
        dates = {
            e.path: ContentDate(e.mtime_ns, "mtime") for e in inventory if stat.S_ISREG(e.mode)
        }
    units = make_units(inventory, group_by, dates, order_by)
    if not units:
        raise ValueError(f"No files or directories to archive in {source}")
    attached = [e for _, entries in attachments for e in entries]
    hardlinks = HardlinkTracker()
    for entry in inventory:
        if stat.S_ISREG(entry.mode):
            hardlinks.add(entry.path, entry.device, entry.inode)
    for path, entries in attachments:
        for entry in entries:
            if stat.S_ISREG(entry.mode):
                hardlinks.add(str(path.parent / entry.path), entry.device, entry.inode)
    hardlinks.check()
    attached_size = sum(e.size for e in attached if stat.S_ISREG(e.mode))
    total = sum(u.size for u in units)
    if minimum is not None and not total + attached_size:
        raise ValueError("PAR2 needs at least one non-empty file; use -r none")

    publisher = f"bd-archive v{__version__}"
    label = args.name
    layout = DiscLayout(
        args.name, publisher, tuple(attached), minimum, readme_allowance(args.description)
    )
    fallback = sum(date.source == "mtime" for date in dates.values())
    section("Source")
    field("Path", source)
    field("Units", f"{len(units)} ({'depth:inf' if group_by is None else f'depth:{group_by}'})")
    field("Size", human_bytes(total))
    field("Order", order_by)
    if by_content:
        field("Files with metadata", len(dates) - fallback)
        field("Files with mtime", fallback)
    if attachments:
        field("Attachments", f"{len(attachments)} ({human_bytes(attached_size)})")
    field("Disc capacity", human_bytes(capacity))
    if not recovery_enabled:
        field("Redundancy", "none (SHA-512 checksums only)")
    elif args.redundancy.percent:
        field("Redundancy", f"{args.redundancy.label} of the data (-r)")
    else:
        field("Redundancy", f"{args.redundancy.label} per disc (-r)")
    field("Fill free space", "enabled (--fill-recovery)" if fill else "disabled")
    field("Max free", max_free.label if max_free else "unlimited")

    # Fast search budget for unit data; every offered plan gets a precise sparse-tree check.
    room = capacity - (
        sum(_weight(e) for e in attached) + layout.readme_bytes + DISC_END_MARGIN + MiB // 2
    )
    if minimum is None:
        budget = room
    elif minimum.percent:
        budget = int(room * 100 / (100 + minimum.value))
    else:
        budget = room - int(minimum.value)
    if budget <= 0:
        raise ValueError("Disc capacity is too small for filesystem, metadata, attachments and -r")
    for index, unit in enumerate(units):
        # Do not reject a nearly full unit solely because search weights are
        # conservative. Such units can still occupy a measured disc alone.
        if unit.weight > budget and measure((index,), units, capacity, layout).required <= capacity:
            units[index] = replace(unit, weight=budget)
    max_unused = max_free.bytes_of(budget) if max_free else None
    candidates = proposals(units, budget, max_unused)
    log.blank()
    log.info("Checking filesystem, checksum and recovery space for candidate plans...")
    plans, sizes = checked_proposals(units, candidates, capacity, max_free, layout)
    recoveries = {}
    for number, strategy in enumerate(STRATEGIES, 1):
        section(f"Plan {number} ({strategy})")
        field("Packing", PACKING_TEXT[strategy])
        plan = plans[strategy]
        if plan is None:
            field("Result", "no plan within the free-space limit")
            continue
        # The plan's first disc is the one this run creates.
        group = plan[0]
        size = sizes[group]
        if group not in recoveries:
            recoveries[group] = plan_recovery(group, units, capacity, layout, fill)
        recovery, estimate = recoveries[group]
        deferred = len(units) - sum(len(g) for g in plan)
        field("Units", len(group))
        field("Data", human_bytes(size.payload))
        if order_by == "name":
            field("Name range", f"{units[group[0]].path} to {units[group[-1]].path}")
        else:
            starts = [units[i].date_start for i in group]
            ends = [units[i].date_end for i in group]
            field("Date range", f"{date_text(min(starts))} to {date_text(max(ends))} (UTC)")
        if by_content:
            field("Files with metadata", sum(units[i].metadata for i in group))
        field("Free (unused)", human_bytes(size.free(capacity)))
        if recovery_enabled:
            field("Recovery (-r)", _recovery_text(recovery.minimum_bytes, recovery.data))
        if fill:
            field(
                "Recovery (fill)",
                _recovery_text(recovery.fill_bytes, recovery.data)
                if recovery.fill_bytes
                else f"skipped (below {FILL_MIN_PERMILLE / 10}% of the disc's data)",
            )
        field("Estimated disc", human_bytes(estimate))
        field("Remaining", human_bytes(total - size.payload))
        field(
            "Further discs",
            f"{len(plan) - 1}" + (f" (+ {deferred} deferred units)" if deferred else ""),
        )
        field("Order consistency", f"{order_consistency(disorder(plan, units)[0])}%")
    available = [number for number, strategy in enumerate(STRATEGIES, 1) if plans[strategy]]
    if not available:
        if max_free is None:
            raise ValueError(
                "No supported plan found; a disc containing only empty files/directories "
                "requires -r none."
            )
        log.blank()
        log.info("No plan meets the free-space limit; nothing was created.")
        return
    distinct = {plans[STRATEGIES[number - 1]][0] for number in available}
    if args.yes:
        # Without a prompt, prefer the most ordered plan.
        choice = available[-1]
        log.step(f"Automatically selected plan {choice} ({STRATEGIES[choice - 1]}): -y")
    elif len(distinct) > 1:
        choice = available[0]
        log.blank()
        while True:
            answer = styled_input(
                f"Select plan [1-{len(STRATEGIES)}] (Enter = {choice}, q = cancel): "
            ).strip()
            if answer.lower() == "q":
                log.info("Cancelled; nothing was created.")
                return
            if not answer:
                break
            if answer.isdigit() and int(answer) in available:
                choice = int(answer)
                break
            log.warn("Choose a listed plan number that has a plan, or q.")
        log.step(f"Selected plan {choice} ({STRATEGIES[choice - 1]})")
    elif len(available) > 1:
        choice = available[0]
        log.step("Automatically selected plan: all available plans are identical")
    else:
        choice = available[0]
        log.step("Automatically selected plan: no other plan available")
    plan = plans[STRATEGIES[choice - 1]]
    group = plan[0]
    selected = [units[i] for i in group]
    section("Disc layout")
    field("Name", args.name)
    for line in _layout_lines(args.name, attachments, recovery_enabled):
        log.info(f"  {line}")
    log.blank()
    log.info("Keep the selected files unchanged until creation finishes.")
    if not args.yes and not prompt_yn("Create this disc?", default_yes=False):
        log.info("Cancelled; nothing was created.")
        return

    # Recheck after the interactive prompt, before the first file is created.
    for unit in selected:
        check_unit(source, unit)
    for path, entries in attachments:
        if scan_attachment(path) != entries:
            raise ValueError(f"Attachment changed: {path}; run create again")
    by_path = {e.path: e for e in inventory}
    ancestors = sorted(
        {
            "/".join(unit.path.split("/")[:depth])
            for unit in selected
            for depth in range(1, unit.path.count("/") + 1)
        }
    )
    items = [
        StageItem(source / path, f"{args.name}/{path}", by_path[path], link=False)
        for path in ancestors
    ]
    items += [
        StageItem(source / e.path, f"{args.name}/{e.path}", e, link=True)
        for unit in selected
        for e in unit.entries
    ]
    items += [
        StageItem(path.parent / e.path, e.path, e, link=False)
        for path, entries in attachments
        for e in entries
    ]
    disc_inventory = layout.disc_entries(group, units)

    output_created = not output.exists()
    images_created = not images.exists()
    staging = None
    published = False
    try:
        if args.iso:
            work.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix="raw-", dir=work))
            root = staging / "disc"
        else:
            images.mkdir(parents=True, exist_ok=True)
            root = images / "disc_0001"
        check_space(root, items)
        root.mkdir()
        if staging is None:
            staging = root
        (root / args.name).mkdir()
        log.step("Bringing selected files into the disc folder")
        digests, transferred = stage(items, root)
        log.info(
            "File transfer: "
            + "; ".join(f"{kind} {human_bytes(size)}" for kind, size in transferred.items())
        )
        payload_signature = tree_signature(root)

        def check_payload(activity: str) -> None:
            current = {
                path: value
                for path, value in tree_signature(root).items()
                if path != RAW_README and path.split("/")[0] != RAW_METADATA_DIR
            }
            if current != payload_signature:
                raise ValueError(
                    f"Disc files changed while {activity}; keep the selected files unchanged "
                    "and run create again"
                )

        entries = [("", root)]
        payload_iso_size = mkisofs.estimate_size(entries, label, publisher, rock_ridge=True)
        metadata = root / RAW_METADATA_DIR
        metadata.mkdir()
        (metadata / RAW_V3_MARKER).write_text(RAW_V3_MARKER_TEXT, encoding="utf-8")
        log.step("Creating SHA-512 checksums for all disc files")
        write_raw_checksums(root, disc_inventory, metadata / RAW_CHECKSUMS, digests=digests)
        check_payload("hashing")
        recovery = None
        choice = choose_recovery(
            metadata, entries, disc_inventory, capacity, payload_iso_size, layout, fill
        )
        if fill and choice.sizing is not None and not choice.fill_bytes:
            log.info(fill_skipped_message(choice.extra))
        blocks, sizing, data = choice.blocks, choice.sizing, choice.data
        if blocks:
            recovery_bytes = blocks * sizing.block_size
            roots = sorted(
                {e.path.split("/")[0] for e in disc_inventory if stat.S_ISREG(e.mode) and e.size}
            )
            log.step("Creating PAR2 recovery data")
            index = metadata / RAW_PAR2_INDEX
            par2.create_disc(
                root,
                index,
                [root / name for name in roots],
                block_size=sizing.block_size,
                recovery_blocks=blocks,
            )
            if not index.is_file() or not list(metadata.glob("recovery.vol*.par2")):
                raise ValueError("PAR2 did not produce both an index and recovery data")
            check_payload("creating PAR2")
            recovery = {
                "Format": "PAR2",
                "Coverage": "Non-empty file contents",
                "Redundancy": f"{100 * recovery_bytes / data:.2f}% ({human_bytes(recovery_bytes)})",
                "Index": f"{RAW_METADATA_DIR}/{RAW_PAR2_INDEX}",
                "Volumes": f"{RAW_METADATA_DIR}/recovery.vol*.par2",
            }

        # Publish the README only after the recovery parameters are final.
        notes = _readme_notes(args.name, recovery is not None)
        write_readme(
            root / RAW_README,
            name=args.name,
            description=args.description,
            archive_format="UDF / ISO 9660 (Rock Ridge)",
            details={},
            checksum_files=[f"{RAW_METADATA_DIR}/{RAW_CHECKSUMS}"],
            recovery=recovery,
            software=software,
            notes=notes,
        )
        iso_size = mkisofs.estimate_size(entries, label, publisher, rock_ridge=True)
        if disc_write_bytes(iso_size) > capacity:
            raise ValueError(
                f"Required write size ({disc_write_bytes(iso_size)} bytes including "
                f"32-KiB write padding) exceeds disc capacity ({capacity} bytes); "
                "use a larger disc or reduce -r."
            )
        if not args.iso:
            folder = DiscFolder(root, label, publisher, True, 0, tree_signature(root))
            folder.image_bytes = folder.measure()
            if disc_write_bytes(folder.image_bytes) > capacity:
                raise ValueError("Disc folder exceeds disc capacity including 32-KiB write padding")
            check_payload("measuring")
            iso_size = folder.image_bytes
            save_disc_set(images, [folder])
        else:
            images.mkdir(parents=True, exist_ok=True)
            pending = staging / "disc.iso"
            log.step("Building directly readable disc image")
            mkisofs.build(pending, entries, label, publisher, rock_ridge=True)
            if disc_write_bytes(pending.stat().st_size) > capacity:
                raise ValueError(
                    "Built ISO exceeds disc capacity including 32-KiB write padding; "
                    "no burnable image was saved."
                )
            check_payload("building the ISO")
            iso_size = pending.stat().st_size
            pending.rename(images / "disc_0001.iso")
        published = True
    finally:
        # A failed run leaves the source unchanged and removes everything it created.
        # Hardlinked disc files are only additional names of the source files.
        if staging is not None and (args.iso or not published):
            shutil.rmtree(staging, ignore_errors=True)
        if not published and images_created:
            with contextlib.suppress(OSError):
                images.rmdir()
        if args.workdir is None:
            with contextlib.suppress(OSError):
                work.rmdir()
        if not published and output_created:
            with contextlib.suppress(OSError):
                output.rmdir()

    disc_path = images / ("disc_0001.iso" if args.iso else "disc_0001")
    log.ok(f"Raw disc ready: {disc_path} ({human_bytes(iso_size)})")
    log.blank()
    if args.yes:
        log.info("Source unchanged: -y never removes archived units.")
    elif prompt_yn(
        f"Remove the {len(selected)} archived units from the source?", default_yes=False
    ):
        remove_units(source, selected, ancestors)
        log.ok(f"Removed {len(selected)} archived units from the source")
    else:
        log.info("Source unchanged: the next run plans these units again.")
    log.info(f"Next step: bd-archive burn -i {shlex.quote(str(output))}")
