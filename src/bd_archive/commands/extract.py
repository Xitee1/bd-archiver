import contextlib
import re
import shutil
import sys
import tempfile
from pathlib import Path

from bd_archive.archive.checksums import verify_slice
from bd_archive.archive.dar_archive import parse_dar_filename
from bd_archive.archive.disc import DiscIO
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools import dar, par2
from bd_archive.tools.optical import resolve_device
from bd_archive.tools.par2 import VerifyResult, is_par2_index
from bd_archive.ui.logger import log
from bd_archive.ui.progress import Progress, copy_with_progress
from bd_archive.ui.prompts import prompt_disc, prompt_yn

# A dar slice or catalog filename ends in ".NNNN.dar"; stripping that
# off yields the dar archive basename (e.g. "photos-gen1" or, on legacy
# pre-Phase-2 archives, just "photos"). That basename is what dar -x
# wants as input, and what we use to group files by generation in
# staging.
_SLICE_SUFFIX_RE = re.compile(r"\.\d+\.dar$")


def _dar_basename(filename: str) -> str:
    return _SLICE_SUFFIX_RE.sub("", filename)


def _mount_with_prompt(dio: DiscIO, mount_dir: Path, prompt_msg: str) -> Path | None:
    while True:
        prompt_disc(prompt_msg, dio.device)
        mounted = dio.mount(mount_dir)
        if mounted is not None:
            return mounted
        log.error("Could not mount disc")
        if not prompt_yn("Retry?"):
            return None


def _copy_disc_data(
    mounted: Path, disc_basename: str, staging: Path, catalog_verified: bool
) -> list[Path]:
    """Copy slices + sha512 sidecars (and the catalog of this disc's
    generation, if not yet verified) from disc to staging. par2 files
    are NOT copied — fetched lazily on damage.

    Returns the list of slice paths in staging that came from this disc.
    """
    catalog_basename = f"{disc_basename}-catalog"
    if not catalog_verified:
        for cat in mounted.glob(f"{catalog_basename}.*.dar"):
            dest = staging / cat.name
            if not dest.exists():
                shutil.copy2(cat, dest)
        for cat_hash in mounted.glob(f"{catalog_basename}.*.dar.sha512"):
            dest = staging / cat_hash.name
            if not dest.exists():
                shutil.copy2(cat_hash, dest)

    slices = sorted(
        p
        for p in mounted.glob(f"{disc_basename}.[0-9]*.dar")
        if "-catalog" not in p.name
    )
    copied: list[Path] = []
    for sp in slices:
        dest = staging / sp.name
        if dest.exists():
            log.info(f"  {sp.name} already in staging — skipping copy")
            copied.append(dest)
            continue
        copy_with_progress(sp, dest, label=f"copy {sp.name}")
        sha = sp.parent / f"{sp.name}.sha512"
        if sha.exists():
            shutil.copy2(sha, staging / sha.name)
        copied.append(dest)
    return copied


def _verify_catalog_on_staging(staging: Path, catalog_basename: str) -> bool:
    """Verify every catalog slice currently in staging for one generation.
    Drop any that fail sha512 so the next disc carrying them can refetch.

    Returns True only when every present slice verified — a single pass
    flags every corrupt slice (no early return), so multi-slice catalogs
    converge in one fewer disc-iteration than a 'stop at first failure'
    variant would.
    """
    catalog_files = sorted(staging.glob(f"{catalog_basename}.*.dar"))
    if not catalog_files:
        return False
    all_ok = True
    for cf in catalog_files:
        if not verify_slice(cf):
            log.warn(f"Catalog: {cf.name} failed sha512 — discarding, will retry from next disc")
            cf.unlink(missing_ok=True)
            (staging / f"{cf.name}.sha512").unlink(missing_ok=True)
            all_ok = False
    if all_ok:
        log.ok(f"Catalog verified ({len(catalog_files)} slice(s))")
    return all_ok


def _repair_slice(slice_path: Path, mounted: Path, staging: Path) -> bool:
    """Fetch par2 for one slice from a mounted disc, attempt repair, re-verify
    via sha512. Returns True on success."""
    name = slice_path.name
    par2_files = sorted(mounted.glob(f"{name}.*par2"))
    if not par2_files:
        log.error(f"  {name}: no par2 files found on disc")
        return False
    log.info(f"  Fetching par2 ({len(par2_files)} file(s))...")
    for pf in par2_files:
        copy_with_progress(pf, staging / pf.name, label=f"copy {pf.name}")

    idx_candidates = [staging / pf.name for pf in par2_files if is_par2_index(staging / pf.name)]
    if not idx_candidates:
        log.error(f"  {name}: no par2 index file present")
        return False
    par2_idx = idx_candidates[0]

    pre = par2.verify(par2_idx)
    if pre == VerifyResult.OK:
        # par2 disagrees with sha512: trust par2 (block-level) and continue.
        log.warn(f"  {name}: par2 reports OK despite sha512 mismatch")
        return True
    if pre == VerifyResult.BROKEN:
        log.error(f"  {name}: par2 reports unrepairable damage")
        return False

    log.info(f"  {name}: repairing via par2...")
    if not par2.repair(par2_idx):
        log.error(f"  {name}: par2 repair failed")
        return False
    if not verify_slice(slice_path):
        log.error(f"  {name}: sha512 still failing after repair")
        return False
    log.ok(f"  {name}: repaired")
    return True


def _cleanup_par2(staging: Path):
    for pf in staging.glob("*.par2"):
        pf.unlink(missing_ok=True)


def cmd_extract(args):
    check_deps("dar", "par2")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    workdir_is_default = args.workdir is None
    work_dir = Path(args.workdir) if args.workdir else output_dir / ".bd-archive-work"
    staging = work_dir / "slices"
    staging.mkdir(parents=True, exist_ok=True)

    device = resolve_device(args.device)
    dio = DiscIO(device)

    log.step("Restore archive from discs")
    log.info(f"Device:   {device}")
    log.info(f"Output:   {output_dir}")
    log.info(f"Staging:  {staging}")
    log.info("Insert discs from any generation, in any order. The tool")
    log.info("detects generations from filenames and extracts the chain")
    log.info("in order at the end.")

    # Per-generation state. Catalog verification and dar basename live
    # under each gen because the chain may mix legacy (gen 1 without
    # -gen<N> suffix) and new-format generations.
    chain_name: str | None = None
    catalogs_verified: dict[int, bool] = {}
    gen_basenames: dict[int, str] = {}
    unrepairable_slices: list[str] = []
    disc_num = 0

    while True:
        target = disc_num + 1

        # ── 1. Mount disc ─────────────────────────────────────────────────
        mount_dir = Path(tempfile.mkdtemp(prefix="bd-mount-"))
        mounted = _mount_with_prompt(dio, mount_dir, f"Insert disc {target}")
        if mounted is None:
            mount_dir.rmdir()
            sys.exit(1)

        try:
            # Detect chain name + generation from any slice filename.
            dar_files = [p for p in mounted.glob("*.dar") if "-catalog" not in p.name]
            if not dar_files:
                log.error("No dar files found on disc — try another")
                continue
            parsed = parse_dar_filename(dar_files[0].name)
            if parsed is None:
                log.error(f"Unrecognised dar filename: {dar_files[0].name}")
                continue
            disc_name, disc_gen, _ = parsed
            disc_basename = _dar_basename(dar_files[0].name)

            if chain_name is None:
                chain_name = disc_name
                log.info(f"Chain: {chain_name}")
            elif disc_name != chain_name:
                log.error(
                    f"Disc belongs to chain '{disc_name}', but this run is for "
                    f"chain '{chain_name}'. Eject and insert a matching disc."
                )
                continue

            log.info(f"Disc {target}: Gen {disc_gen} ({disc_basename})")
            gen_basenames.setdefault(disc_gen, disc_basename)
            catalog_verified = catalogs_verified.get(disc_gen, False)

            disc_num = target

            # ── 2. Copy data (no par2) ────────────────────────────────────
            log.info(f"Copying disc {disc_num}...")
            copied = _copy_disc_data(mounted, disc_basename, staging, catalog_verified)
            log.ok(f"  {len(copied)} slice(s) staged")
        finally:
            dio.umount(mounted)
            with contextlib.suppress(OSError):
                mount_dir.rmdir()
            dio.eject()

        # ── 3. Verify catalog for this generation (first time it lands) ───
        if not catalogs_verified.get(disc_gen, False):
            log.info(f"Verifying Gen {disc_gen} catalog on staging...")
            if _verify_catalog_on_staging(staging, f"{disc_basename}-catalog"):
                catalogs_verified[disc_gen] = True

        # ── 4. Verify slices on staging via sha512 ───────────────────────
        log.info(f"Verifying disc {disc_num} slices on staging...")
        failed = []
        for sp in copied:
            with Progress(f"sha512 {sp.name}", sp.stat().st_size) as p:
                if not verify_slice(sp, progress=p.advance):
                    failed.append(sp)
        if not failed:
            log.ok(f"  All {len(copied)} slice(s) intact")
        else:
            log.warn(f"  {len(failed)} slice(s) failed sha512 — par2 repair needed")

        # ── 5. Damage path: re-mount disc, fetch par2, repair ────────────
        if failed:
            mount_dir = Path(tempfile.mkdtemp(prefix="bd-mount-"))
            mounted = _mount_with_prompt(
                dio, mount_dir, f"Re-insert disc {disc_num} for par2 repair"
            )
            if mounted is None:
                mount_dir.rmdir()
                sys.exit(1)
            try:
                for sp in failed:
                    if _repair_slice(sp, mounted, staging):
                        continue
                    log.error(f"  {sp.name}: unrecoverable damage")
                    log.warn(
                        f"  {sp.name}: keeping as-is — files from this slice may "
                        f"be corrupt; will be listed in corrupted-files.txt"
                    )
                    unrepairable_slices.append(sp.name)
            finally:
                dio.umount(mounted)
                with contextlib.suppress(OSError):
                    mount_dir.rmdir()
                dio.eject()
            _cleanup_par2(staging)

        # Report current chain collection state.
        gens_collected = sorted(gen_basenames)
        log.info(f"Chain so far: Gen {gens_collected} ({disc_num} disc(s) total)")

        if not prompt_yn("Insert another disc?"):
            break

    if chain_name is None:
        log.error("No discs processed")
        sys.exit(1)

    # ── Extract: one dar -x per generation in order ──────────────────────
    log.step("Extracting archive chain")
    sorted_gens = sorted(gen_basenames)
    log.info(f"Chain: {chain_name}")
    log.info(f"Generations: {sorted_gens}")

    all_corrupted: list[str] = []
    for i, gen in enumerate(sorted_gens):
        basename = gen_basenames[gen]
        log.info(f"Gen {gen}: dar -x {basename}")
        catalog_basename = f"{basename}-catalog"
        has_catalog = any(staging.glob(f"{catalog_basename}.*.dar"))
        # Subsequent generations must overwrite earlier ones (later gens
        # carry the newer file contents). Gen 1 extracts into a clean
        # output dir, so overwrite is a no-op there — but we set it
        # uniformly to keep the call site simple.
        rc, corrupted = dar.extract_sequential(
            staging / basename,
            output_dir,
            catalog_base=staging / catalog_basename if has_catalog else None,
            overwrite=i > 0,
        )
        all_corrupted.extend(corrupted)
        if rc != 0:
            log.error(f"Gen {gen} dar extract failed (exit {rc})")
            log.info(f"Slices remain in: {staging}")
            log.info(
                f"Manual retry: dar -x {staging / basename} -R {output_dir} --sequential-read -wa"
            )
            sys.exit(1)

    if not all_corrupted and not unrepairable_slices:
        log.ok("Extraction complete!")
    else:
        log.warn(
            f"Extraction finished with corruption: "
            f"{len(all_corrupted)} file(s) reported by dar, "
            f"{len(unrepairable_slices)} slice(s) unrepairable"
        )

    # Write corrupted-files.txt manifest into output_dir (NOT into the
    # workdir, which may be auto-cleaned) when anything went sideways.
    manifest_path: Path | None = None
    if all_corrupted or unrepairable_slices:
        manifest_path = output_dir / "corrupted-files.txt"
        lines = [
            "# bd-archive: corrupted-files manifest",
            "# Files listed here are present in the output but their bytes",
            "# could not be validated. par2 repair on the affected disc(s)",
            "# followed by a re-run of `bd-archive extract` will overwrite",
            "# them with intact data if the par2 recovery succeeds.",
            "",
        ]
        if all_corrupted:
            lines.append(f"## {len(all_corrupted)} file(s) reported by dar with bad CRC:")
            for fp in all_corrupted:
                try:
                    rel = str(Path(fp).resolve().relative_to(output_dir.resolve()))
                except ValueError:
                    rel = fp
                lines.append(rel)
            lines.append("")
        if unrepairable_slices:
            lines.append(f"## {len(unrepairable_slices)} slice(s) failed sha512 + par2 repair:")
            for sn in unrepairable_slices:
                lines.append(sn)
            lines.append("")
            lines.append(
                "# Files originating from these slices may be "
                "corrupt even if dar didn't report them above —"
            )
            lines.append("# slice-level corruption can also damage dar's internal metadata.")
        manifest_path.write_text("\n".join(lines) + "\n")
        log.warn(f"Wrote {manifest_path}")

    # Sum extracted size BEFORE cleaning the workdir, since the default
    # workdir lives under output_dir and we'd otherwise count its bytes.
    total = sum(
        f.stat().st_size for f in output_dir.rglob("*") if f.is_file() and work_dir not in f.parents
    )

    if workdir_is_default:
        shutil.rmtree(work_dir, ignore_errors=True)

    log.step("Restore complete")
    print(f"\n  Chain:        {chain_name}")
    print(f"  Generations:  {sorted_gens}")
    print(f"  Discs:        {disc_num}")
    print(f"  Output:       {output_dir}")
    print(f"  Size:         {human_bytes(total)}")
    if manifest_path is not None:
        print(f"  CORRUPT:      {manifest_path}")
    if not workdir_is_default:
        print(f"\n  Cleanup staging: rm -rf {work_dir}")
    print()

    # Non-zero exit when corruption was detected so scripts know the
    # restore was not fully clean.
    if all_corrupted or unrepairable_slices:
        sys.exit(1)
