from enum import Enum
from pathlib import Path

from bd_archive.constants import PAR2_RECOVERY_RE
from bd_archive.shell.runner import run
from bd_archive.ui.par2_progress import Par2CreateProgress, Par2ScanProgress


class VerifyResult(Enum):
    OK = 0
    REPAIRABLE = 1
    BROKEN = 2


def is_par2_index(path: Path) -> bool:
    """True for the PAR2 index file, False for recovery volumes."""
    return path.suffix == ".par2" and not PAR2_RECOVERY_RE.search(path.name)


def create(
    target_file: Path,
    redundancy: int | None = None,
    *,
    block_size: int | None = None,
    recovery_blocks: int | None = None,
):
    """Protect one file with a percentage, or with an explicit block size and count."""
    if recovery_blocks is not None:
        if redundancy is not None or block_size is None or recovery_blocks < 1:
            raise ValueError("Explicit recovery blocks require a block size and no percentage")
        sizing = [f"-s{block_size}", f"-c{recovery_blocks}"]
    else:
        if redundancy is None:
            raise ValueError("Specify redundancy or recovery blocks")
        sizing = [f"-r{redundancy}"]
    par2_base = target_file.parent / f"{target_file.name}.par2"
    run(["par2", "create", *sizing, "-n1", str(par2_base), str(target_file)], label="par2")


def create_disc(
    base_dir: Path,
    par2_index: Path,
    roots: list[Path],
    *,
    block_size: int,
    recovery_blocks: int,
):
    """Protect the given files and folder trees, recording filenames relative to base_dir.

    Folders are passed as a literal wildcard: par2's own recursive expansion
    includes dotfiles and avoids the OS argument-size limit for large trees.
    The index must not live inside any protected folder.
    """
    if block_size < 4 or recovery_blocks < 1:
        raise ValueError("PAR2 needs a block size and at least one recovery block")
    progress = Par2CreateProgress()
    try:
        run(
            [
                "par2",
                "create",
                f"-B{base_dir}",
                f"-s{block_size}",
                f"-c{recovery_blocks}",
                "-n1",
                "-R",
                str(par2_index),
                *(str(root / "*") if root.is_dir() else str(root) for root in roots),
            ],
            output_transform=progress,
        )
    finally:
        # End the live line before any later output or error message.
        print(progress.finish(), end="", flush=True)


def verify(par2_index: Path, *, base_dir: Path | None = None) -> VerifyResult:
    # par2cmdline exit codes: 0 = all files OK, 1 = repair possible,
    # 2 = repair not possible. Progress only decorates output; the exit
    # status remains authoritative for verification results.
    base_args = [f"-B{base_dir}"] if base_dir is not None else []
    # Without -B, PAR2 resolves file names against the index directory.
    progress = Par2ScanProgress(base_dir if base_dir is not None else par2_index.parent)
    # Hash one file at a time to avoid competing reads and seeks on optical media.
    r = run(
        ["par2", "verify", "-T1", *base_args, str(par2_index)],
        check=False,
        output_transform=progress,
    )
    # PAR2 may exit while the live line is still shown; end it before the result is logged.
    print(progress.finish(), end="", flush=True)
    if r.returncode == 0:
        return VerifyResult.OK
    if r.returncode == 1:
        return VerifyResult.REPAIRABLE
    return VerifyResult.BROKEN


def repair(par2_index: Path) -> bool:
    # Repair's Loading /
    # Constructing / Verifying steps all use \r-updated progress that
    # the line-buffered streamer would swallow.
    r = run(["par2", "repair", str(par2_index)], check=False, passthrough=True)
    return r.returncode == 0
