import subprocess
import threading
from pathlib import Path

from bd_archive.shell.runner import run


def create_sliced(base_path: Path, source: Path, slice_bytes: int,
                  compression: str, comp_level: str | None,
                  execute_hook: str | None = None):
    """Create a sliced dar archive with sha512 hashes.

    If execute_hook is set, dar invokes it via -E once each slice has
    been completed (verified against dar 2.7.17). This is used by
    cmd_create to run par2 on each slice while its bytes are still in
    the OS page cache.
    """
    cmd = ["dar", "-c", str(base_path),
           "-R", str(source), "-s", str(slice_bytes),
           "--hash", "sha512", "--min-digits", "4", "-Q"]
    if compression != "none":
        flag = f"-z{compression}"
        if comp_level:
            flag += f":{comp_level}"
        cmd += [flag, "-am"]
    if execute_hook is not None:
        cmd += ["-E", execute_hook]
    run(cmd, label="dar")


def isolate_catalog(base_path: Path):
    """Isolate the catalog into a separate dar archive with sha512 hashes."""
    run(["dar", "-C", str(base_path) + "-catalog",
         "-A", str(base_path),
         "--hash", "sha512", "--min-digits", "4", "-Q"],
        label="dar", check=True)


def compress(archive_path: Path, source: Path,
             compression: str, comp_level: str | None):
    """Create an unsliced dar archive (used for compression-ratio sampling)."""
    cmd = ["dar", "-c", str(archive_path), "-R", str(source), "-Q"]
    if compression != "none":
        flag = f"-z{compression}"
        if comp_level:
            flag += f":{comp_level}"
        cmd += [flag, "-am"]
    run(cmd, label="dar")


def extract_sequential(base_path: Path, output_dir: Path,
                       catalog_base: Path | None = None,
                       execute_hook: str | None = None) -> int:
    """Extract a dar archive with --sequential-read.

    When execute_hook is set, dar receives it via -E. dar fires the
    hook before opening each slice (per dar 2.7 manpage: "-E ... is
    executed before the slice is read or even asked"), which the
    caller uses to mount the next disc and symlink the expected
    slice path. The ESC-skip feeder is disabled in this mode —
    the hook pre-stages every slice, so any prompt would indicate
    a real error.

    When execute_hook is None, the legacy ESC-feeding behaviour is
    used: dar's missing-slice prompts auto-skip so a partial disc
    set still restores ~95% of files without user intervention.

    Returns dar's exit code.
    """
    cmd = ["dar", "-x", str(base_path), "-R", str(output_dir),
           "-O", "--sequential-read"]
    if catalog_base is not None:
        # -A uses the isolated catalog as rescue source — handles
        # corruption of the in-archive catalog (PAR2 covers slice
        # bytes but the embedded catalog inside the slice can still
        # be lost past PAR2's repair threshold).
        cmd += ["-A", str(catalog_base)]
    if execute_hook is not None:
        cmd += ["-E", execute_hook]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    assert proc.stdin is not None and proc.stdout is not None

    if execute_hook is None:
        def _feed_esc():
            try:
                while True:
                    proc.stdin.write("\x1b")
                    proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass
        threading.Thread(target=_feed_esc, daemon=True).start()

    for line in proc.stdout:
        print(f"  [dar] {line}", end="")
    proc.wait()
    return proc.returncode
