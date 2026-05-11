from enum import Enum
from pathlib import Path

from bd_archive.constants import PAR2_RECOVERY_RE
from bd_archive.shell.runner import run


class VerifyResult(Enum):
    OK = 0
    REPAIRABLE = 1
    BROKEN = 2


def is_par2_index(path: Path) -> bool:
    """True for the PAR2 index file, False for recovery volumes."""
    return path.suffix == ".par2" and not PAR2_RECOVERY_RE.search(path.name)


def create(target_file: Path, redundancy: int):
    par2_base = target_file.parent / f"{target_file.name}.par2"
    run(
        ["par2", "create", f"-r{redundancy}", "-n1", str(par2_base), str(target_file)], label="par2"
    )


def verify(par2_index: Path) -> VerifyResult:
    # par2cmdline exit codes: 0 = all files OK, 1 = repair possible,
    # 2 = repair not possible. Maps directly onto VerifyResult, so we
    # use passthrough to let par2 paint its "Scanning: X%" progress
    # straight to the terminal — verify takes ~20 min on a 25 GB BD-R
    # and is otherwise a black screen.
    r = run(["par2", "verify", str(par2_index)], check=False, passthrough=True)
    if r.returncode == 0:
        return VerifyResult.OK
    if r.returncode == 1:
        return VerifyResult.REPAIRABLE
    return VerifyResult.BROKEN


def repair(par2_index: Path) -> bool:
    # Same passthrough rationale as verify: repair's Loading /
    # Constructing / Verifying steps all use \r-updated progress that
    # the line-buffered streamer would swallow.
    r = run(["par2", "repair", str(par2_index)], check=False, passthrough=True)
    return r.returncode == 0
