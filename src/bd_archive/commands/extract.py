import contextlib
import shutil
import sys
import tempfile
from pathlib import Path

from bd_archive.archive.checksums import verify_slice
from bd_archive.archive.dar_archive import DiscArchive, find_disc_archives
from bd_archive.archive.disc import DiscIO
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools import dar, par2
from bd_archive.tools import eject as eject_tool
from bd_archive.tools.optical import resolve_device
from bd_archive.tools.par2 import VerifyResult, is_par2_index
from bd_archive.ui.keypress import cbreak_stdin, read_keypress
from bd_archive.ui.logger import log
from bd_archive.ui.progress import Progress, copy_with_progress
from bd_archive.ui.prompts import prompt_disc, prompt_yn

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧"
POLL_INTERVAL_S = 0.3


def _prompt_chain(names: list[str]) -> str:
    """Numbered picker for which archive chain to extract, used when the
    first disc of a run is a packed (shared) disc carrying more than one
    chain. EOFError from input() bubbles up as the usual cancel path."""
    log.info(f"Disc contains {len(names)} archive chains:")
    for i, n in enumerate(names, 1):
        log.info(f"  [{i}] {n}")
    while True:
        resp = input(f"Extract which chain [1-{len(names)}]? ").strip()
        try:
            idx = int(resp)
        except ValueError:
            continue
        if 1 <= idx <= len(names):
            return names[idx - 1]


def _mount_with_prompt(dio: DiscIO, mount_dir: Path, prompt_msg: str) -> Path | None:
    while True:
        prompt_disc(prompt_msg, dio.device)
        mounted, mount_err = dio.mount(mount_dir)
        if mounted is not None:
            return mounted
        log.error("Could not mount disc")
        if mount_err:
            log.error(f"  {mount_err}")
        if not prompt_yn("Retry?"):
            return None


def _wait_for_next_disc(dio: DiscIO, mount_dir: Path, target: int) -> Path | None:
    """Poll drive + stdin until a disc is mountable or the user presses 'e'.

    Returns the mount path when a disc is detected and mounts cleanly.
    Returns None when the user pressed 'e' — caller should break the
    disc-collection loop and proceed to the extraction phase.
    """
    is_stdout_tty = sys.stdout.isatty()
    log.info(f"Waiting for disc {target}... (press 'e' to extract all collected discs)")
    if not sys.stdin.isatty():
        log.warn("stdin not a TTY — press Ctrl+C to abort instead of 'e'")

    frame = 0
    try:
        with cbreak_stdin():
            while True:
                if is_stdout_tty:
                    sys.stdout.write(f"\r  {SPINNER_FRAMES[frame]} polling drive...")
                    sys.stdout.flush()
                    frame = (frame + 1) % len(SPINNER_FRAMES)

                key = read_keypress(POLL_INTERVAL_S)
                if key == "e":
                    return None

                if eject_tool.drive_status(dio.device) == eject_tool.CDS_DISC_OK:
                    mounted, _err = dio.mount(mount_dir)
                    if mounted is not None:
                        return mounted
    finally:
        if is_stdout_tty:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()


def _copy_disc_data(
    disc_dir: Path, disc_basename: str, staging: Path, catalog_verified: bool
) -> list[Path]:
    """Copy slices + sha512 sidecars (and the catalog of this disc's
    generation, if not yet verified) from one archive's directory on
    disc (its top-level folder, or the disc root on legacy flat discs)
    to staging. par2 files are NOT copied — fetched lazily on damage.

    Returns the list of slice paths in staging that came from this disc.
    """
    catalog_basename = f"{disc_basename}-catalog"
    if not catalog_verified:
        for cat in disc_dir.glob(f"{catalog_basename}.*.dar"):
            dest = staging / cat.name
            if not dest.exists():
                shutil.copy2(cat, dest)
        for cat_hash in disc_dir.glob(f"{catalog_basename}.*.dar.sha512"):
            dest = staging / cat_hash.name
            if not dest.exists():
                shutil.copy2(cat_hash, dest)

    slices = sorted(
        p for p in disc_dir.glob(f"{disc_basename}.[0-9]*.dar") if "-catalog" not in p.name
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


def _repair_slice(slice_path: Path, disc_dir: Path, staging: Path) -> bool:
    """Fetch par2 for one slice from its directory on a mounted disc,
    attempt repair, re-verify via sha512. Returns True on success."""
    name = slice_path.name
    par2_files = sorted(disc_dir.glob(f"{name}.*par2"))
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
        # Disc 1 keeps the classic press-Enter prompt so the user can read
        # the header info before the run starts. Discs ≥ 2 auto-detect:
        # poll drive + stdin, return on disc-ready or user pressing 'e'.
        mount_dir = Path(tempfile.mkdtemp(prefix="bd-mount-"))
        if target == 1:
            mounted = _mount_with_prompt(dio, mount_dir, f"Insert disc {target}")
            if mounted is None:
                mount_dir.rmdir()
                sys.exit(1)
        else:
            mounted = _wait_for_next_disc(dio, mount_dir, target)
            if mounted is None:
                # 'e' pressed → done collecting, proceed to extraction
                with contextlib.suppress(OSError):
                    mount_dir.rmdir()
                break

        try:
            # Detect every archive on the disc: per-archive top-level
            # folders on v1.1+ discs, slice files at the root on legacy
            # flat discs. A packed (shared) disc carries several.
            archives = find_disc_archives(mounted)
            if not archives:
                log.error("No dar files found on disc — try another")
                continue

            if chain_name is None:
                names = sorted({a.chain_name for a in archives})
                chain_name = names[0] if len(names) == 1 else _prompt_chain(names)
                log.info(f"Chain: {chain_name}")

            matching = [a for a in archives if a.chain_name == chain_name]
            foreign = sorted(a.basename for a in archives if a.chain_name != chain_name)
            if not matching:
                log.error(
                    f"Disc belongs to {', '.join(foreign)}, but this run is for "
                    f"chain '{chain_name}'. Eject and insert a matching disc."
                )
                continue
            if foreign:
                log.info(f"Ignoring foreign archive(s) on disc: {', '.join(foreign)}")

            disc_num = target

            # ── 2. Copy data (no par2) ────────────────────────────────────
            # A packed disc can hold several generations of this chain
            # (e.g. gen1's last disc + gen2's first disc) — stage each.
            staged: list[tuple[DiscArchive, list[Path]]] = []
            for arc in matching:
                log.info(f"Disc {target}: Gen {arc.generation} ({arc.basename})")
                gen_basenames.setdefault(arc.generation, arc.basename)
                log.info(f"Copying disc {disc_num} ({arc.basename})...")
                copied = _copy_disc_data(
                    arc.directory,
                    arc.basename,
                    staging,
                    catalogs_verified.get(arc.generation, False),
                )
                log.ok(f"  {len(copied)} slice(s) staged")
                staged.append((arc, copied))
        finally:
            dio.umount(mounted)
            with contextlib.suppress(OSError):
                mount_dir.rmdir()
            dio.eject()

        # ── 3. Verify catalogs for generations that just landed ──────────
        for arc, _ in staged:
            if not catalogs_verified.get(arc.generation, False):
                log.info(f"Verifying Gen {arc.generation} catalog on staging...")
                if _verify_catalog_on_staging(staging, f"{arc.basename}-catalog"):
                    catalogs_verified[arc.generation] = True

        # ── 4. Verify slices on staging via sha512 ───────────────────────
        log.info(f"Verifying disc {disc_num} slices on staging...")
        failed: list[tuple[Path, DiscArchive]] = []
        n_copied = 0
        for arc, copied in staged:
            n_copied += len(copied)
            for sp in copied:
                with Progress(f"sha512 {sp.name}", sp.stat().st_size) as p:
                    if not verify_slice(sp, progress=p.advance):
                        failed.append((sp, arc))
        if not failed:
            log.ok(f"  All {n_copied} slice(s) intact")
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
                for sp, arc in failed:
                    # Re-resolve the archive's directory on the fresh
                    # mount — the mountpoint path differs per mount.
                    src_dir = mounted / arc.rel_dir if arc.rel_dir else mounted
                    if _repair_slice(sp, src_dir, staging):
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
