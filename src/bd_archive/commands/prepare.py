"""Interactively split a source into ordinary raw-disc sources by moving files."""

import shlex
import stat
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from bd_archive.archive.content_dates import ContentDate, scan_dates
from bd_archive.archive.prepare import STRATEGIES, Plan, disorder, make_units, proposals
from bd_archive.archive.prepare_move import check_space, move_plan
from bd_archive.archive.prepare_sizing import measure
from bd_archive.archive.raw import scan_raw_source
from bd_archive.commands.create import _validate_name
from bd_archive.constants import DISC_END_MARGIN, MiB
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools.mediainfo import detect_disc_capacity
from bd_archive.tools.optical import resolve_device
from bd_archive.ui.logger import log
from bd_archive.ui.prompts import prompt_yn, styled_input


def date_text(value: int) -> str:
    try:
        return datetime.fromtimestamp(value // 10**9, UTC).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "out-of-range date"


PACKING_TEXT = {
    "efficient": "by size",
    "balanced": "in order, backfilling gaps",
    "ordered": "strictly in order",
}


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


MAX_MEASURED_PLANS = 64  # per strategy


def checked_proposals(units, candidates, capacity, redundancy, limit):
    """Measure each strategy's candidates, longest prefix first; keep the first that qualifies.

    A plan qualifies when every disc fits and, with a free-space limit, no disc
    exceeds it. Optimistic search weights are refined without splitting a unit.
    """
    cache = {}

    def measured(group):
        if group not in cache:
            cache[group] = measure(group, units, capacity, redundancy)
        return cache[group]

    plans: dict[str, Plan | None] = {}
    for strategy, proposals_for_strategy in candidates.items():
        plans[strategy] = None
        for candidate in proposals_for_strategy[:MAX_MEASURED_PLANS]:
            pending = list(candidate)
            result = []
            while pending:
                group = pending.pop(0)
                if redundancy != 0 and not sum(units[i].size for i in group):
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
                        "Choose finer grouping or different capacity/redundancy."
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


def cmd_prepare(args):
    try:
        _prepare(args)
    except OSError as exc:
        raise ValueError(
            f"Prepare stopped: {exc}. If moving had started, completed moves remain at the "
            "destination; inspect source and destination before retrying."
        ) from exc


def _prepare(args):
    _validate_name(args.name)
    if args.redundancy is not None and not 0 <= args.redundancy <= 100:
        raise ValueError("--redundancy must be 0-100 or none")
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    if not source.is_dir():
        raise ValueError(f"Source directory does not exist: {source}")
    if source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError("Source and output must be separate, non-overlapping trees")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"Output must be a new or empty directory: {output}")
    capacity = args.bytes
    if capacity is not None and capacity <= 0:
        raise ValueError("--bytes must be positive")
    by_content = args.order_by == "content-date"
    by_name = args.order_by == "name"
    check_deps(
        "mkisofs",
        *(["exiftool"] if by_content else []),
        *([] if capacity is not None else ["dvd+rw-mediainfo"]),
    )
    if capacity is None:
        capacity = detect_disc_capacity(resolve_device(args.device))
        if capacity is None or capacity <= 0:
            raise ValueError("No writable disc detected; insert one or use --bytes")

    log.step("Scanning source and planning raw discs")
    inventory = scan_raw_source(source)
    if by_content:
        log.info("Reading content dates with ExifTool...")
        dates = scan_dates(source, inventory)
    else:
        dates = {
            e.path: ContentDate(e.mtime_ns, "mtime") for e in inventory if stat.S_ISREG(e.mode)
        }
    units = make_units(inventory, args.group_by, dates, args.order_by)
    if not units:
        log.info("No files or directories to prepare.")
        return
    total = sum(u.size for u in units)
    if not total and args.redundancy != 0:
        raise ValueError("Only empty files/directories found; use -r none")
    fallback = sum(date.source == "mtime" for date in dates.values())
    section("Source")
    field("Path", source)
    field(
        "Units",
        f"{len(units)} ({'depth:inf' if args.group_by is None else f'depth:{args.group_by}'})",
    )
    field("Size", human_bytes(total))
    field("Order", args.order_by)
    if by_content:
        field("Files with metadata", len(dates) - fallback)
        field("Files with mtime", fallback)
    field("Disc capacity", human_bytes(capacity))
    if args.redundancy is None:
        field("Redundancy", "automatic")
    elif args.redundancy == 0:
        field("Redundancy", "none")
    else:
        field("Redundancy", f"{args.redundancy}%")
    field("Max free per disc", args.max_free.text if args.max_free else "unlimited")
    # Fast search reserve; every offered plan gets a precise sparse-tree check.
    budget = (capacity - DISC_END_MARGIN - MiB // 2) * 100 // (100 + (args.redundancy or 0))
    if budget <= 0:
        raise ValueError("Disc capacity is too small for filesystem and metadata")
    for index, unit in enumerate(units):
        # Do not reject a nearly full unit solely because search weights are
        # conservative. Such units can still occupy a measured disc alone.
        if (
            unit.weight > budget
            and measure((index,), units, capacity, args.redundancy).required <= capacity
        ):
            units[index] = replace(unit, weight=budget)
    candidates = proposals(units, budget, args.redundancy != 0, args.max_free is not None)
    log.blank()
    log.info("Checking filesystem, checksum and recovery space for candidate plans...")
    plans, sizes = checked_proposals(units, candidates, capacity, args.redundancy, args.max_free)
    for number, strategy in enumerate(STRATEGIES, 1):
        section(f"Plan {number} ({strategy})")
        field("Packing", PACKING_TEXT[strategy])
        plan = plans[strategy]
        if plan is None:
            field("Result", "no plan within the free-space limit")
            continue
        included = sum(units[i].size for group in plan for i in group)
        field("Discs", len(plan))
        field("Included", human_bytes(included))
        field("Deferred", human_bytes(total - included))
        field("Unused", human_bytes(sum(sizes[group].free(capacity) for group in plan)))
        if by_content:
            field("Files with metadata", sum(units[i].metadata for group in plan for i in group))
        field("Order consistency", f"{order_consistency(disorder(plan, units)[0])}%")
    available = [number for number, strategy in enumerate(STRATEGIES, 1) if plans[strategy]]
    if not available:
        if args.max_free is None:
            raise ValueError(
                "No supported complete plan found; discs containing only empty files/directories "
                "require -r none."
            )
        log.blank()
        log.info("No plan meets the free-space limit; all data stays in the source.")
        return
    choice = available[0]
    distinct = {plans[STRATEGIES[number - 1]] for number in available}
    if len(distinct) > 1:
        log.blank()
        while True:
            answer = styled_input(
                f"Select plan [1-{len(STRATEGIES)}] (Enter = {choice}, q = cancel): "
            ).strip()
            if answer.lower() == "q":
                log.info("Cancelled; no files moved.")
                return
            if not answer:
                break
            if answer.isdigit() and int(answer) in available:
                choice = int(answer)
                break
            log.warn("Choose a listed plan number that has a plan, or q.")
        log.step(f"Selected plan {choice} ({STRATEGIES[choice - 1]})")
    elif len(available) > 1:
        log.step("Automatically selected plan: all available plans are identical")
    else:
        log.step("Automatically selected plan: no other plan available")
    plan = plans[STRATEGIES[choice - 1]]
    chosen = {i for group in plan for i in group}
    for number, group in enumerate(plan, 1):
        size = sizes[group]
        section(f"Disc {number:04d}")
        field("Data", human_bytes(size.payload))
        field("Free", f"{human_bytes(size.free(capacity))} before automatic recovery")
        if by_name:
            field("Name range", f"{units[group[0]].path} to {units[group[-1]].path}")
        else:
            starts = [units[i].date_start for i in group]
            ends = [units[i].date_end for i in group]
            field("Date range", f"{date_text(min(starts))} to {date_text(max(ends))} (UTC)")
    deferred = [u for i, u in enumerate(units) if i not in chosen]
    section("Deferred")
    field("Units", len(deferred))
    field("Size", human_bytes(sum(u.size for u in deferred)))
    for unit in deferred:
        log.info(f"  {unit.path}")
    check_space(source, output, [units[i] for i in sorted(chosen)])
    log.blank()
    if not prompt_yn(f"Move the selected files into {len(plan)} disc folders?", default_yes=False):
        log.info("Cancelled; no files moved.")
        return
    log.step("Moving selected files")
    try:
        move_plan(source, output, units, plan)
    except (ValueError, KeyboardInterrupt):
        log.warn("Preparation interrupted; inspect source and destination before retrying.")
        raise
    log.ok(f"Prepared {len(plan)} raw-disc sources in {output}")
    section("Next steps (same capacity and redundancy)")
    for number in range(1, len(plan) + 1):
        disc = f"disc_{number:04d}"
        suffix = f"-{number:04d}"
        target = output.parent / f"{output.name}-created" / disc
        command = [
            "bd-archive",
            "create",
            "-m",
            "raw",
            "-s",
            str(output / disc),
            "-n",
            f"{args.name[: 27 - len(suffix)]}{suffix}",
            "-o",
            str(target),
            "-b",
            str(capacity),
        ]
        if args.redundancy is not None:
            command.extend(["-r", str(args.redundancy)])
        if number > 1:
            log.blank()
        log.info(f"  {shlex.join(command)}")
        log.info(f"  {shlex.join(['bd-archive', 'burn', '-i', str(target)])}")
