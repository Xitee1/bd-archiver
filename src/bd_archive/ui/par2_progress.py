"""Add estimated throughput and remaining source-scan time to PAR2 output."""

import re
import sys
import time

from bd_archive.shell.format import human_bytes


class Par2ScanProgress:
    """Transform PAR2's aggregate source-scan records without reading payloads.

    Estimates use the reported data size and average progress since the source
    scan began. Extra-file scans have a different denominator and are excluded.
    """

    def __init__(self):
        self.total = 0
        self.start: float | None = None
        self.tty = sys.stdout.isatty()
        self.progress_visible = False
        self.last_log: float | None = None

    def __call__(self, line: str) -> str:
        record = line.strip()
        size = re.fullmatch(r"The total size of the data files is (\d+) bytes\.", record)
        if size:
            self.total = int(size[1])
        if record == "Verifying source files:":
            self.start = time.monotonic()
            self.last_log = None
        elif record.startswith(("Scanning extra files:", "Verifying repaired files:")):
            self.start = None

        scan = re.fullmatch(r"Scanning: (\d+(?:\.\d+)?)%", record)
        if scan and self.start is not None and self.total > 0:
            now = time.monotonic()
            pct = float(scan[1])
            elapsed = now - self.start
            if 0 < pct <= 100 and elapsed > 0:
                speed = self.total * pct / 100 / elapsed
                remaining = max(0, int(elapsed * (100 - pct) / pct))
                record += (
                    f"  |  {human_bytes(speed)}/s  |  ETA {remaining // 60}m{remaining % 60:02d}s"
                )
            if self.tty:
                self.progress_visible = True
                return f"\r\033[K{record}"
            # Keep redirected logs readable during long optical-disc scans.
            if self.last_log is not None and now - self.last_log < 5 and pct != 100:
                return ""
            self.last_log = now
            return record + "\n"

        if self.progress_visible:
            if not record:
                return ""
            self.progress_visible = False
            return "\n" + line
        return line
