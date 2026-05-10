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
    run(["par2", "create", f"-r{redundancy}", "-n1",
         str(par2_base), str(target_file)], label="par2")


def verify(par2_index: Path) -> VerifyResult:
    r = run(["par2", "verify", str(par2_index)],
            check=False, capture=True)
    out = r.stdout + r.stderr
    if "All files are correct" in out:
        return VerifyResult.OK
    if "Repair is required" in out:
        return VerifyResult.REPAIRABLE
    return VerifyResult.BROKEN


def repair(par2_index: Path) -> bool:
    r = run(["par2", "repair", str(par2_index)],
            label="par2", check=False)
    return r.returncode == 0
