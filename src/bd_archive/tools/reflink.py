"""Opportunistic Linux copy-on-write file cloning, without an external tool."""

import errno
import fcntl
import shutil
import tempfile
from pathlib import Path

# linux/fs.h: _IOW(0x94, 9, int). The fallback also supports older Python versions.
_FICLONE = getattr(fcntl, "FICLONE", 0x40049409)
_UNSUPPORTED = {errno.EXDEV, errno.EOPNOTSUPP, errno.ENOTTY, errno.EINVAL, errno.ENOSYS}


def try_clone(source: Path, destination: Path) -> bool:
    """Clone one file, preserving metadata; return False if cloning is unsupported.

    Only capability/compatibility errors permit a normal-copy fallback.
    Permission, storage and I/O errors propagate. The destination is published
    after both cloning and metadata preservation succeed; any temporary file
    is removed on failure, including cancellation. The source is never changed.
    """
    pending = None
    try:
        with (
            source.open("rb") as reader,
            tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix=".bd-reflink-", delete=False
            ) as writer,
        ):
            pending = Path(writer.name)
            try:
                fcntl.ioctl(writer.fileno(), _FICLONE, reader.fileno())
            except OSError as exc:
                if exc.errno in _UNSUPPORTED:
                    return False
                raise
        shutil.copystat(source, pending)
        pending.replace(destination)
        return True
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
