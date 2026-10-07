"""Move selected units without a journal, overwrites, or unverified source removal."""

import ctypes
import errno
import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path

from bd_archive.archive.prepare import Plan, Unit
from bd_archive.archive.raw import RawEntry, scan_raw_source
from bd_archive.shell.format import human_bytes
from bd_archive.ui.progress import Progress


def entry_matches(path: Path, entry: RawEntry) -> bool:
    info = path.lstat()
    return (
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_ino,
        info.st_dev,
    ) == (entry.mode, entry.size, entry.mtime_ns, entry.ctime_ns, entry.inode, entry.device)


def check_unit(source: Path, unit: Unit) -> None:
    root = source / unit.path
    if not all(entry_matches(source / entry.path, entry) for entry in unit.entries):
        raise ValueError(f"Selected source changed: {unit.path}; run prepare again")
    if root.is_dir():
        expected = {e.path[len(unit.path) + 1 :] for e in unit.entries if e.path != unit.path}
        if {e.path for e in scan_raw_source(root)} != expected:
            raise ValueError(f"Selected source changed: {unit.path}; run prepare again")


def existing_parent(path: Path) -> Path:
    while not path.exists():
        path = path.parent
    if not path.is_dir():
        raise ValueError(f"Not a directory: {path}")
    return path


def check_space(source: Path, output: Path, selected: list[Unit]) -> None:
    """Check additional allocation only for units requiring cross-device copies."""
    target = existing_parent(output)
    device = target.stat().st_dev
    fs = os.statvfs(target)
    block = fs.f_frsize or fs.f_bsize
    required = 0
    for unit in selected:
        if any(e.device != device for e in unit.entries):
            required += sum(
                ((e.size + block - 1) // block) * block if stat.S_ISREG(e.mode) else block
                for e in unit.entries
            )
    available = fs.f_bavail * block
    if required > available:
        raise ValueError(
            f"Not enough destination space: need {human_bytes(required)}, "
            f"available {human_bytes(available)} at {target}"
        )


def rename_exclusive(source: Path, target: Path) -> None:
    """Linux rename with RENAME_NOREPLACE, including for directory units."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_verified(source: Path, target: Path, entry: RawEntry) -> None:
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
    # Persist copied metadata as well as contents before removing any source.
    with target.open("rb") as reader:
        os.fsync(reader.fileno())


def move_unit(source: Path, unit: Unit, target: Path) -> None:
    check_unit(source, unit)
    origin = source / unit.path
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    device = target.parent.stat().st_dev
    if all(e.device == device for e in unit.entries):
        try:
            rename_exclusive(origin, target)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # Bind mounts can return EXDEV even when st_dev is identical.
            required = sum(e.size for e in unit.entries if stat.S_ISREG(e.mode))
            if required > shutil.disk_usage(target.parent).free:
                raise ValueError(f"Not enough destination space for {unit.path}") from exc

    with tempfile.TemporaryDirectory(prefix=".bd-prepare-", dir=target.parent) as scratch:
        pending = Path(scratch) / "payload"
        directory = stat.S_ISDIR(unit.entries[0].mode)
        if directory:
            pending.mkdir()
        for entry in unit.entries:
            rel = entry.path[len(unit.path) :].lstrip("/")
            dest = pending / rel if directory else pending
            if stat.S_ISDIR(entry.mode):
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                copy_verified(source / entry.path, dest, entry)
        check_unit(source, unit)
        for entry in reversed(unit.entries):
            if stat.S_ISDIR(entry.mode):
                dest = pending / entry.path[len(unit.path) :].lstrip("/")
                sync_directory(dest)
                shutil.copystat(source / entry.path, dest)
        rename_exclusive(pending, target)
        sync_directory(target.parent)
        # Never remove the destination on a later error: it may already hold
        # the only remaining copies of previously removed source files.
        for entry in unit.entries:
            if stat.S_ISREG(entry.mode):
                original = source / entry.path
                if not entry_matches(original, entry):
                    raise ValueError(f"Source changed; kept both copies: {original}")
                original.unlink()
        for entry in reversed(unit.entries):
            if stat.S_ISDIR(entry.mode):
                (source / entry.path).rmdir()


def move_plan(source: Path, output: Path, units: list[Unit], plan: Plan) -> None:
    selected = [units[i] for group in plan for i in group]
    # Recheck after the interactive prompt, before the first mutation.
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"Output must be empty: {output}")
    for unit in selected:
        check_unit(source, unit)
    check_space(source, output, selected)
    # Keep parent metadata for layouts whose grouping cuts below the root.
    parents = {}
    for unit in selected:
        for parent in Path(unit.path).parents:
            if parent != Path("."):
                parents[parent] = (source / parent).stat()
    output.mkdir(parents=True, exist_ok=True)
    for number, group in enumerate(plan, 1):
        disc = output / f"disc_{number:04d}"
        disc.mkdir()
        for i in group:
            move_unit(source, units[i], disc / units[i].path)
        for parent, info in sorted(
            parents.items(), key=lambda item: len(item[0].parts), reverse=True
        ):
            target = disc / parent
            if target.is_dir():
                os.chmod(target, stat.S_IMODE(info.st_mode))
                os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    # Remove only empty grouping ancestors of moved entries, never source itself.
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        try:
            (source / parent).rmdir()
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                raise
