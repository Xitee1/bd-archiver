"""Single-keypress stdin handling for interactive waits.

Used by `extract`'s auto-detect-next-disc loop to read a single 'e'
without requiring Enter. Linux-only (termios), which matches the rest
of the project (sysfs, ioctl, udisks).
"""

import contextlib
import select
import sys
import termios
import time
import tty


@contextlib.contextmanager
def cbreak_stdin():
    """Put stdin into cbreak mode for the duration of the block.

    On exit (including exceptions) the original terminal attributes are
    restored — leaving the user's shell in cbreak mode after a crash
    would be a nasty surprise. No-op when stdin is not a TTY.
    """
    if not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def read_keypress(timeout: float) -> str | None:
    """Wait up to `timeout` seconds for a single character on stdin.

    Returns the character (lowercased) or None if nothing was pressed.
    On a TTY, must be called from within a `cbreak_stdin()` context to
    read raw single chars. Piped/redirected stdin is read the same way
    (line buffering on the sender means the char arrives after a
    newline; the stray newline is picked up and ignored by the caller's
    next poll). At EOF on a pipe, sleeps for the timeout so the poll
    loop keeps ticking at the normal rate instead of spinning.
    """
    rlist, _, _ = select.select([sys.stdin], [], [], timeout)
    if not rlist:
        return None
    ch = sys.stdin.read(1)
    if not ch:
        # EOF (closed pipe): select reports readable forever — throttle.
        time.sleep(timeout)
        return None
    return ch.lower()
