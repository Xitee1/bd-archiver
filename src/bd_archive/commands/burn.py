import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path

from bd_archive.archive.disc import (
    DiscIO,
    drives_with_label,
    find_sg_device,
    iso_volume_label,
    wait_for_labelled_disc,
)
from bd_archive.archive.disc_folder import DiscFolder, load_disc_set
from bd_archive.archive.sizing import disc_write_bytes
from bd_archive.archive.verify import verify_disc
from bd_archive.constants import DISC_OVERSIZE_TOLERANCE
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools import udev
from bd_archive.tools.growisofs import DeviceBusyError
from bd_archive.tools.lsof import find_device_holders
from bd_archive.tools.mediainfo import detect_disc_capacity
from bd_archive.tools.optical import list_drives, resolve_device
from bd_archive.tools.par2 import VerifyResult
from bd_archive.ui.logger import log
from bd_archive.ui.prompts import prompt_disc, styled_input

VERIFY_DEVICE_AUTO = "auto"


def cmd_burn(args):
    check_deps("growisofs", "dvd+rw-mediainfo")

    input_dir = Path(args.input)
    images_dir = input_dir / "images"

    isos = sorted(images_dir.glob("disc_*.iso"))
    discs_dir = input_dir / "discs"
    folders = []
    if discs_dir.exists() and not discs_dir.is_dir():
        raise ValueError(f"Disc output path is not a directory: {discs_dir}")
    if discs_dir.exists() and any(discs_dir.iterdir()):
        if isos:
            log.error("Output contains both disc folders and ISOs; choose an unambiguous set")
            sys.exit(1)
        check_deps("mkisofs")
        try:
            folders = load_disc_set(discs_dir)
        except ValueError as exc:
            log.error(str(exc))
            sys.exit(1)
    discs = folders or isos
    disc_count = len(discs)
    if disc_count == 0:
        log.error(f"No prepared disc folders or ISO images in {input_dir}")
        log.info("Run 'create' first to prepare the discs.")
        sys.exit(1)

    start = args.start
    if start < 1 or start > disc_count:
        log.error(f"--start must be between 1 and {disc_count}")
        sys.exit(1)

    if args.no_verify and (args.verify_device is not None or args.no_close_tray):
        log.error("--verify-device and --no-close-tray require post-burn verification")
        sys.exit(1)
    if args.verify_device == VERIFY_DEVICE_AUTO and not udev.is_available():
        log.error(f"--verify-device auto requires the udev database ({udev.UDEV_DATA_DIR})")
        log.info("Name the verify drive instead, e.g. --verify-device /dev/sr1.")
        sys.exit(1)

    device = resolve_device(args.device)
    dio = DiscIO(device)
    verify_device = _resolve_verify_device(args.verify_device, device)

    # The set's largest ISO defines the capacity class the whole set was
    # sized for — the oversize fit check compares against it, so a
    # half-full last disc doesn't get refused on the same media as the
    # (full) discs before it. A single image may be intentionally partial
    # (especially in raw mode), so its size cannot identify a media class.
    try:
        max_iso_bytes = max(
            disc_write_bytes(
                disc.measure() if isinstance(disc, DiscFolder) else disc.stat().st_size
            )
            for disc in discs
        )
    except ValueError as exc:
        log.error(str(exc))
        sys.exit(1)

    log.step("Burn prepared discs")
    log.info(f"Discs:    {disc_count}")
    log.info(f"Device:   {device}")
    if args.no_verify:
        log.info("Verify:   disabled")
    elif verify_device == VERIFY_DEVICE_AUTO:
        log.info("Verify:   any drive, detected by volume label")
    else:
        log.info(f"Verify:   {verify_device or device}")
    if start > 1:
        log.info(f"Resuming from disc {start}")

    for i in range(start, disc_count + 1):
        iso = discs[i - 1]
        if not folders and iso != images_dir / f"disc_{i:04d}.iso":
            log.error(f"ISO not found: {iso}")
            log.info("Run 'create' first to build the disc images.")
            sys.exit(1)

        try:
            _burn_one_disc(args, input_dir, iso, i, disc_count, dio, max_iso_bytes, verify_device)
        except ValueError as exc:
            log.error(str(exc))
            log.info(f"Resume later with: bd-archive burn -i {input_dir} --start {i}")
            sys.exit(1)
        except KeyboardInterrupt:
            # Top-level handler will print the cancel banner + exit 130.
            # Print the resume hint here so the user sees exactly which
            # disc to resume from, without having to count by hand.
            log.info(f"Resume later with: bd-archive burn -i {input_dir} --start {i}")
            raise

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


def _same_device(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _resolve_verify_device(value: str | None, burn_device: str) -> str | None:
    """Return None to verify in the burner, VERIFY_DEVICE_AUTO, or the
    path of a separate verify drive."""
    if value is None or value == VERIFY_DEVICE_AUTO:
        return value
    verify_device = resolve_device(value)
    return None if _same_device(verify_device, burn_device) else verify_device


def _auto_verify_devices(burn_device: str) -> list[str]:
    others = [d.path for d in list_drives() if not _same_device(d.path, burn_device)]
    return [burn_device, *others]


def _burn_one_disc(
    args,
    input_dir: Path,
    iso: Path | DiscFolder,
    i: int,
    disc_count: int,
    dio: DiscIO,
    max_iso_bytes: int,
    verify_device: str | None = None,
):
    log.step(f"Disc {i}/{disc_count}")
    folder = iso if isinstance(iso, DiscFolder) else None
    if folder is not None:
        log.info(f"Folder: {folder.root}")
        log.info("Keep the prepared disc folder unchanged until burning finishes.")
    else:
        log.info(f"ISO: {iso.name} ({human_bytes(iso.stat().st_size)})")

    prompt_disc(f"Insert blank disc {i}/{disc_count}", dio.device)
    # Measure after the potentially long insertion prompt. Folder burns use
    # the identical mkisofs options, without writing an intermediate ISO.
    iso_size = folder.measure() if folder is not None else iso.stat().st_size
    write_bytes = disc_write_bytes(iso_size)

    # growisofs pads both input paths to full 32-KiB write blocks.
    # Include that padding in the hard fit gate. detect_disc_capacity returns the
    # writable extent. The too-small check is per-ISO; the oversize
    # check compares against the largest ISO of the set (a partially
    # filled last disc is normal — only a wrong media class is not).
    # Single-image sets permit unused space, retaining the hard lower bound.
    if not args.skip_fit_check:
        actual = detect_disc_capacity(dio.device)
        if actual is None:
            if folder is not None:
                raise ValueError(
                    "Could not detect disc capacity; retry or explicitly use --skip-fit-check"
                )
            log.warn("Could not detect disc capacity — skipping fit check")
        elif actual < write_bytes:
            log.error(
                f"Disc too small: {actual} bytes available < {write_bytes} bytes required "
                "(including 32-KiB write padding)"
            )
            log.info(f"Resume later with: bd-archive burn -i {input_dir} --start {i}")
            sys.exit(1)
        elif disc_count > 1 and actual > max_iso_bytes * DISC_OVERSIZE_TOLERANCE:
            pct_over = int((DISC_OVERSIZE_TOLERANCE - 1) * 100)
            log.error(
                f"Disc too large: {human_bytes(actual)} exceeds the set's "
                f"largest image ({human_bytes(max_iso_bytes)}) by more than "
                f"{pct_over}% — refusing to waste space"
            )
            log.info("Insert a smaller disc, or pass --skip-fit-check to override.")
            log.info(f"Resume later with: bd-archive burn -i {input_dir} --start {i}")
            sys.exit(1)
        else:
            log.ok(
                f"Disc capacity {human_bytes(actual)} fits required write size "
                f"{human_bytes(write_bytes)} (including 32-KiB write padding)"
            )

    # Auto verify identifies the burned disc by its volume label. Drives
    # that already hold a disc with that label (e.g. an earlier attempt)
    # are snapshotted now, before the burn, and ignored until replaced.
    verify_label = None
    stale_drives: set[str] = set()
    if not args.no_verify and verify_device == VERIFY_DEVICE_AUTO:
        verify_label = folder.volume_label if folder is not None else iso_volume_label(iso)
        if verify_label is None:
            raise ValueError(f"Could not read the volume label of {iso}")
        stale_drives = drives_with_label(_auto_verify_devices(dio.device), verify_label)

    # Burn (with sg-busy retry)
    log.info("Burning...")
    while True:
        try:
            if folder is not None:
                folder.check_unchanged()
                dio.burn_folder(
                    folder.entries,
                    folder.volume_label,
                    folder.publisher,
                    args.speed,
                    rock_ridge=folder.rock_ridge,
                    write_timeout=args.write_timeout,
                    ring_buffer=args.buffer,
                )
                folder.check_unchanged()
            else:
                dio.burn(iso, args.speed, write_timeout=args.write_timeout, ring_buffer=args.buffer)
            break
        except DeviceBusyError:
            log.error(
                f"Optical device {dio.device} is locked by "
                f"another process (growisofs couldn't grab "
                f"the associated sg device)."
            )
            sg = find_sg_device(dio.device)
            holders = find_device_holders(dio.device, sg)
            if holders:
                log.info("Holding processes:")
                for h in holders:
                    log.info(f"  {h}")
            else:
                log.info("Common culprits: MakeMKV, K3b, Brasero, or a desktop auto-mount probe.")
            resp = styled_input("Close the program, then press Enter to retry (q = cancel): ")
            if resp.strip().lower() == "q":
                log.warn("Cancelled by user")
                log.info(f"Resume later with: bd-archive burn -i {input_dir} --start {i}")
                sys.exit(1)
    log.ok(f"Disc {i} burned")

    # Post-burn verify. growisofs auto-ejects the tray on finish (we no
    # longer pass `notray`), which is what generates the kernel
    # media-change event that makes mount actually see the new
    # filesystem. We then wait for the disc to be back in — either via
    # software close-tray (full-size drives) or the user pushing it in
    # (slim drives). The 10s pre-pause is on the long side, but slower
    # USB BD drives can take 5–8s just to extend the tray; closing it
    # before the eject motion finishes races the two motors and can
    # leave the drive in a confused state. A separate verify drive is
    # only polled: the burner's tray stays open for the user to move the
    # disc.
    verify_dio = dio
    if not args.no_verify:
        time.sleep(10)
        if verify_device is None:
            dio.wait_for_disc_ready(close_tray=not args.no_close_tray)
        elif verify_device == VERIFY_DEVICE_AUTO:
            found = wait_for_labelled_disc(
                _auto_verify_devices(dio.device),
                verify_label,
                stale_drives,
                close_device=None if args.no_close_tray else dio.device,
            )
            if not _same_device(found, dio.device):
                verify_dio = DiscIO(found)
        else:
            verify_dio = DiscIO(verify_device)
            verify_dio.wait_for_disc_ready(close_tray=False)

        log.info("Post-burn verification...")
        while True:
            mount_dir = Path(tempfile.mkdtemp(prefix="bd-verify-"))
            verify_ok = False
            try:
                mounted, mount_err = verify_dio.mount_with_retry(mount_dir)
                if mounted is None:
                    log.error("Could not mount disc for verification")
                    if mount_err:
                        log.error(f"  {mount_err}")
                else:
                    try:
                        result = verify_disc(mounted, f"Disc {i} (post-burn)", quiet=True)
                        # A fresh burn must be flawless: REPAIRABLE means
                        # the disc already eats into its par2 margin on
                        # day one — treat it like a failed burn rather
                        # than shipping it to the shelf pre-damaged.
                        if result == VerifyResult.OK:
                            verify_ok = True
                        elif result == VerifyResult.REPAIRABLE:
                            log.error(
                                "Post-burn verification found repairable damage — "
                                "a just-burned disc should be flawless. Consider "
                                "re-burning this image on fresh media."
                            )
                        else:
                            log.error("Post-burn verification failed!")
                    finally:
                        verify_dio.umount(mounted)
            finally:
                with contextlib.suppress(OSError):
                    mount_dir.rmdir()
            if verify_ok:
                break
            resp = styled_input(
                "Re-insert the disc if needed, then press Enter "
                "to retry verification (q = cancel): "
            )
            if resp.strip().lower() == "q":
                log.warn("Cancelled by user")
                log.info(f"Resume later with: bd-archive burn -i {input_dir} --start {i}")
                sys.exit(1)

    verify_dio.eject()
    log.ok(f"Disc {i}/{disc_count} done")
