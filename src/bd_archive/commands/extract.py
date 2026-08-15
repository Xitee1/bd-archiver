import contextlib
import shutil
import sys
import tempfile
from pathlib import Path

from bd_archive.archive.checksums import verify_slice
from bd_archive.archive.dar_archive import DiscArchive, find_disc_archives
from bd_archive.archive.disc import DiscIO, LoopMountError, loop_mounted
from bd_archive.constants import EXTRACT_MARKER_NAME
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
        log.warn("stdin not a TTY — send 'e' followed by a newline to finish collecting")

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

                # status None = the CDROM ioctl is unavailable (odd device,
                # missing permission) — fall back to blind mount attempts
                # so the wait can still complete.
                status = eject_tool.drive_status(dio.device)
                if status == eject_tool.CDS_DISC_OK or status is None:
                    mounted, _err = dio.mount(mount_dir)
                    if mounted is not None:
                        return mounted
    finally:
        if is_stdout_tty:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()


class _DeviceSource:
    """Discs handed to us one at a time through a physical drive.

    Disc 1 waits at the classic press-Enter prompt so the user can read
    the header first; discs >= 2 auto-detect. The set is open-ended —
    open_next() returns None only when the user pressed 'e'.
    """

    item_label = "Disc"

    def __init__(self, device: str):
        self.dio = DiscIO(device)
        self._mount_dir: Path | None = None
        self._mounted: Path | None = None

    def log_source(self):
        log.info(f"Device:   {self.dio.device}")

    def log_hint(self):
        log.info("Insert discs from any generation, in any order. The tool")
        log.info("detects generations from filenames and extracts the chain")
        log.info("in order at the end.")

    def _remember(self, mount_dir: Path, mounted: Path | None) -> Path | None:
        if mounted is None:
            with contextlib.suppress(OSError):
                mount_dir.rmdir()
            return None
        self._mount_dir, self._mounted = mount_dir, mounted
        return mounted

    def open_next(self, target: int) -> Path | None:
        mount_dir = Path(tempfile.mkdtemp(prefix="bd-mount-"))
        if target == 1:
            mounted = _mount_with_prompt(self.dio, mount_dir, f"Insert disc {target}")
            if mounted is None:
                # User gave up on mounting the very first disc — nothing
                # was collected, so this is a failure, not a clean stop.
                self._remember(mount_dir, None)
                sys.exit(1)
        else:
            # None here = 'e' pressed → done collecting.
            mounted = _wait_for_next_disc(self.dio, mount_dir, target)
        return self._remember(mount_dir, mounted)

    def reopen_current(self, disc_num: int) -> Path | None:
        mount_dir = Path(tempfile.mkdtemp(prefix="bd-mount-"))
        mounted = _mount_with_prompt(
            self.dio, mount_dir, f"Re-insert disc {disc_num} for par2 repair"
        )
        return self._remember(mount_dir, mounted)

    def close_current(self):
        if self._mounted is not None:
            self.dio.umount(self._mounted)
        if self._mount_dir is not None:
            with contextlib.suppress(OSError):
                self._mount_dir.rmdir()
        self._mounted = self._mount_dir = None
        self.dio.eject()


class _IsoSource:
    """A fixed, ordered list of ISO images, loop-mounted one at a time.

    Same per-disc flow as a physical drive, minus the interaction: the
    set is known up front, so open_next() walks its own cursor and
    returns None once the list is exhausted. A failed image is fatal —
    the user named it explicitly, so silently skipping it would hide a
    typo or a truncated download.
    """

    item_label = "Image"

    def __init__(self, isos: list[Path]):
        self._isos = isos
        self._cursor = 0
        self._current: Path | None = None
        self._stack: contextlib.ExitStack | None = None

    def log_source(self):
        log.info(f"Images:   {len(self._isos)} ISO file(s)")
        for iso in self._isos:
            log.info(f"            {iso}")

    def log_hint(self):
        log.info("Reading from ISO images instead of discs. Generations are")
        log.info("detected from filenames and extracted in order at the end.")

    def _mount(self, iso: Path) -> Path:
        stack = contextlib.ExitStack()
        try:
            mounted = stack.enter_context(loop_mounted(iso, prefix="bd-extract-"))
        except LoopMountError as e:
            stack.close()
            log.error(str(e))
            sys.exit(1)
        self._stack = stack
        return mounted

    def open_next(self, target: int) -> Path | None:
        if self._cursor >= len(self._isos):
            return None
        iso = self._isos[self._cursor]
        self._cursor += 1
        self._current = iso
        log.info(f"Image {self._cursor}/{len(self._isos)}: {iso.name}")
        return self._mount(iso)

    def reopen_current(self, disc_num: int) -> Path | None:
        assert self._current is not None
        log.info(f"Re-mounting {self._current.name} for par2 repair")
        return self._mount(self._current)

    def close_current(self):
        if self._stack is not None:
            self._stack.close()
            self._stack = None


def _resolve_iso_paths(raw_paths: list[str]) -> list[Path]:
    """Expand the --iso arguments into an ordered list of image files.

    A directory expands to its `disc_*.iso` in lexical (= numerical,
    they are zero-padded) order, also looking in `<dir>/images/` so
    pointing at a create run's output dir just works. Files are taken
    as given. Duplicates are dropped so an accidental
    `--iso out/images out/images/disc_0001.iso` doesn't process an
    image twice.
    """
    resolved: list[Path] = []
    seen: set[Path] = set()
    for raw in raw_paths:
        p = Path(raw)
        if p.is_dir():
            found = sorted(p.glob("disc_*.iso")) or sorted((p / "images").glob("disc_*.iso"))
            if not found:
                log.error(f"No disc_*.iso found in {p} or {p / 'images'}")
                sys.exit(1)
        elif p.is_file():
            found = [p]
        else:
            log.error(f"--iso path does not exist: {p}")
            sys.exit(1)
        for iso in found:
            key = iso.resolve()
            if key not in seen:
                seen.add(key)
                resolved.append(iso)
    return resolved


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
                copy_with_progress(cat, dest, label=f"copy {cat.name}")
        for cat_hash in disc_dir.glob(f"{catalog_basename}.*.dar.sha512"):
            dest = staging / cat_hash.name
            if not dest.exists():
                copy_with_progress(cat_hash, dest)

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
            copy_with_progress(sha, staging / sha.name)
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


def _check_output_dir(output_dir: Path, work_dir: Path) -> str | None:
    """Refuse to extract into a directory holding foreign data.

    dar runs with -wa (always overwrite), so any colliding file in the
    output dir would be silently replaced. The only non-empty dir that
    is safe to write into is a previous extract of the same chain (the
    documented repair/resume path) — identified by the marker file this
    tool drops on every run. Returns the marker's chain name, or None
    when the dir is fresh; the chain match itself happens once the first
    disc reveals which chain this run restores.
    """
    marker = output_dir / EXTRACT_MARKER_NAME
    if marker.exists():
        return marker.read_text().strip() or None
    foreign = [
        e
        for e in output_dir.iterdir()
        if e.name != "corrupted-files.txt" and e.resolve() != work_dir.resolve()
    ]
    if foreign:
        log.error(f"Output dir {output_dir} already contains data (e.g. '{foreign[0].name}').")
        log.info("Extraction overwrites colliding files — refusing to restore into")
        log.info("a directory holding foreign data. Choose an empty -o directory.")
        log.info(
            f"(To resume a pre-existing extract made with an older version, create "
            f"'{EXTRACT_MARKER_NAME}' in it containing the chain name.)"
        )
        sys.exit(1)
    return None


def cmd_extract(args):
    check_deps("dar", "par2")

    # Resolve the disc source first: bad --iso paths and a failed drive
    # detection abort here, before the output dir exists.
    source: _DeviceSource | _IsoSource
    if args.iso:
        check_deps("udisksctl")
        source = _IsoSource(_resolve_iso_paths(args.iso))
    else:
        source = _DeviceSource(resolve_device(args.device))

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    workdir_is_default = args.workdir is None
    work_dir = Path(args.workdir) if args.workdir else output_dir / ".bd-archive-work"

    # Guard before anything (incl. staging) is created in the output dir:
    # a refused run must not leave litter in a directory we don't own.
    marker_chain = _check_output_dir(output_dir, work_dir)

    staging = work_dir / "slices"
    staging.mkdir(parents=True, exist_ok=True)

    log.step("Restore archive from discs")
    source.log_source()
    log.info(f"Output:   {output_dir}")
    log.info(f"Staging:  {staging}")
    source.log_hint()

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
        # None = the source is done handing out discs ('e' pressed on a
        # drive, list exhausted on --iso) → proceed to extraction.
        mounted = source.open_next(target)
        if mounted is None:
            break

        try:
            # Detect every archive on the disc: per-archive top-level
            # folders on v1.1+ discs, slice files at the root on legacy
            # flat discs. A packed (shared) disc carries several.
            archives = find_disc_archives(mounted)
            if not archives:
                log.error(f"No dar files found on this {source.item_label.lower()} — skipping")
                continue

            if chain_name is None:
                names = sorted({a.chain_name for a in archives})
                if marker_chain is not None and marker_chain in names:
                    # Re-run into a previous extract of this chain (repair
                    # or resume) — adopt it without prompting, even on a
                    # packed disc carrying other chains.
                    chain_name = marker_chain
                    log.info(f"Resuming previous extract of chain '{chain_name}'")
                else:
                    chain_name = names[0] if len(names) == 1 else _prompt_chain(names)
                if marker_chain is not None and chain_name != marker_chain:
                    log.error(
                        f"Output dir holds a previous extract of chain "
                        f"'{marker_chain}', but this disc carries '{chain_name}'."
                    )
                    log.info("Choose an empty -o directory for this chain.")
                    sys.exit(1)
                (output_dir / EXTRACT_MARKER_NAME).write_text(chain_name + "\n")
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
            source.close_current()

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
            mounted = source.reopen_current(disc_num)
            if mounted is None:
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
                source.close_current()
            _cleanup_par2(staging)

        # Report current chain collection state.
        gens_collected = sorted(gen_basenames)
        log.info(f"Chain so far: Gen {gens_collected} ({disc_num} disc(s) total)")

    if chain_name is None:
        log.error(f"No {source.item_label.lower()}s processed")
        sys.exit(1)

    # ── Extract: one dar -x per generation in order ──────────────────────
    log.step("Extracting archive chain")
    sorted_gens = sorted(gen_basenames)
    log.info(f"Chain: {chain_name}")
    log.info(f"Generations: {sorted_gens}")

    # An incomplete chain restores incomplete data: a missing earlier
    # generation means its file contents are absent, and deletions/
    # renames recorded in a skipped generation are silently lost. Warn
    # and let the user decide — they may only have partial media left.
    missing_gens = sorted(set(range(1, sorted_gens[-1] + 1)) - set(sorted_gens))
    if missing_gens:
        log.warn(
            f"Generation(s) {missing_gens} of this chain were not collected. "
            f"Files saved only in those generations will be missing, and "
            f"deletions/renames they recorded will not be applied."
        )
        if not prompt_yn("Extract the incomplete chain anyway?", default_yes=False):
            log.info(f"Slices remain in: {staging}")
            log.info("Re-run extract and insert the missing generation's discs as well.")
            sys.exit(1)

    all_corrupted: list[str] = []
    for gen in sorted_gens:
        basename = gen_basenames[gen]
        log.info(f"Gen {gen}: dar -x {basename}")
        catalog_basename = f"{basename}-catalog"
        has_catalog = any(staging.glob(f"{catalog_basename}.*.dar"))
        # Always overwrite (-wa): later generations carry newer file
        # contents than earlier ones, and a re-run into a non-empty
        # output dir (the documented repair path after corruption, or a
        # resume after a crash) must replace existing — possibly stale
        # or truncated — files. Without -wa, dar's overwrite prompt gets
        # auto-answered negatively on our piped stdin and silently keeps
        # the old bytes.
        rc, corrupted = dar.extract_sequential(
            staging / basename,
            output_dir,
            catalog_base=staging / catalog_basename if has_catalog else None,
            overwrite=True,
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
    print(f"  {source.item_label + 's:':<14}{disc_num}")
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
