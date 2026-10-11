"""Turn PAR2's per-file verify output into one live scan line plus problem reports."""

import os
import re
import shutil
import sys
import time
from pathlib import Path

from bd_archive.shell.format import human_bytes
from bd_archive.ui.logger import log

CLEAR_LINE = "\r\033[K"
_FIELD_SEPARATOR = "  |  "
_ELLIPSIS = "…"
# Narrower names carry no useful information; the live line then shows progress only.
_MIN_NAME_COLUMNS = 8
# PAR2 prints names longer than 56 characters as 28 leading + "..." + 28 trailing characters.
_PAR2_SHORT_PART = 28
_PAR2_SHORT_MARK = "..."

# PAR2 calls files with recovery data "recoverable", which reads like damage.
_FILE_COUNT_RE = re.compile(r"There are (\d+) recoverable files and (\d+) other files\.")
_TOTAL_SIZE_RE = re.compile(r"The total size of the data files is (\d+) bytes\.")
_SCAN_RE = re.compile(r"Scanning: (\d+(?:\.\d+)?)%")
_OPENING_RE = re.compile(r'Opening: "(.*)"')
_TARGET_RE = re.compile(r'Target: "(.*)" - (.*)\.')
_DAMAGED_RE = re.compile(r"damaged\. Found (\d+) of (\d+) data blocks")
_READ_ERROR_RE = re.compile(r"Could not read \d+ bytes from (.*) at offset \d+: (.*)")


def fit_name(head: str, tail: str | None, columns: int) -> str:
    """Shorten a path in the middle to at most columns characters.

    tail is None when head is the complete name. Otherwise the middle between
    head and tail is already unknown and only the two ends can shrink further.
    """
    if tail is None:
        if len(head) <= columns:
            return head
        half = len(head) // 2
        head, tail = head[:half], head[half:]
    elif len(head) + len(_ELLIPSIS) + len(tail) <= columns:
        return head + _ELLIPSIS + tail
    if columns < _MIN_NAME_COLUMNS:
        return ""
    keep_tail = (columns - len(_ELLIPSIS)) // 2
    keep_head = columns - len(_ELLIPSIS) - keep_tail
    return head[:keep_head] + _ELLIPSIS + tail[len(tail) - keep_tail :]


class Par2ScanProgress:
    """Transform PAR2 verify output into a live scan line and permanent problem reports.

    On a terminal, one line shows PAR2's aggregate scan percentage, the average
    throughput, the remaining source-scan time and the file being read. Intact
    files leave no output. Damaged, missing and unreadable files are reported
    once each on a permanent line above the live line. Without a terminal, the
    live line becomes throttled log lines and the file being read is not shown.

    Estimates use the reported data size and average progress since the source
    scan began. Extra-file scans have a different denominator and are excluded.
    """

    def __init__(self, base_dir: Path | None = None):
        # Read errors name absolute paths; reports use paths relative to base_dir
        # like PAR2's own Target lines.
        self.base_dir = os.path.abspath(base_dir) if base_dir is not None else None
        self.total = 0
        self.start: float | None = None
        self.tty = sys.stdout.isatty()
        # Progress fields of the visible live line, None while no live line is shown.
        self.live: str | None = None
        # Head and tail of the file being read; tail is None when head is complete.
        self.current: tuple[str, str | None] | None = None
        self.last_log: float | None = None
        self.reported_unreadable: set[str] = set()

    def __call__(self, line: str) -> str:
        record = line.strip()
        size = _TOTAL_SIZE_RE.fullmatch(record)
        if size:
            self.total = int(size[1])
        if record == "Verifying source files:":
            self.start = time.monotonic()
            self.last_log = None
        elif record.startswith(("Scanning extra files:", "Verifying repaired files:")):
            self.start = None

        if counts := _FILE_COUNT_RE.fullmatch(record):
            return (
                f"Recovery set: {counts[1]} files with recovery data, "
                f"{counts[2]} files without recovery data.\n"
            )
        if scan := _SCAN_RE.fullmatch(record):
            return self._scan(record, float(scan[1]))
        if opening := _OPENING_RE.fullmatch(record):
            return self._opening(opening[1])
        if target := _TARGET_RE.fullmatch(record):
            return self._target(target[1], target[2])
        if read_error := _READ_ERROR_RE.fullmatch(record):
            return self._read_error(read_error[1], read_error[2])
        if self.live is not None:
            if not record:
                return ""
            # Any other output ends the scan: keep its final progress, continue below it.
            return self.finish() + line
        return line

    def finish(self) -> str:
        """End the live line, keeping its progress fields as a permanent line."""
        if self.live is None:
            return ""
        final = CLEAR_LINE + self.live + "\n"
        self.live = None
        self.current = None
        return final

    def _scan(self, record: str, pct: float) -> str:
        now = time.monotonic()
        status = record
        if self.start is not None and self.total > 0:
            elapsed = now - self.start
            if 0 < pct <= 100 and elapsed > 0:
                speed = self.total * pct / 100 / elapsed
                remaining = max(0, int(elapsed * (100 - pct) / pct))
                eta = f"ETA {remaining // 60}m{remaining % 60:02d}s"
                status += f"{_FIELD_SEPARATOR}{human_bytes(speed)}/s{_FIELD_SEPARATOR}{eta}"
        if self.tty:
            self.live = status
            return CLEAR_LINE + self._live_line()
        # Keep redirected logs readable during long optical-disc scans.
        if self.last_log is not None and now - self.last_log < 5 and pct != 100:
            return ""
        self.last_log = now
        return status + "\n"

    def _live_line(self) -> str:
        assert self.live is not None
        text = self.live
        if self.current is not None:
            # Leave the last column free so the cursor never wraps onto a new line.
            columns = shutil.get_terminal_size().columns - 1 - len(text) - len(_FIELD_SEPARATOR)
            name = fit_name(*self.current, columns)
            if name:
                text += _FIELD_SEPARATOR + name
        return text

    def _opening(self, name: str) -> str:
        cut, mark = _PAR2_SHORT_PART, _PAR2_SHORT_MARK
        if len(name) == 2 * cut + len(mark) and name[cut : cut + len(mark)] == mark:
            self.current = (name[:cut], name[cut + len(mark) :])
        else:
            self.current = (name, None)
        if self.live is None:
            return ""
        return CLEAR_LINE + self._live_line()

    def _target(self, name: str, status: str) -> str:
        if status == "found":
            return ""
        if damaged := _DAMAGED_RE.fullmatch(status):
            status = f"damaged (found {damaged[1]} of {damaged[2]} blocks)"
        return self._report(f"{name} - {status}")

    def _read_error(self, path: str, error: str) -> str:
        # PAR2 reports one unreadable file once per read attempt.
        name = self._relative(path)
        if name in self.reported_unreadable:
            return ""
        self.reported_unreadable.add(name)
        return self._report(f"{name} - unreadable ({error})")

    def _relative(self, path: str) -> str:
        if self.base_dir is None:
            return path
        try:
            return str(Path(os.path.abspath(path)).relative_to(self.base_dir))
        except ValueError:
            return path

    def _report(self, problem: str) -> str:
        line = log.warn_line(problem) + "\n"
        if self.live is None:
            return line
        # Print the report where the live line was, then redraw the live line below it.
        return CLEAR_LINE + line + self._live_line()


_PAR2_LABEL_RE = re.compile(r"\s*\[par2\] (.*)", re.DOTALL)
_BLOCK_SIZE_RE = re.compile(r"Block size: (\d+)")
_SOURCE_FILES_RE = re.compile(r"Source file count: (\d+)")
_SOURCE_BLOCKS_RE = re.compile(r"Source block count: (\d+)")
_CREATE_PROGRESS_RE = re.compile(r"(Constructing|Processing): (\d+(?:\.\d+)?)%")
_CREATE_OPENING_RE = re.compile(r"Opening: (.*)")
_CREATE_PHASE_DONE = "Constructing: done."


def rate_fields(total: int, pct: float, elapsed: float) -> str:
    """Average throughput and remaining time, as shown on PAR2 live lines."""
    if total <= 0 or not 0 < pct <= 100 or elapsed <= 0:
        return ""
    speed = total * pct / 100 / elapsed
    remaining = max(0, int(elapsed * (100 - pct) / pct))
    eta = f"ETA {remaining // 60}m{remaining % 60:02d}s"
    return f"{_FIELD_SEPARATOR}{human_bytes(speed)}/s{_FIELD_SEPARATOR}{eta}"


class Par2CreateProgress:
    """Show PAR2 creation progress on one live line instead of one line per update.

    Opening, Constructing and Processing records update the live line; Processing
    adds the average throughput and remaining time, based on the source block
    count and block size PAR2 reports. All other output passes through below the
    final live state. Without a terminal, progress becomes throttled log lines.

    Records may also arrive inside another tool's output with a ``[par2]`` label,
    as from DAR's slice hook; the live line then keeps that label.
    """

    def __init__(self, prefix: str = "  [par2] "):
        self.prefix = prefix
        self.tty = sys.stdout.isatty()
        self.live: str | None = None
        self.name: str | None = None
        self.block_size = 0
        self.source_blocks = 0
        self.source_files = 0
        self.opened = 0
        self.start: float | None = None
        self.last_log: float | None = None

    def __call__(self, line: str) -> str:
        record = line.strip()
        label = ""
        if labelled := _PAR2_LABEL_RE.fullmatch(record):
            # Keep the relayed label's spacing so live and other lines align.
            record, label = labelled[1], "  [par2] "
        if size := _BLOCK_SIZE_RE.fullmatch(record):
            # Each PAR2 run starts with its block size; reset per-run state.
            self.block_size, self.opened, self.start = int(size[1]), 0, None
        elif files := _SOURCE_FILES_RE.fullmatch(record):
            self.source_files = int(files[1])
        elif blocks := _SOURCE_BLOCKS_RE.fullmatch(record):
            self.source_blocks = int(blocks[1])

        if progress := _CREATE_PROGRESS_RE.fullmatch(record):
            phase, pct = progress[1], float(progress[2])
            status = record
            if phase == "Processing":
                now = time.monotonic()
                if self.start is None:
                    self.start = now
                total = self.block_size * self.source_blocks
                status += rate_fields(total, pct, now - self.start)
            return self._show(label + status, final=pct == 100)
        if record == _CREATE_PHASE_DONE:
            return self._show(label + record, final=True)
        if opening := _CREATE_OPENING_RE.fullmatch(record):
            self.opened += 1
            files = f"/{self.source_files}" if self.source_files else ""
            return self._show(f"{label}Opening: {self.opened}{files} files", name=opening[1])
        if not record and self.live is not None:
            return ""
        # Any other output ends the live line: keep its final state, continue below it.
        return self.finish() + self.prefix + line.rstrip("\r\n") + "\n"

    def finish(self) -> str:
        """End the live line, keeping its progress fields as a permanent line."""
        if self.live is None:
            return ""
        final = CLEAR_LINE + self.prefix + self.live + "\n"
        self.live = None
        self.name = None
        return final

    def _show(self, status: str, *, name: str | None = None, final: bool = False) -> str:
        if self.tty:
            self.live, self.name = status, name
            text = self.prefix + status
            if name is not None:
                # Leave the last column free so the cursor never wraps onto a new line.
                columns = shutil.get_terminal_size().columns - 1 - len(text) - len(_FIELD_SEPARATOR)
                if shown := fit_name(name, None, columns):
                    text += _FIELD_SEPARATOR + shown
            return CLEAR_LINE + text
        # Keep redirected logs readable: at most one progress line every 5 seconds.
        now = time.monotonic()
        if not final and self.last_log is not None and now - self.last_log < 5:
            return ""
        self.last_log = now
        return self.prefix + status + "\n"
