from pathlib import Path

from bd_archive.archive.checksums import verify_dar_hashes
from bd_archive.tools import par2
from bd_archive.tools.par2 import VerifyResult, is_par2_index
from bd_archive.ui.logger import log


def verify_disc(disc_path: Path, label: str = "",
                quiet: bool = False) -> VerifyResult:
    if not quiet:
        log.step(f"Verifying: {label or disc_path}")

    worst = VerifyResult.OK

    # SHA-512 — dar emits one .sha512 file per slice (and per catalog
    # slice). PAR2 and README have no hash by design: PAR2 is
    # self-verifying and README is non-load-bearing.
    hash_files = sorted(disc_path.glob("*.sha512"))
    if hash_files:
        if not quiet:
            log.info(f"Checking SHA-512 hashes ({len(hash_files)} file(s))...")
        ok_count, fail_count = verify_dar_hashes(disc_path)
        if fail_count == 0:
            log.ok(f"SHA-512: all {ok_count} file(s) intact")
        else:
            log.error(f"SHA-512: {fail_count} file(s) corrupted!")
            worst = VerifyResult.BROKEN
    elif not quiet:
        log.warn("No .sha512 hash files found")

    # PAR2
    par2_indices = [p for p in sorted(disc_path.glob("*.par2"))
                    if is_par2_index(p)]
    for par2_index in par2_indices:
        if not quiet:
            log.info(f"PAR2 check: {par2_index.name}")
        result = par2.verify(par2_index)
        if result == VerifyResult.OK:
            log.ok("PAR2: data intact")
        elif result == VerifyResult.REPAIRABLE:
            log.warn("PAR2: damage detected — repair possible")
            if worst == VerifyResult.OK:
                worst = VerifyResult.REPAIRABLE
        else:
            log.error("PAR2: damage detected — repair NOT possible")
            worst = VerifyResult.BROKEN

    if not par2_indices and not quiet:
        log.warn("No PAR2 files found")

    if worst == VerifyResult.OK:
        log.ok("Verification passed")
    elif worst == VerifyResult.REPAIRABLE:
        log.warn("Repair needed — can be fixed with PAR2")
    else:
        log.error("Verification FAILED")

    return worst
