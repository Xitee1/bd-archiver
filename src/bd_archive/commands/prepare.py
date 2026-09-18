"""Interactively split a source into ordinary raw-disc sources by moving files."""

import shlex
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from bd_archive.archive.content_dates import scan_dates
from bd_archive.archive.prepare import Plan, chronology, make_units, proposals
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


def checked_proposals(units, candidates, capacity, redundancy, limit):
    """Refine search estimates against real filesystem and recovery reservations."""
    cache = {}

    def measured(group):
        if group not in cache:
            cache[group] = measure(group, units, capacity, redundancy)
        return cache[group]

    valid = set()
    best = None
    for candidate in candidates:
        count = sum(map(len, candidate))
        if best is not None:
            best_count = sum(map(len, best))
            best_score = chronology(best, units)[0]
            if count < best_count:
                # Only inspect useful neighboring tradeoffs after finding a main plan.
                if len(candidate) not in (len(best), len(best) - 1):
                    continue
                if len(candidate) == len(best) and chronology(candidate, units)[0] >= best_score:
                    continue
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
        if limit is not None:
            last = measured(plan[-1])
            if not limit.allows(last.free(capacity), last.budget(capacity)):
                continue
        valid.add(plan)
        if best is None or (-count, len(plan), chronology(plan, units)[0], plan) < (
            -sum(map(len, best)),
            len(best),
            chronology(best, units)[0],
            best,
        ):
            best = plan
        if len(valid) >= 96:
            break
    if best is None:
        return [], cache

    def included(plan):
        return sum(units[i].size for group in plan for i in group)

    def score(plan):
        return chronology(plan, units)[0]

    choices = [best]
    remaining = valid - {best}
    # Show actual tradeoffs, never a plan worse in every displayed dimension.
    for pool in (
        [p for p in remaining if included(p) == included(best) and score(p) < score(best)],
        [p for p in remaining if len(p) == len(best) and score(p) < score(best)],
        [p for p in remaining if len(p) == len(best) - 1],
    ):
        if pool:
            choice = min(pool, key=lambda p: (-included(p), len(p), score(p), p))
            if choice not in choices:
                choices.append(choice)
    return choices, cache


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
    check_deps("mkisofs", "exiftool", *([] if capacity is not None else ["dvd+rw-mediainfo"]))
    if capacity is None:
        capacity = detect_disc_capacity(resolve_device(args.device))
        if capacity is None or capacity <= 0:
            raise ValueError("No writable disc detected; insert one or use --bytes")

    log.step("Scanning source and planning raw-disc groups")
    inventory = scan_raw_source(source)
    log.info("Reading content dates with ExifTool...")
    dates = scan_dates(source, inventory)
    fallback = sum(date.source == "mtime" for date in dates.values())
    log.info(
        f"Dates: {len(dates) - fallback} files from content metadata, "
        f"{fallback} using modification time."
    )
    units = make_units(inventory, args.group_by, dates)
    if not units:
        log.info("No files or directories to prepare.")
        return
    total = sum(u.size for u in units)
    if not total and args.redundancy != 0:
        raise ValueError("Only empty files/directories found; use -r none")
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
    candidates = proposals(units, budget, args.redundancy != 0, args.max_last_free is not None)
    log.info(f"Source: {len(units)} units, {human_bytes(total)}")
    log.info(f"Disc capacity: {human_bytes(capacity)}")
    log.info("Checking filesystem, checksum and recovery space for candidate plans...")
    choices, sizes = checked_proposals(
        units, candidates, capacity, args.redundancy, args.max_last_free
    )
    if not choices:
        if args.max_last_free is None:
            raise ValueError(
                "No supported complete plan found; discs containing only empty files/directories "
                "require -r none."
            )
        log.info("No plan meets the last-disc fill limit; all data stays in the source.")
        return
    log.info("Plan  Discs  Included         Deferred         Chronology")
    for number, plan in enumerate(choices, 1):
        included = sum(units[i].size for group in plan for i in group)
        _, affected, worst = chronology(plan, units)
        detail = (
            "no unit-center overlap"
            if not affected
            else (
                f"{affected} units overlap earlier discs, up to {worst / (86400 * 10**9):.1f} days"
            )
        )
        log.info(
            f"{number:4}  {len(plan):5}  {human_bytes(included):15}  "
            f"{human_bytes(total - included):15}  {detail}"
        )
    log.info("Plan 1 includes the largest searched chronological prefix using the fewest discs.")
    log.info("Planning uses a bounded heuristic; a global optimum is not guaranteed.")
    log.info("Chronology uses size-weighted date medians; media ranges may overlap within units.")
    choice = 0
    if len(choices) > 1:
        while True:
            answer = styled_input(
                f"Select plan [1-{len(choices)}] (Enter = 1, q = cancel): "
            ).strip()
            if answer.lower() == "q":
                log.info("Cancelled; no files moved.")
                return
            if not answer:
                break
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                choice = int(answer) - 1
                break
            log.warn("Choose a listed plan number or q.")
    plan = choices[choice]
    chosen = {i for group in plan for i in group}
    for number, group in enumerate(plan, 1):
        starts = [units[i].date_start for i in group]
        ends = [units[i].date_end for i in group]
        size = sizes[group]
        log.info(
            f"Disc {number:04d}: {human_bytes(size.payload)} data, "
            f"{human_bytes(size.free(capacity))} free before automatic recovery; "
            f"content range {date_text(min(starts))} to {date_text(max(ends))} (UTC)"
        )
        for i in group:
            unit = units[i]
            log.info(
                f"  {unit.path} ({human_bytes(unit.size)}): center {date_text(unit.date)}, "
                f"range {date_text(unit.date_start)} to {date_text(unit.date_end)}"
            )
    deferred = [u for i, u in enumerate(units) if i not in chosen]
    if deferred:
        log.info(f"Deferred: {len(deferred)} units, {human_bytes(sum(u.size for u in deferred))}")
        for unit in deferred:
            log.info(f"  {unit.path}")
    check_space(source, output, [units[i] for i in sorted(chosen)])
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
    log.info("Create each disc using the same capacity and redundancy:")
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
        log.info(shlex.join(command))
        log.info(shlex.join(["bd-archive", "burn", "-i", str(target)]))
