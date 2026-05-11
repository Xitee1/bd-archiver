"""dar -E hook for disc-swap during extract.

Invoked as: python3 -m bd_archive._swap_helper <state-file> <slice-num> <context>

- <state-file>: JSON file (read+written) holding mount state shared with
  the main `cmd_extract` process.
- <slice-num>: dar's %N substitution (zero-padded). "0" means "dar does
  not yet know the slice number" (catalog-probe path in non-sequential
  mode); we no-op so dar falls back to its own prompt.
- <context>: dar's %c — "init" or "operation" during restore.

The helper is idempotent. If the requested slice is already in place
(symlink with reachable target) it returns without prompting — this is
the normal init-context fire for slice 1, which the main process has
pre-staged.
"""
import contextlib
import json
import sys
import tempfile
from pathlib import Path

from bd_archive.archive.disc import DiscIO
from bd_archive.ui.logger import log
from bd_archive.ui.prompts import prompt_disc, prompt_yn


def _slice_name(archive_name: str, slice_num: int) -> str:
    return f"{archive_name}.{slice_num:04d}.dar"


def _release(dio: DiscIO, state: dict, staging: Path,
             archive_name: str):
    """Unmount + eject whatever disc state points at, drop its symlink."""
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


def _save(state_file: Path, state: dict):
    state_file.write_text(json.dumps(state))


def main():
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <state-file> <slice-num> <context>",
              file=sys.stderr)
        sys.exit(2)

    state_file = Path(sys.argv[1])
    slice_num = int(sys.argv[2])
    context = sys.argv[3]  # noqa: F841 — kept for log/debug; dar always passes it

    state = json.loads(state_file.read_text())
    archive_name = state["archive_name"]
    staging = Path(state["staging_dir"])
    device = state["device"]
    dio = DiscIO(device)

    # dar uses %n=0 to mean "I do not know the slice number yet" (it
    # wants to find the catalog in the last slice, non-sequential mode).
    # With --sequential-read this should not fire, but be defensive:
    # let dar fall back to its own prompt by returning without action.
    if slice_num == 0:
        log.warn("dar requested unknown slice (%n=0) — deferring to "
                 "dar's built-in prompt")
        return

    # Idempotency: if the slice is already wired up and reachable, we
    # are done. This fires on dar's init-context call for slice 1
    # because the main process pre-staged it.
    target_link = staging / _slice_name(archive_name, slice_num)
    if target_link.is_symlink():
        resolved = target_link.resolve()
        if resolved.exists():
            return
        # Symlink dangles (mount has gone away under us); fall through
        # to a swap.

    # Perform the swap.
    _release(dio, state, staging, archive_name)
    _save(state_file, state)

    while True:
        prompt_disc(f"Insert disc {slice_num}", device)
        new_dir = Path(tempfile.mkdtemp(prefix="bd-mount-"))
        new_path = dio.mount(new_dir)
        if new_path is None:
            log.error("Could not mount disc")
            with contextlib.suppress(OSError):
                new_dir.rmdir()
            if not prompt_yn("Retry?"):
                sys.exit(1)
            continue

        target = new_path / _slice_name(archive_name, slice_num)
        if not target.exists():
            log.error(f"Slice {slice_num} ({target.name}) not on this disc")
            dio.umount(new_path)
            with contextlib.suppress(OSError):
                new_dir.rmdir()
            dio.eject()
            if not prompt_yn("Try another disc?", default_yes=True):
                sys.exit(1)
            continue

        link = staging / target.name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
        state["current_slice"] = slice_num
        state["current_mount_dir"] = str(new_dir)
        state["current_mount_path"] = str(new_path)
        _save(state_file, state)
        return


if __name__ == "__main__":
    main()
