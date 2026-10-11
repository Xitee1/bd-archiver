"""Build a disc tree from selected sources without changing them; remove them only on request."""

import errno
import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from bd_archive.archive.plan import Unit
from bd_archive.archive.raw import RawEntry, scan_raw_source
from bd_archive.shell.format import human_bytes
from bd_archive.tools import reflink
from bd_archive.ui.progress import Progress

# Hardlinks are impossible across filesystems and on some filesystems or
# link-count limits; a verified copy is used instead.
_NO_HARDLINK = {errno.EXDEV, errno.EPERM, errno.EMLINK, errno.EOPNOTSUPP, errno.ENOSYS}


@dataclass(frozen=True)
class StageItem:
    source: Path  # the original file or directory
    target: str  # disc-relative path
    entry: RawEntry  # signature of the original at scan time
    link: bool  # a hardlink is acceptable: the original name is removed later


def entry_matches(path: Path, entry: RawEntry, *, ctime: bool = True) -> bool:
    """Compare the scan signature; hardlinking changes ctime, so cleanup ignores it."""
    info = path.lstat()
    actual = (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev)
    expected = (entry.mode, entry.size, entry.mtime_ns, entry.inode, entry.device)
    return actual == expected and (not ctime or info.st_ctime_ns == entry.ctime_ns)


def check_unit(source: Path, unit: Unit, *, ctime: bool = True) -> None:
    """A selected unit must be unchanged and must not have gained entries."""
    root = source / unit.path
    if not all(entry_matches(source / e.path, e, ctime=ctime) for e in unit.entries):
        raise ValueError(f"Source changed: {unit.path}; run create again")
    if root.is_dir():
        expected = {e.path[len(unit.path) + 1 :] for e in unit.entries if e.path != unit.path}
        if {e.path for e in scan_raw_source(root)} != expected:
            raise ValueError(f"Source changed: {unit.path}; run create again")


def existing_parent(path: Path) -> Path:
    while not path.exists():
        path = path.parent
    if not path.is_dir():
        raise ValueError(f"Not a directory: {path}")
    return path


def check_space(destination: Path, items: list[StageItem]) -> None:
    """Require space for every file that may need a copy.

    Hardlink candidates on the destination filesystem need no data space.
    """
    target = existing_parent(destination)
    device = target.stat().st_dev
    fs = os.statvfs(target)
    block = fs.f_frsize or fs.f_bsize
    required = 0
    for item in items:
        if item.link and item.entry.device == device:
            continue
        size = item.entry.size if stat.S_ISREG(item.entry.mode) else 0
        required += max(1, (size + block - 1) // block) * block
    available = fs.f_bavail * block
    if required > available:
        raise ValueError(
            f"Not enough destination space: need {human_bytes(required)}, "
            f"available {human_bytes(available)} at {target}"
        )


def copy_verified(source: Path, target: Path, entry: RawEntry) -> str:
    """Copy, re-read and compare; return the SHA-512 hex digest of the source data."""
    digest = hashlib.sha512()
    with (
        source.open("rb") as reader,
        target.open("xb") as writer,
        Progress(source.name, entry.size) as progress,
    ):
        for chunk in iter(lambda: reader.read(4 * 1024 * 1024), b""):
            writer.write(chunk)
            digest.update(chunk)
            progress.advance(len(chunk))
        writer.flush()
        os.fsync(writer.fileno())
    with target.open("rb") as reader:
        copied = hashlib.file_digest(reader, "sha512").digest()
    if copied != digest.digest() or not entry_matches(source, entry):
        raise ValueError(f"Source changed or copy verification failed: {source}")
    shutil.copystat(source, target)
    return digest.hexdigest()


def _hardlink(source: Path, target: Path) -> bool:
    try:
        os.link(source, target)
    except OSError as exc:
        if exc.errno in _NO_HARDLINK:
            return False
        raise
    return True


def stage(items: list[StageItem], root: Path) -> tuple[dict[str, str], dict[str, int]]:
    """Create every item below ``root``: reflink, else hardlink if allowed, else verified copy.

    The originals are never changed. Returns the digests computed while
    copying, by disc-relative path, and the transferred bytes per method.
    """
    digests: dict[str, str] = {}
    totals = {"reflinked": 0, "hardlinked": 0, "copied": 0}
    ordered = sorted(items, key=lambda item: item.target)
    for item in ordered:
        target = root / item.target
        if stat.S_ISDIR(item.entry.mode):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not entry_matches(item.source, item.entry):
            raise ValueError(f"Source changed: {item.source}; run create again")
        if reflink.try_clone(item.source, target):
            if not entry_matches(item.source, item.entry):
                raise ValueError(f"Source changed: {item.source}; run create again")
            totals["reflinked"] += item.entry.size
        elif item.link and _hardlink(item.source, target):
            totals["hardlinked"] += item.entry.size
        else:
            digests[item.target] = copy_verified(item.source, target, item.entry)
            totals["copied"] += item.entry.size
    # Directory metadata last: creating children changes a directory's mtime.
    for item in reversed(ordered):
        if stat.S_ISDIR(item.entry.mode):
            shutil.copystat(item.source, root / item.target)
    return digests, totals


def remove_units(source: Path, units: list[Unit], ancestors: list[str]) -> None:
    """Remove archived units from the source after checking all of them first.

    Empty grouping ancestors are removed as well, never the source itself.
    """
    for unit in units:
        try:
            check_unit(source, unit, ctime=False)
        except ValueError:
            raise ValueError(
                f"Source changed after creation: {unit.path}; nothing was removed from the source"
            ) from None
    for unit in units:
        for entry in unit.entries:
            if not stat.S_ISDIR(entry.mode):
                (source / entry.path).unlink()
        for entry in reversed(unit.entries):
            if stat.S_ISDIR(entry.mode):
                (source / entry.path).rmdir()
    for parent in sorted(ancestors, key=lambda path: path.count("/"), reverse=True):
        try:
            (source / parent).rmdir()
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                raise
