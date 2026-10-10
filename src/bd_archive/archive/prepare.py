"""Stateless grouping and bounded, order-aware raw disc planning."""

import bisect
import math
import os
import re
import stat
from dataclasses import dataclass, replace

from bd_archive.archive.content_dates import ContentDate, weighted_median
from bd_archive.archive.raw import MAX_PAR2_BLOCKS, RawEntry


def grouping(value: str) -> int | None:
    """Parse --group-by: depth:N keeps folders N levels deep whole; depth:inf separates files."""
    match = re.fullmatch(r"depth:(inf|[1-9]\d*)", value)
    if not match:
        raise ValueError("expected depth:N (N >= 1) or depth:inf")
    return None if match[1] == "inf" else int(match[1])


ORDERS = ("name", "mtime", "content-date")
STRATEGIES = ("efficient", "balanced", "ordered")
_PACKING = {"efficient": "size", "balanced": "order", "ordered": "sequential"}


@dataclass(frozen=True)
class Unit:
    path: str
    entries: tuple[RawEntry, ...]
    size: int
    date: int
    nonempty: int
    weight: int
    date_start: int
    date_end: int
    order: int  # fill-order key: the rank by path, or the date in nanoseconds
    metadata: int  # files dated from content metadata


def make_units(
    inventory: list[RawEntry],
    depth: int | None,
    dates: dict[str, ContentDate],
    order_by: str = "name",
) -> list[Unit]:
    """Use disjoint roots; keep empty leaf directories even in file grouping.

    Units are returned in fill order: by relative path for ``name``, otherwise
    by their size-weighted median date with the path as tie-breaker.
    """
    groups: dict[str, list[RawEntry]] = {}
    parents = {entry.path.rpartition("/")[0] for entry in inventory}
    for entry in inventory:
        parts = entry.path.split("/")
        if depth is not None and len(parts) >= depth:
            root = "/".join(parts[:depth])
        elif stat.S_ISREG(entry.mode) or entry.path not in parents:
            root = entry.path
        else:
            continue
        groups.setdefault(root, []).append(entry)
    result = []
    for path, entries in groups.items():
        files = [e for e in entries if stat.S_ISREG(e.mode)]
        if files:
            date = weighted_median([(dates[e.path].timestamp, e.size) for e in files])
            media = [dates[e.path].timestamp for e in files if dates[e.path].media]
            span = media or [dates[e.path].timestamp for e in files]
        else:
            date = max(e.mtime_ns for e in entries)
            span = [date]
        # Fast search weights include sector rounding and a path/metadata allowance.
        # Final proposals are measured with mkisofs before moving anything.
        weight = sum(
            ((e.size + 2047) // 2048) * 2048 + 512 + 2 * len(os.fsencode(e.path))
            if stat.S_ISREG(e.mode)
            else 4096
            for e in entries
        ) + 4096 * path.count("/")
        result.append(
            Unit(
                path,
                tuple(entries),
                sum(e.size for e in files),
                date,
                sum(e.size > 0 for e in files),
                max(1, weight),
                min(span),
                max(span),
                date,
                sum(dates[e.path].source != "mtime" for e in files),
            )
        )
    if order_by == "name":
        ranked = sorted(result, key=lambda u: u.path)
        return [replace(unit, order=rank) for rank, unit in enumerate(ranked)]
    return sorted(result, key=lambda u: (u.date, u.path))


Plan = tuple[tuple[int, ...], ...]


def ordered(groups) -> Plan:
    return tuple(sorted((tuple(sorted(g)) for g in groups if g), key=lambda g: g[0]))


def disorder(plan: Plan, units: list[Unit]) -> tuple[float, int, int]:
    """Blend affected-unit share, average gap and the worst outlier in fill order.

    Gaps use the units' order key: nanoseconds for date orders, positions for
    name order. Returns the score, the affected unit count and the worst gap.
    """
    if not plan:
        return 0.0, 0, 0
    keys = [units[i].order for g in plan for i in g]
    span = max(max(keys) - min(keys), 1)
    latest = min(keys)
    gaps = []
    for group in plan:
        gaps.extend(max(0, latest - units[i].order) for i in group)
        latest = max(latest, *(units[i].order for i in group))
    affected = sum(gap > 0 for gap in gaps)
    worst = max(gaps, default=0)
    score = (
        0.5 * affected / len(keys)
        + 0.25 * sum(gap / span for gap in gaps) / len(keys)
        + 0.25 * (worst / span) ** 2
    )
    return score, affected, worst


def pack(units: list[Unit], count: int, budget: int, strategy: str) -> Plan:
    groups: list[list[int]] = []
    remaining: list[int] = []
    files: list[int] = []
    available: list[tuple[int, int]] = []
    order = list(range(count))
    if strategy == "size":
        order.sort(key=lambda i: (-units[i].weight, i))
    for i in order:
        unit = units[i]
        target = None
        if strategy == "sequential":
            if (
                groups
                and remaining[-1] >= unit.weight
                and files[-1] + unit.nonempty <= MAX_PAR2_BLOCKS
            ):
                target = len(groups) - 1
        else:
            start = bisect.bisect_left(available, (unit.weight, -1))
            # Bound the search when many bins have exhausted their PAR2 file slots.
            for _, j in available[start : start + 64]:
                if files[j] + unit.nonempty <= MAX_PAR2_BLOCKS:
                    target = j
                    break
        if target is None:
            target = len(groups)
            groups.append([])
            remaining.append(budget)
            files.append(0)
        elif strategy != "sequential":
            available.remove((remaining[target], target))
        groups[target].append(i)
        remaining[target] -= unit.weight
        files[target] += unit.nonempty
        if strategy != "sequential":
            bisect.insort(available, (remaining[target], target))
    return ordered(groups)


FILL_SEARCH_NODES = 20_000  # per cutoff
FILL_MAX_UNITS = 200  # bounds the recursion; small units fill well with heuristic packings


class _Exhausted(Exception):
    pass


def fill(units: list[Unit], count: int, budget: int, floor: int) -> Plan | None:
    """Search the most ordered plan for the first ``count`` units within ``floor..budget``.

    Every disc must load between ``floor`` and ``budget``. The search runs for at
    most ``FILL_MAX_UNITS`` units and ``FILL_SEARCH_NODES`` steps; within that it
    is exact, otherwise it returns the best plan found. It tries the fewest discs
    first and builds each disc around the earliest unplaced unit, adding later
    units in fill order. The disorder of placed discs never decreases as discs are
    added, so branches already scoring no better than the best plan are skipped.
    Discs with only empty units are avoided when any unit has data, as those
    cannot carry PAR2.
    """
    if count > FILL_MAX_UNITS:
        return None
    weights = [units[i].weight for i in range(count)]
    keys = [units[i].order for i in range(count)]
    total = sum(weights)
    span = max(max(keys) - min(keys), 1)
    protected = any(units[i].size for i in range(count))
    nodes = 0
    skipped = 0
    best_score = math.inf
    best: list[tuple[int, ...]] | None = None

    def score(affected: int, gaps: int, worst: int) -> float:
        # The terms of disorder(), restricted to the units placed so far.
        return 0.5 * affected / count + 0.25 * gaps / span / count + 0.25 * (worst / span) ** 2

    def discs_for(rest, left, groups, latest, affected, gaps, worst) -> bool:
        """Place ``rest`` on ``left`` discs; return whether any placement exists."""
        nonlocal best, best_score
        if not rest:
            current = score(affected, gaps, worst)
            if current < best_score:
                best, best_score = list(groups), current
            return True
        if (rest, left) in failed:
            return False
        remaining = sum(weights[i] for i in rest)
        first, others = rest[0], rest[1:]
        tail = [0] * (len(others) + 1)
        for pos in range(len(others) - 1, -1, -1):
            tail[pos] = tail[pos + 1] + weights[others[pos]]
        chosen = [first]
        found = False
        before = skipped

        def choose(pos, load, files, data, affected, gaps, worst, after_latest):
            nonlocal nodes, skipped, found
            nodes += 1
            if nodes > FILL_SEARCH_NODES:
                raise _Exhausted
            if score(affected, gaps, worst) >= best_score:
                skipped += 1
                return
            after = remaining - load
            if (
                load >= floor
                and (data or not protected)
                and (left - 1) * floor <= after <= (left - 1) * budget
            ):
                taken = set(chosen)
                groups.append(tuple(chosen))
                if discs_for(
                    tuple(i for i in others if i not in taken),
                    left - 1,
                    groups,
                    after_latest,
                    affected,
                    gaps,
                    worst,
                ):
                    found = True
                groups.pop()
            for at in range(pos, len(others)):
                if load + tail[at] < floor:
                    break
                i = others[at]
                if load + weights[i] > budget or files + units[i].nonempty > MAX_PAR2_BLOCKS:
                    continue
                gap = max(0, latest - keys[i])
                chosen.append(i)
                choose(
                    at + 1,
                    load + weights[i],
                    files + units[i].nonempty,
                    data or units[i].size > 0,
                    affected + (gap > 0),
                    gaps + gap,
                    max(worst, gap),
                    max(after_latest, keys[i]),
                )
                chosen.pop()

        gap = max(0, latest - keys[first])
        choose(
            0,
            weights[first],
            units[first].nonempty,
            units[first].size > 0,
            affected + (gap > 0),
            gaps + gap,
            max(worst, gap),
            max(latest, keys[first]),
        )
        # Without skipped branches, a failed search proves that no placement exists.
        if not found and skipped == before:
            failed.add((rest, left))
        return found

    discs = -(-total // budget)
    # Every further disc only adds unused space.
    while discs * floor <= total:
        failed: set[tuple[tuple[int, ...], int]] = set()
        try:
            discs_for(tuple(range(count)), discs, [], min(keys), 0, 0, 0)
        except _Exhausted:
            return None if best is None else ordered(best)
        if best is not None:
            return ordered(best)
        discs += 1
    return None


def improve(plan: Plan, units: list[Unit], budget: int, floor: int = 0) -> Plan:
    """Try bounded boundary moves/swaps across the complete set of adjacent discs.

    A move never lowers the smaller load of the two discs below ``floor``.
    """
    best = plan
    best_score = disorder(best, units)[0]
    if not best_score:
        return best
    attempts = 0
    for _ in range(2):
        changed = False
        for boundary in range(len(best) - 1):
            left, right = best[boundary : boundary + 2]
            left_weight = sum(units[i].weight for i in left)
            right_weight = sum(units[i].weight for i in right)
            left_files = sum(units[i].nonempty for i in left)
            right_files = sum(units[i].nonempty for i in right)
            for a in (None, *left[-8:]):
                for b in (None, *right[:8]):
                    attempts += 1
                    if attempts > 500:
                        return best
                    if a is None and b is None:
                        continue
                    aw, af = (units[a].weight, units[a].nonempty) if a is not None else (0, 0)
                    bw, bf = (units[b].weight, units[b].nonempty) if b is not None else (0, 0)
                    loads = (left_weight - aw + bw, right_weight - bw + aw)
                    if max(loads) > budget:
                        continue
                    if min(loads) < min(floor, left_weight, right_weight):
                        continue
                    if max(left_files - af + bf, right_files - bf + af) > MAX_PAR2_BLOCKS:
                        continue
                    new_left = [i for i in left if i != a] + ([] if b is None else [b])
                    new_right = [i for i in right if i != b] + ([] if a is None else [a])
                    trial = ordered((*best[:boundary], new_left, new_right, *best[boundary + 2 :]))
                    score = disorder(trial, units)[0]
                    if (len(trial), score) < (len(best), best_score):
                        best, best_score = trial, score
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
        if not changed or not best_score:
            break
    return best


def proposals(
    units: list[Unit], budget: int, max_unused: int | None = None
) -> dict[str, list[Plan]]:
    """Return one candidate plan per strategy and cutoff, longest prefix first.

    ``efficient`` packs by size, ``balanced`` fills in order and backfills gaps,
    ``ordered`` never reorders. Without ``max_unused`` every unit is included.
    With it, only prefixes in fill order may be included: all cutoffs for small
    backlogs, sampled cutoffs for large ones. Plans whose search weights leave at
    most ``max_unused`` free on every disc are preferred, and ``efficient`` also
    receives a bounded exact search for such a plan. The search is deterministic
    with bounded local improvements; it does not claim global optimality.
    """
    defer = max_unused is not None
    floor = budget - max_unused if defer else 0
    for unit in units:
        if unit.weight > budget or unit.nonempty > MAX_PAR2_BLOCKS:
            raise ValueError(
                f"Unit cannot fit on one disc with these settings: {unit.path}. "
                "Choose finer --group-by grouping, larger media, or a smaller --reserve."
            )
    n = len(units)
    cuts = {n}
    if defer:
        if n <= 64:
            cuts.update(range(1, n))
        else:
            cuts.update(range(max(1, n - 16), n))
            cuts.update(max(1, math.ceil(n * k / 48)) for k in range(1, 48))
            sequential = pack(units, n, budget, "sequential")
            boundaries = [g[-1] + 1 for g in sequential]
            step = max(1, len(boundaries) // 48)
            cuts.update(boundaries[::step])
    candidates: dict[str, list[Plan]] = {strategy: [] for strategy in STRATEGIES}
    for count in sorted(cuts, reverse=True):
        packed = {}
        for strategy, packing in _PACKING.items():
            plan = pack(units, count, budget, packing)
            # Avoid repeatedly optimizing a large backlog for every cutoff.
            if strategy != "ordered" and (count == n or n <= 64):
                plan = improve(plan, units, budget, floor)
            packed[strategy] = plan
        pools = {
            "efficient": STRATEGIES,
            "balanced": ("balanced", "ordered"),
            "ordered": ("ordered",),
        }
        if defer:
            # Heuristic packings ignore the limit; search for a plan that meets it.
            plan = fill(units, count, budget, floor)
            if plan is not None:
                packed["fill"] = improve(plan, units, budget, floor)
                pools["efficient"] = (*STRATEGIES, "fill")

        def rank(plan: Plan):
            unmet = defer and any(sum(units[i].weight for i in group) < floor for group in plan)
            return unmet, len(plan), disorder(plan, units)[0], plan

        # Each row may fall back to a more ordered packing that needs no extra disc,
        # so efficient never trails balanced and balanced never trails ordered.
        for strategy, pool in pools.items():
            candidates[strategy].append(min((packed[s] for s in pool if s in packed), key=rank))
    return candidates
