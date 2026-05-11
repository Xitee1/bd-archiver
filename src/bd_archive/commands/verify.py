import contextlib
import sys
import tempfile
import time
from pathlib import Path

from bd_archive.archive.disc import DiscIO
from bd_archive.archive.verify import verify_disc
from bd_archive.shell.deps import check_deps
from bd_archive.tools import udisks
from bd_archive.tools.par2 import VerifyResult
from bd_archive.ui.logger import log


def cmd_verify(args):
    check_deps("par2")
    target = Path(args.target)

    if target.is_file() and target.suffix.lower() == ".iso":
        # Loop-mount the ISO via udisksctl (no privileges needed),
        # run the same verify_disc on the mount, then tear down.
        # Lets users verify pre-built images before burning.
        check_deps("udisksctl")
        ok, loop_dev, message = udisks.loop_setup(str(target.resolve()))
        if not ok:
            log.error(f"loop-setup failed: {message}")
            sys.exit(1)
        assert loop_dev is not None

        time.sleep(0.5)  # let udev settle so the loop device is ready
        dio = DiscIO(loop_dev)
        mount_dir = Path(tempfile.mkdtemp(prefix="bd-verify-"))
        result = VerifyResult.BROKEN
        try:
            mounted = dio.mount(mount_dir)
            if mounted is None:
                log.error(f"Could not mount {loop_dev}")
                sys.exit(1)
            try:
                result = verify_disc(mounted, f"ISO {target.name}")
            finally:
                dio.umount(mounted)
                with contextlib.suppress(OSError):
                    mount_dir.rmdir()
        finally:
            udisks.loop_delete(loop_dev)
        sys.exit(result.value)

    elif target.is_block_device():
        dio = DiscIO(str(target))
        mount_dir = Path(tempfile.mkdtemp(prefix="bd-verify-"))
        mounted = dio.mount(mount_dir)
        if mounted is None:
            log.error(f"Could not mount {target}")
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
