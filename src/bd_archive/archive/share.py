"""Percent-or-size values shared by --redundancy, --reserve and --max-free."""

import math
import re
from dataclasses import dataclass
from decimal import Decimal

_UNITS = {"": 1, "M": 10**6, "G": 10**9}


@dataclass(frozen=True)
class Share:
    """A percentage of some budget, or a fixed number of decimal bytes."""

    value: Decimal
    percent: bool
    text: str

    @property
    def disabled(self) -> bool:
        return self.value == 0

    @property
    def label(self) -> str:
        return f"{self.text}%" if self.percent and self.text != "none" else self.text

    def bytes_of(self, budget: int) -> int:
        """Bytes this share means for a budget: a percentage of it, or the fixed size."""
        return int(self.value * budget / 100) if self.percent else int(self.value)

    def allows(self, free: int, budget: int) -> bool:
        return free <= self.bytes_of(budget)


def parse_share(value: str) -> Share:
    """Parse `5` (percent), `500M` (decimal MB) or `2G` (decimal GB)."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([MG]?)", value, re.IGNORECASE)
    if not match:
        raise ValueError("expected a percentage (5), decimal MB (500M) or decimal GB (2G)")
    amount = Decimal(match[1])
    suffix = match[2].upper()
    if not suffix and amount > 100:
        raise ValueError("expected a percentage between 0 and 100")
    return Share(amount * _UNITS[suffix], not suffix, value)


def parse_redundancy(value: str) -> Share:
    """Like parse_share, with whole percentages only; `none` disables recovery."""
    if value.lower() == "none":
        return Share(Decimal(0), True, "none")
    try:
        share = parse_share(value)
    except ValueError as exc:
        raise ValueError(f"{exc}, or none") from None
    if share.percent and share.value != share.value.to_integral_value():
        raise ValueError("expected a whole percentage, a size (500M, 2G), or none")
    return share


def recovery_requested(share: Share | None) -> bool:
    """Automatic recovery (None) or any nonzero share generates PAR2 data."""
    return share is None or not share.disabled


def fixed_recovery_layout(share: Share, file_size: int) -> tuple[int, int]:
    """PAR2 block size and recovery block count giving one file a fixed amount.

    About 2000 source blocks keep PAR2's repeated packet lists small, and the
    count is rounded down so the recovery data stays within the reserved size.
    """
    size = int(share.value)
    block_size = (max(4, min(math.ceil(file_size / 2000), size)) + 3) // 4 * 4
    return block_size, min(65535, max(1, size // block_size))


def recovery_blocks(share: Share, source_blocks: int, block_size: int) -> int:
    """Recovery blocks for a share: a percentage of the source blocks, or a fixed size."""
    if share.percent:
        return max(1, math.ceil(source_blocks * share.value / 100))
    return max(1, math.ceil(share.value / block_size))
