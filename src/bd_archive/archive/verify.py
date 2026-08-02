from pathlib import Path

from bd_archive.tools import par2
from bd_archive.tools.par2 import VerifyResult, is_par2_index
from bd_archive.ui.logger import log


def verify_disc(disc_path: Path, label: str = "", quiet: bool = False) -> VerifyResult:
    """par2-verify every archive under disc_path.

    `quiet` suppresses the success chatter (step header, per-index info,
    OK lines) for callers that report the outcome themselves (post-burn
    check inside `burn`); warnings and errors always print.
    """
    if not quiet:
        log.step(f"Verifying: {label or disc_path}")

    # PAR2 alone is sufficient: it verifies source-file MD5/CRC32 packets
    # AND its own packet hashes, so it catches both slice and par2
    # corruption in a single disc read. The .sha512 sidecars on disc are
    # still used by `extract`, where they run against local staging and
    # par2 is only fetched on mismatch.
    # rglob, not glob: foldered discs keep each archive's files in a
    # top-level <name>-gen<N>/ directory (and a packed disc carries
    # several); legacy flat discs still match at the root.
    par2_indices = [p for p in sorted(disc_path.rglob("*.par2")) if is_par2_index(p)]
    if not par2_indices:
        # Nothing verifiable is not "verified OK" — a wrong disc, an
        # empty mount, or a botched burn must not pass.
        log.error("No PAR2 files found — nothing could be verified")
        return VerifyResult.BROKEN

    worst = VerifyResult.OK
    for par2_index in par2_indices:
        if not quiet:
            log.info(f"PAR2 check: {par2_index.relative_to(disc_path)}")
        result = par2.verify(par2_index)
        if result == VerifyResult.OK:
            if not quiet:
                log.ok("PAR2: data intact")
        elif result == VerifyResult.REPAIRABLE:
            log.warn("PAR2: damage detected — repair possible")
            if worst == VerifyResult.OK:
                worst = VerifyResult.REPAIRABLE
        else:
            log.error("PAR2: damage detected — repair NOT possible")
            worst = VerifyResult.BROKEN

    if worst == VerifyResult.OK:
        if not quiet:
            log.ok("Verification passed")
    elif worst == VerifyResult.REPAIRABLE:
        log.warn("Repair needed — can be fixed with PAR2")
    else:
        log.error("Verification FAILED")

    return worst
