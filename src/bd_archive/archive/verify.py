from pathlib import Path

from bd_archive.archive.checksums import verify_manifest
from bd_archive.archive.dar_archive import parse_dar_filename
from bd_archive.archive.raw import RAW_CHECKSUMS, is_raw_metadata_name
from bd_archive.constants import RAW_MARKER, RAW_METADATA_DIR, RAW_PAR2_INDEX, RAW_ROOT_MARKER
from bd_archive.shell.deps import check_deps
from bd_archive.tools import par2
from bd_archive.tools.par2 import VerifyResult, is_par2_index
from bd_archive.ui.logger import log


def verify_disc(disc_path: Path, label: str = "", quiet: bool = False) -> VerifyResult:
    """Verify with PAR2 where available, otherwise with SHA-512 checksums.

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
    raw_v2 = (disc_path / RAW_ROOT_MARKER).is_file()
    raw_metadata = disc_path if raw_v2 else disc_path / RAW_METADATA_DIR
    raw = raw_v2 or (raw_metadata / RAW_MARKER).is_file()
    if raw:
        # Both layouts record paths relative to the disc root. Payload
        # .par2 files are ordinary data, not additional archive indices.
        index = raw_metadata / RAW_PAR2_INDEX
        par2_indices = [index] if index.is_file() else []
    else:
        par2_indices = [p for p in sorted(disc_path.rglob("*.par2")) if is_par2_index(p)]
    manifests: dict[Path, tuple[Path, set[Path] | None]] = {}
    if raw and not par2_indices:
        payload = {
            p
            for p in disc_path.rglob("*")
            if p.is_file()
            and (
                not (p.parent == disc_path and is_raw_metadata_name(p.name))
                if raw_v2
                else not p.is_relative_to(raw_metadata)
            )
        }
        manifests[raw_metadata / RAW_CHECKSUMS] = (disc_path, payload)
    elif not raw:
        # Check each unprotected slice, including mixed packed discs. A
        # protected sibling must not hide an archive created with -r 0.
        protected = {p.with_suffix("") for p in par2_indices}
        protected_archives = {
            (p.parent, parsed[:2])
            for p in protected
            if (parsed := parse_dar_filename(p.name)) is not None
        }
        candidates = set(disc_path.rglob("*.sha512"))
        candidates.update(Path(str(p) + ".sha512") for p in disc_path.rglob("*.dar"))
        for manifest in sorted(candidates):
            target = manifest.with_suffix("")
            parsed = parse_dar_filename(target.name)
            if target in protected or (
                parsed is not None
                and parsed[2]
                and (target.parent, parsed[:2]) in protected_archives
            ):
                continue
            manifests[manifest] = (manifest.parent, {target} if parsed else None)

    if not par2_indices and not manifests:
        # Nothing verifiable is not "verified OK" — a wrong disc, an
        # empty mount, or a botched burn must not pass.
        log.error("No PAR2 files or SHA-512 checksums found — nothing could be verified")
        return VerifyResult.BROKEN

    worst = VerifyResult.OK
    if par2_indices:
        check_deps("par2")
    for par2_index in par2_indices:
        if not quiet:
            log.info(f"PAR2 check: {par2_index.relative_to(disc_path)}")
        result = par2.verify(par2_index, base_dir=disc_path) if raw else par2.verify(par2_index)
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

    for manifest, (base_dir, expected_files) in manifests.items():
        if not quiet:
            log.info(f"SHA-512 check: {manifest.relative_to(disc_path)} (no PAR2 recovery)")
        try:
            verify_manifest(manifest, base_dir, expected_files=expected_files)
        except (OSError, ValueError) as exc:
            log.error(f"SHA-512 verification failed: {exc}")
            worst = VerifyResult.BROKEN
        else:
            if not quiet:
                log.ok("SHA-512: data intact")

    if worst == VerifyResult.OK:
        if not quiet:
            log.ok("Verification passed")
    elif worst == VerifyResult.REPAIRABLE:
        log.warn("Repair needed — can be fixed with PAR2")
    else:
        log.error("Verification FAILED")

    return worst
