import contextlib
import sys
import tempfile
import time
from pathlib import Path

from bd_archive.archive.disc import DiscIO, find_sg_device
from bd_archive.archive.verify import verify_disc
from bd_archive.constants import DISC_OVERSIZE_TOLERANCE
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools.growisofs import DeviceBusyError
from bd_archive.tools.lsof import find_device_holders
from bd_archive.tools.mediainfo import detect_disc_capacity
from bd_archive.tools.par2 import VerifyResult
from bd_archive.ui.logger import log
from bd_archive.ui.prompts import prompt_disc


def cmd_burn(args):
    check_deps("growisofs", "dvd+rw-mediainfo")

    input_dir = Path(args.input)
    images_dir = input_dir / "images"

    if not images_dir.is_dir():
        log.error(f"No images directory at {images_dir}")
        log.info("Run 'create' first to build the disc images.")
        sys.exit(1)

    isos = sorted(images_dir.glob("disc_*.iso"))
    disc_count = len(isos)
    if disc_count == 0:
        log.error(f"No disc_*.iso files in {images_dir}")
        log.info("Run 'create' first to build the disc images.")
        sys.exit(1)

    start = args.start
    if start < 1 or start > disc_count:
        log.error(f"--start must be between 1 and {disc_count}")
        sys.exit(1)

    dio = DiscIO(args.device)

    log.step("Burn disc images")
    log.info(f"Discs:    {disc_count}")
    log.info(f"Device:   {args.device}")
    if start > 1:
        log.info(f"Resuming from disc {start}")

    for i in range(start, disc_count + 1):
        iso = images_dir / f"disc_{i:04d}.iso"
        if not iso.exists():
            log.error(f"ISO not found: {iso}")
            log.info("Run 'create' first to build the disc images.")
            sys.exit(1)

        log.step(f"Disc {i}/{disc_count}")
        iso_size = iso.stat().st_size
        log.info(f"ISO: {iso.name} ({human_bytes(iso_size)})")

        prompt_disc(f"Insert blank disc {i}/{disc_count}", args.device)

        # Pre-burn fit check — iso_size is the exact byte count growisofs
        # will write. detect_disc_capacity returns the format-aware
        # writable extent.
        if not args.skip_fit_check:
            actual = detect_disc_capacity(args.device)
            if actual is None:
                log.warn("Could not detect disc capacity — skipping fit check")
            elif actual < iso_size:
                log.error(f"Disc too small: {human_bytes(actual)} < ISO {human_bytes(iso_size)}")
                log.info(f"Resume later with: bd-archive burn -i {input_dir} --start {i}")
                sys.exit(1)
            elif actual > iso_size * DISC_OVERSIZE_TOLERANCE:
                pct_over = int((DISC_OVERSIZE_TOLERANCE - 1) * 100)
                log.error(
                    f"Disc too large: {human_bytes(actual)} > "
                    f"{human_bytes(iso_size)} + {pct_over}% — refusing "
                    f"to waste space"
                )
                log.info("Insert a smaller disc, or pass --skip-fit-check to override.")
                log.info(f"Resume later with: bd-archive burn -i {input_dir} --start {i}")
                sys.exit(1)
            else:
                log.ok(f"Disc capacity {human_bytes(actual)} fits ISO {human_bytes(iso_size)}")

        # Burn (with sg-busy retry)
        log.info("Burning...")
        while True:
            try:
                dio.burn(iso, args.speed)
                break
            except DeviceBusyError:
                log.error(
                    f"Optical device {args.device} is locked by "
                    f"another process (growisofs couldn't grab "
                    f"the associated sg device)."
                )
                sg = find_sg_device(args.device)
                holders = find_device_holders(args.device, sg)
                if holders:
                    log.info("Holding processes:")
                    for h in holders:
                        log.info(f"  {h}")
                else:
                    log.info(
                        "Common culprits: MakeMKV, K3b, Brasero, or a desktop auto-mount probe."
                    )
                resp = input(
                    "\033[1;33mClose the program, then press Enter to retry (q = cancel): \033[0m"
                )
                if resp.strip().lower() == "q":
                    log.warn("Cancelled by user")
                    log.info(f"Resume later with: bd-archive burn -i {input_dir} --start {i}")
                    sys.exit(1)
        log.ok(f"Disc {i} burned")

        # Give the drive a moment to settle, then pull the tray back
        # in if it auto-ejected. Slot-load drives ignore close-tray.
        time.sleep(3)
        dio.close_tray_if_open()

        # Post-burn verify — retry on any failure (mount or verify)
        # until it succeeds, since some drives auto-eject after burn
        # and the user needs a chance to re-insert before we give up.
        if not args.no_verify:
            log.info("Post-burn verification...")
            while True:
                mount_dir = Path(tempfile.mkdtemp(prefix="bd-verify-"))
                verify_ok = False
                try:
                    mounted = dio.mount_with_retry(mount_dir)
                    if mounted is None:
                        log.error("Could not mount disc for verification")
                    else:
                        try:
                            result = verify_disc(mounted, f"Disc {i} (post-burn)", quiet=True)
                            if result == VerifyResult.BROKEN:
                                log.error("Post-burn verification failed!")
                            else:
                                verify_ok = True
                        finally:
                            dio.umount(mounted)
                finally:
                    with contextlib.suppress(OSError):
                        mount_dir.rmdir()
                if verify_ok:
                    break
                resp = input(
                    "\033[1;33mRe-insert the disc if needed, then press Enter "
                    "to retry verification (q = cancel): \033[0m"
                )
                if resp.strip().lower() == "q":
                    log.warn("Cancelled by user")
                    log.info(f"Resume later with: bd-archive burn -i {input_dir} --start {i}")
                    sys.exit(1)

        dio.eject()
        log.ok(f"Disc {i}/{disc_count} done")

        if i < disc_count:
            remaining = disc_count - i
            log.info(
                f"{remaining} disc(s) remaining. "
                f"Resume: bd-archive burn -i {input_dir} "
                f"--start {i + 1}"
            )

    log.step("All discs burned")
    print(f"\n  Discs:    {disc_count}")
    print(f"  Cleanup:  rm -rf {input_dir}\n")
