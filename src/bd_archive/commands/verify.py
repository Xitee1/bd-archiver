import contextlib
import sys
import tempfile
from pathlib import Path

from bd_archive.archive.disc import DiscIO, LoopMountError, loop_mounted
from bd_archive.archive.verify import verify_disc
from bd_archive.shell.deps import check_deps
from bd_archive.tools.optical import resolve_device
from bd_archive.ui.logger import log


def cmd_verify(args):
    target = Path(resolve_device(None)) if args.target is None else Path(args.target)

    if target.is_file() and target.suffix.lower() == ".iso":
        # Loop-mount the ISO via udisksctl (no privileges needed),
        # run the same verify_disc on the mount, then tear down.
        # Lets users verify pre-built images before burning.
        check_deps("udisksctl")
        try:
            with loop_mounted(target, prefix="bd-verify-") as mounted:
                result = verify_disc(mounted, f"ISO {target.name}")
        except LoopMountError as e:
            log.error(str(e))
            sys.exit(1)
        sys.exit(result.value)

    elif target.is_block_device():
        dio = DiscIO(str(target))
        mount_dir = Path(tempfile.mkdtemp(prefix="bd-verify-"))
        mounted, mount_err = dio.mount(mount_dir)
        if mounted is None:
            log.error(f"Could not mount {target}")
            if mount_err:
                log.error(f"  {mount_err}")
            mount_dir.rmdir()
            sys.exit(1)
        try:
            result = verify_disc(mounted, f"Disc at {target}")
        finally:
            dio.umount(mounted)
            with contextlib.suppress(OSError):
                mount_dir.rmdir()
        sys.exit(result.value)

    elif target.is_dir():
        result = verify_disc(target)
        sys.exit(result.value)

    else:
        log.error(f"Path does not exist: {target}")
        sys.exit(1)
