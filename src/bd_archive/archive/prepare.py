"""Stateless grouping and bounded, order-aware raw disc planning."""

import bisect
import math
import os
import re
import stat
from dataclasses import dataclass, replace
from decimal import Decimal

from bd_archive.archive.content_dates import ContentDate, weighted_median
from bd_archive.archive.raw import MAX_PAR2_BLOCKS, RawEntry


@dataclass(frozen=True)
class FreeLimit:
    value: Decimal
    percent: bool
    text: str

    def allows(self, free: int, budget: int) -> bool:
        allowed = self.value * budget / 100 if self.percent else self.value
        return free <= allowed


def free_limit(value: str) -> FreeLimit:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([MG]?)", value, re.IGNORECASE)
    if not match:
        raise ValueError("expected a percentage (5), decimal MB (500M) or decimal GB (2G)")
    amount = Decimal(match[1])
    suffix = match[2].upper()
    if not suffix and amount > 100:
        raise ValueError("expected a percentage between 0 and 100")
    return FreeLimit(amount * {"": 1, "M": 10**6, "G": 10**9}[suffix], not suffix, value)


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


def pack(units: list[Unit], count: int, budget: int, recovery: bool, strategy: str) -> Plan:
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
                and (not recovery or files[-1] + unit.nonempty <= MAX_PAR2_BLOCKS)
            ):
                target = len(groups) - 1
        else:
            start = bisect.bisect_left(available, (unit.weight, -1))
            # Bound the search when many bins have exhausted their PAR2 file slots.
            for _, j in available[start : start + 64]:
                if not recovery or files[j] + unit.nonempty <= MAX_PAR2_BLOCKS:
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


def improve(plan: Plan, units: list[Unit], budget: int, recovery: bool) -> Plan:
    """Try bounded boundary moves/swaps across the complete set of adjacent discs."""
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
                    if max(left_weight - aw + bw, right_weight - bw + aw) > budget:
                        continue
                    if (
                        recovery
                        and max(left_files - af + bf, right_files - bf + af) > MAX_PAR2_BLOCKS
                    ):
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


def proposals(units: list[Unit], budget: int, recovery: bool, defer: bool) -> dict[str, list[Plan]]:
    """Return one candidate plan per strategy and cutoff, longest prefix first.

    ``efficient`` packs by size, ``balanced`` fills in order and backfills gaps,
    ``ordered`` never reorders. Only prefixes in fill order may be included:
    all cutoffs for small backlogs, sampled cutoffs for large ones. The search
    is deterministic with bounded local improvements; it does not claim global
    optimality.
    """
    for unit in units:
        if unit.weight > budget or (recovery and unit.nonempty > MAX_PAR2_BLOCKS):
            raise ValueError(
                f"Unit cannot fit on one disc with these settings: {unit.path}. "
                "Choose finer --group-by grouping, larger media, or different redundancy."
            )
    n = len(units)
    cuts = {n}
    if defer:
        if n <= 64:
            cuts.update(range(1, n))
        else:
            cuts.update(range(max(1, n - 16), n))
            cuts.update(max(1, math.ceil(n * k / 48)) for k in range(1, 48))
            sequential = pack(units, n, budget, recovery, "sequential")
            boundaries = [g[-1] + 1 for g in sequential]
            step = max(1, len(boundaries) // 48)
            cuts.update(boundaries[::step])
    candidates: dict[str, list[Plan]] = {strategy: [] for strategy in STRATEGIES}
    for count in sorted(cuts, reverse=True):
        packed = {}
        for strategy, packing in _PACKING.items():
            plan = pack(units, count, budget, recovery, packing)
            # Avoid repeatedly optimizing a large backlog for every cutoff.
            if strategy != "ordered" and (count == n or n <= 64):
                plan = improve(plan, units, budget, recovery)
            packed[strategy] = plan
        # Each row may fall back to a more ordered packing that needs no extra disc,
        # so efficient never trails balanced and balanced never trails ordered.
        for strategy, pool in (
            ("efficient", STRATEGIES),
            ("balanced", ("balanced", "ordered")),
            ("ordered", ("ordered",)),
        ):
            candidates[strategy].append(
                min((packed[s] for s in pool), key=lambda p: (len(p), disorder(p, units)[0], p))
            )
    return candidates
