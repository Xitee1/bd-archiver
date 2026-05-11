import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path

from bd_archive.shell.format import human_bytes

UPDATE_INTERVAL_S = 0.5
COPY_CHUNK = 4 * 1024 * 1024  # 4 MiB — balances syscall overhead vs. responsiveness
MIN_PROGRESS_BYTES = 50 * 1024 * 1024  # below this, copy silently — par2 indices,
# sha512 sidecars etc. would just be noise


class Progress:
    """Throttled byte-counted progress for a single operation.

    Use as a context manager. On TTY, updates a single line via `\\r`;
    on non-TTY (pipe/log file), falls back to periodic line prints.
    A final summary line is printed on successful exit; on exception
    (or if no progress was reported), the in-progress line is cleared
    so the next output is readable.
    """

    def __init__(self, label: str, total: int, min_size: int = MIN_PROGRESS_BYTES):
        self.label = label
        self.total = max(total, 1)
        self.done = 0
        self.start = time.monotonic()
        self.last_update = self.start
        self.tty = sys.stdout.isatty()
        # Tiny operations (par2 indices, sha512 sidecars, ...) skip
        # rendering so a bulk loop doesn't spam one final-summary line
        # per file. The advance counter still increments — callers can
        # read .done if they need it.
        self.silent = total < min_size

    def __enter__(self) -> "Progress":
        return self

    def __exit__(self, exc_type, *_) -> None:
        if self.silent:
            return
        if exc_type is None and self.done > 0:
            self._render_final()
        elif self.tty:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def advance(self, n: int) -> None:
        self.done += n
        if self.silent:
            return
        now = time.monotonic()
        if now - self.last_update >= UPDATE_INTERVAL_S:
            self._render(now)
            self.last_update = now

    def _format(self, now: float) -> str:
        elapsed = max(now - self.start, 0.001)
        speed = self.done / elapsed
        pct = self.done * 100 // self.total
        if speed > 0 and self.done < self.total:
            eta_s = int((self.total - self.done) / speed)
            eta = f"  ETA {eta_s // 60}m{eta_s % 60:02d}s"
        else:
            eta = ""
        return (
            f"  {self.label}  {pct}%  "
            f"{human_bytes(self.done)} / {human_bytes(self.total)}  "
            f"@ {human_bytes(speed)}/s{eta}"
        )

    def _render(self, now: float) -> None:
        line = self._format(now)
        if self.tty:
            sys.stdout.write(f"\r\033[K{line}")
            sys.stdout.flush()
        else:
            print(line, flush=True)

    def _render_final(self) -> None:
        elapsed = max(time.monotonic() - self.start, 0.001)
        speed = self.done / elapsed
        line = (
            f"  {self.label}  done — "
            f"{human_bytes(self.done)} @ {human_bytes(speed)}/s "
            f"in {elapsed:.1f}s"
        )
        if self.tty:
            sys.stdout.write(f"\r\033[K{line}\n")
            sys.stdout.flush()
        else:
            print(line, flush=True)


def copy_with_progress(
    src: Path,
    dst: Path,
    label: str | None = None,
    chunk: int = COPY_CHUNK,
    min_size: int = MIN_PROGRESS_BYTES,
) -> None:
    """Like shutil.copy2 (preserves mtime/permissions). For files at or
    above `min_size`, emits a Progress as bytes flow; smaller files copy
    silently to keep par2 indices / sha512 sidecars from spamming.
    `dst` must be a full file path, not a dir."""
    size = src.stat().st_size
    if size < min_size:
        shutil.copy2(src, dst)
        return
    with open(src, "rb") as fi, open(dst, "wb") as fo, Progress(label or src.name, size) as p:
        while True:
            buf = fi.read(chunk)
            if not buf:
                break
            fo.write(buf)
            p.advance(len(buf))
    shutil.copystat(src, dst)


# Re-exported so callers needing the type for an `advance`-style callback
# can import it from here without pulling typing themselves.
ProgressCallback = Callable[[int], None]
