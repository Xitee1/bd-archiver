import contextlib
import json
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from bd_archive.archive.disc import DiscIO
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools import dar, par2
from bd_archive.tools.par2 import is_par2_index
from bd_archive.ui.logger import log
from bd_archive.ui.prompts import prompt_disc, prompt_yn


def _slice_name(archive_name: str, slice_num: int) -> str:
    return f"{archive_name}.{slice_num:04d}.dar"


def _release(dio: DiscIO, state: dict, staging: Path, archive_name: str):
    """Mirror of _swap_helper._release; used by main process for the
    pre-dar-launch path and post-dar cleanup."""
    mount_path = state.get("current_mount_path")
    if not mount_path:
        return
    slice_num = state.get("current_slice")
    if slice_num:
        link = staging / _slice_name(archive_name, slice_num)
        if link.is_symlink() or link.exists():
            link.unlink()
    dio.umount(Path(mount_path))
    md = state.get("current_mount_dir")
    if md:
        with contextlib.suppress(OSError):
            Path(md).rmdir()
    dio.eject()
    state["current_mount_path"] = None
    state["current_mount_dir"] = None
    state["current_slice"] = None


def cmd_extract(args):
    check_deps("dar", "par2")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    work_dir = Path(args.workdir) if args.workdir else Path(
        tempfile.mkdtemp(prefix="bd-extract-"))
    staging = work_dir / "slices"
    staging.mkdir(parents=True, exist_ok=True)

    dio = DiscIO(args.device)

    log.step("Restore archive from discs (read-once)")
    log.info(f"Device:   {args.device}")
    log.info(f"Output:   {output_dir}")
    log.info(f"Staging:  {staging}")

    # ── Disc 1 bootstrap: archive-name detection + catalog copy ─────
    archive_name: str | None = None
    first_dir: Path | None = None
    first_path: Path | None = None
    while True:
        prompt_disc("Insert disc 1", args.device)
        first_dir = Path(tempfile.mkdtemp(prefix="bd-mount-"))
        first_path = dio.mount(first_dir)
        if first_path is None:
            log.error("Could not mount disc")
            with contextlib.suppress(OSError):
                first_dir.rmdir()
            if not prompt_yn("Retry?"):
                sys.exit(1)
            continue

        dar_files = [p for p in first_path.glob("*.dar")
                     if "-catalog" not in p.name]
        if not dar_files:
            log.error("No dar slices on this disc — insert disc 1")
            dio.umount(first_path)
            with contextlib.suppress(OSError):
                first_dir.rmdir()
            dio.eject()
            continue

        archive_name = dar_files[0].stem.rsplit(".", 1)[0]
        break

    assert archive_name is not None and first_path is not None
    log.info(f"Archive detected: {archive_name}")

    # Catalog files (small, ~MB) get copied so they survive disc swaps.
    cat_count = 0
    for cat in first_path.glob(f"{archive_name}-catalog.*"):
        dest = staging / cat.name
        if not dest.exists():
            shutil.copy2(cat, dest)
            cat_count += 1
    log.info(f"Catalog: {cat_count} file(s) copied")

    # Symlink slice 1 in place so dar finds it on its first open.
    slice1 = first_path / _slice_name(archive_name, 1)
    if not slice1.exists():
        log.error(f"Slice 1 ({slice1.name}) missing on disc 1")
        sys.exit(1)
    slice1_link = staging / slice1.name
    if slice1_link.is_symlink() or slice1_link.exists():
        slice1_link.unlink()
    slice1_link.symlink_to(slice1)

    # State file shared with the swap helper.
    state_file = work_dir / ".swap_state.json"
    state = {
        "device": args.device,
        "archive_name": archive_name,
        "staging_dir": str(staging),
        "current_mount_dir": str(first_dir),
        "current_mount_path": str(first_path),
        "current_slice": 1,
    }
    state_file.write_text(json.dumps(state))

    # ── Run dar with the swap helper as -E ──────────────────────────
    log.step("Extracting archive")
    log.info("(disc swaps happen between slices, driven by dar -E)")

    helper_cmd = (
        f"{shlex.quote(sys.executable)} -m bd_archive._swap_helper "
        f"{shlex.quote(str(state_file))} %N %c"
    )

    dar_base = staging / archive_name
    catalog_base = staging / f"{archive_name}-catalog"
    has_catalog = any(staging.glob(f"{archive_name}-catalog.*.dar"))

    rc = dar.extract_sequential(
        dar_base, output_dir,
        catalog_base=catalog_base if has_catalog else None,
        execute_hook=helper_cmd,
    )

    # Reload state — helper may have advanced through several discs.
    state = json.loads(state_file.read_text())

    # ── Cleanup: release whichever disc is still mounted ────────────
    _release(dio, state, staging, archive_name)
    state_file.write_text(json.dumps(state))

    if rc == 0:
        total = sum(f.stat().st_size for f in output_dir.rglob("*")
                    if f.is_file())
        log.step("Restore complete")
        print(f"\n  Archive: {archive_name}")
        print(f"  Output:  {output_dir}")
        print(f"  Size:    {human_bytes(total)}")
        print(f"\n  Cleanup staging: rm -rf {work_dir}\n")
        return

    # ── dar failed — likely slice CRC mismatch on the last-touched disc.
    last_slice = _last_known_slice(state_file)
    log.error(f"dar exited with code {rc}")
    if last_slice:
        log.info(f"The disc most recently being read was disc {last_slice}.")
    log.info("If the disc has bit rot we can run par2 repair on the bad "
             "slice now; re-running `bd-archive extract` afterwards will "
             "pick up the repaired file from staging instead of the disc.")
    if not prompt_yn("Run par2 repair?", default_yes=True):
        sys.exit(rc)
    if last_slice is None:
        log.error("Unknown disc — cannot offer automated repair")
        sys.exit(rc)

    _par2_repair_slice(dio, state, staging, archive_name, last_slice,
                       state_file, args, work_dir, output_dir)


def _last_known_slice(state_file: Path) -> int | None:
    """Read state file ignoring any 'released' nulls — for the
    post-dar-failure path where the helper may have nulled the
    current_slice during cleanup."""
    try:
        state = json.loads(state_file.read_text())
        return state.get("current_slice")
    except (OSError, json.JSONDecodeError):
        return None


def _par2_repair_slice(dio: DiscIO, state: dict, staging: Path,
                       archive_name: str, slice_num: int,
                       state_file: Path, args, work_dir: Path,
                       output_dir: Path):
    """Mount disc <slice_num>, copy slice + par2 sidecars into staging,
    run par2 repair, leave a real file (not a symlink) at the staging
    slot so a re-run of `bd-archive extract` consumes the repaired
    bytes instead of the disc."""
    while True:
        prompt_disc(f"Insert disc {slice_num} for repair", args.device)
        md = Path(tempfile.mkdtemp(prefix="bd-mount-"))
        mp = dio.mount(md)
        if mp is None:
            log.error("Could not mount disc")
            with contextlib.suppress(OSError):
                md.rmdir()
            if not prompt_yn("Retry?"):
                sys.exit(1)
            continue
        if not (mp / _slice_name(archive_name, slice_num)).exists():
            log.error(f"Slice {slice_num} not on this disc")
            dio.umount(mp)
            with contextlib.suppress(OSError):
                md.rmdir()
            dio.eject()
            if not prompt_yn("Try another disc?"):
                sys.exit(1)
            continue
        state["current_mount_dir"] = str(md)
        state["current_mount_path"] = str(mp)
        state["current_slice"] = slice_num
        state_file.write_text(json.dumps(state))
        break

    target_name = _slice_name(archive_name, slice_num)
    target = mp / target_name

    # Drop any existing symlink and replace with a real copy that par2
    # can rewrite in place.
    dest = staging / target_name
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    log.info(f"Copying {target_name} to staging for repair...")
    shutil.copy2(target, dest)
    for pf in mp.glob(f"{target_name}.*par2"):
        pf_dest = staging / pf.name
        if not pf_dest.exists():
            shutil.copy2(pf, pf_dest)

    par2_idx_list = [p for p in staging.glob(f"{target_name}.*par2")
                     if is_par2_index(p)]
    if not par2_idx_list:
        log.error("No par2 index found in staging — cannot repair")
        _release(dio, state, staging, archive_name)
        state_file.write_text(json.dumps(state))
        sys.exit(2)

    log.info("Running par2 repair...")
    if not par2.repair(par2_idx_list[0]):
        log.error("par2 repair failed — slice is unrecoverable")
        _release(dio, state, staging, archive_name)
        state_file.write_text(json.dumps(state))
        sys.exit(2)

    log.ok(f"Slice {slice_num} repaired in staging.")
    _release(dio, state, staging, archive_name)
    state_file.write_text(json.dumps(state))
    log.info(f"Re-run: bd-archive extract -o {output_dir} "
             f"-D {args.device} -w {work_dir}")
