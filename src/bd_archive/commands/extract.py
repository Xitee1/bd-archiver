import shutil
import sys
import tempfile
from pathlib import Path

from bd_archive.archive.disc import DiscIO
from bd_archive.archive.verify import verify_disc
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools import dar, par2
from bd_archive.tools.par2 import VerifyResult, is_par2_index
from bd_archive.ui.logger import log
from bd_archive.ui.prompts import prompt_disc, prompt_yn


def cmd_extract(args):
    check_deps("dar", "par2")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    work_dir = Path(args.workdir) if args.workdir else Path(
        tempfile.mkdtemp(prefix="bd-extract-"))
    staging = work_dir / "slices"
    staging.mkdir(parents=True, exist_ok=True)

    dio = DiscIO(args.device)

    log.step("Restore archive from discs")
    log.info(f"Device:   {args.device}")
    log.info(f"Output:   {output_dir}")
    log.info(f"Staging:  {staging}")

    archive_name = None
    disc_num = 0

    while True:
        target = disc_num + 1

        # Mount retry loop — keeps prompting until mount succeeds or user gives up.
        mount_dir = Path(tempfile.mkdtemp(prefix="bd-mount-"))
        mounted: Path | None
        while True:
            prompt_disc(f"Insert disc {target}", args.device)
            mounted = dio.mount(mount_dir)
            if mounted is not None:
                break
            log.error("Could not mount disc")
            if not prompt_yn("Retry?"):
                mount_dir.rmdir()
                sys.exit(1)

        try:
            # Detect archive name on the first disc that has dar files.
            # If the user inserted the wrong disc, retry without consuming
            # the slot.
            if archive_name is None:
                dar_files = [p for p in mounted.glob("*.dar")
                             if "-catalog" not in p.name]
                if not dar_files:
                    log.error("No dar files found on disc — try another")
                    continue
                archive_name = dar_files[0].stem.rsplit(".", 1)[0]
                log.info(f"Archive detected: {archive_name}")

            disc_num = target

            # Copy catalog
            for cat in mounted.glob(f"{archive_name}-catalog.*.dar"):
                dest = staging / cat.name
                if not dest.exists():
                    shutil.copy2(cat, dest)

            # Verify
            log.info(f"Checking disc {disc_num}...")
            result = verify_disc(mounted, f"Disc {disc_num}", quiet=True)

            # Copy slices
            slices = sorted(mounted.glob(f"{archive_name}.[0-9]*.dar"))
            slices = [s for s in slices if "-catalog" not in s.name]

            for sp in slices:
                dest = staging / sp.name
                if dest.exists():
                    log.info(f"{sp.name} already present — skipping")
                    continue

                if result == VerifyResult.REPAIRABLE:
                    log.warn(f"{sp.name}: damage detected — repairing...")
                    repair_dir = work_dir / f"repair_{disc_num}"
                    repair_dir.mkdir(exist_ok=True)
                    try:
                        shutil.copy2(sp, repair_dir)
                        for pf in mounted.glob(f"{sp.name}.*par2"):
                            shutil.copy2(pf, repair_dir)

                        par2_idx = [p for p in repair_dir.glob("*.par2")
                                    if is_par2_index(p)]
                        if par2_idx and par2.repair(par2_idx[0]):
                            shutil.copy2(repair_dir / sp.name, dest)
                            log.ok(f"{sp.name}: repaired successfully")
                        else:
                            log.error(f"{sp.name}: repair failed!")
                            if prompt_yn("Use anyway?", default_yes=False):
                                shutil.copy2(repair_dir / sp.name, dest)
                    finally:
                        shutil.rmtree(repair_dir, ignore_errors=True)

                elif result == VerifyResult.BROKEN:
                    log.error(f"{sp.name}: unrepairable damage!")
                    if prompt_yn("Copy anyway?", default_yes=False):
                        shutil.copy2(sp, dest)

                else:
                    shutil.copy2(sp, dest)
                    log.ok(f"{sp.name} copied")

        finally:
            dio.umount(mounted)
            try:
                mount_dir.rmdir()
            except OSError:
                pass
            dio.eject()

        collected = sorted(staging.glob(f"{archive_name}.[0-9]*.dar"))
        collected = [c for c in collected if "-catalog" not in c.name]
        log.info(f"Collected: {len(collected)} slice(s)")

        if not prompt_yn("Insert another disc?"):
            break

    # ── Extract ─────────────────────────────────────────────────────────
    log.step("Extracting archive")
    collected = [c for c in sorted(staging.glob(f"{archive_name}.[0-9]*.dar"))
                 if "-catalog" not in c.name]
    log.info(f"Slices: {len(collected)}")
    log.info(f"Output: {output_dir}")

    dar_base = staging / archive_name
    catalog_base = staging / f"{archive_name}-catalog"
    has_catalog = any(staging.glob(f"{archive_name}-catalog.*.dar"))

    rc = dar.extract_sequential(
        dar_base, output_dir,
        catalog_base=catalog_base if has_catalog else None,
    )

    if rc == 0:
        log.ok("Extraction complete!")
    else:
        log.error(f"dar extraction failed (exit {rc})")
        log.info(f"Slices are in: {staging}")
        if has_catalog:
            log.info(f"Retry without rescue catalog: "
                     f"dar -x {dar_base} -R {output_dir} --sequential-read")
        else:
            log.info(f"Manual: dar -x {dar_base} -R {output_dir} --sequential-read")
        sys.exit(1)

    total = sum(f.stat().st_size for f in output_dir.rglob("*") if f.is_file())
    log.step("Restore complete")
    print(f"\n  Archive: {archive_name}")
    print(f"  Slices:  {len(collected)}")
    print(f"  Discs:   {disc_num}")
    print(f"  Output:  {output_dir}")
    print(f"  Size:    {human_bytes(total)}")
    print(f"\n  Cleanup staging: rm -rf {work_dir}\n")
