import re
import subprocess
import threading
from pathlib import Path

from bd_archive.shell.runner import run

# dar 2.7 prints "Error while restoring <path> : Bad CRC, data corruption
# occurred" when a file's per-file CRC fails during extract. dar logs this
# and **continues**, writing the partial/corrupt bytes to disk and exiting
# with code 0 anyway — so we cannot rely on the exit code to detect
# corruption. We parse the message instead.
_BAD_CRC_RE = re.compile(
    r"Error while restoring (.+?) : Bad CRC")


def create_sliced(base_path: Path, source: Path, slice_bytes: int,
                  compression: str, comp_level: str | None):
    """Create a sliced dar archive with sha512 hashes."""
    cmd = ["dar", "-c", str(base_path),
           "-R", str(source), "-s", str(slice_bytes),
           "--hash", "sha512", "--min-digits", "4", "-Q"]
    if compression != "none":
        flag = f"-z{compression}"
        if comp_level:
            flag += f":{comp_level}"
        cmd += [flag, "-am"]
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
                       ) -> tuple[int, list[str]]:
    """Extract a dar archive with --sequential-read.

    Feeds ESC bytes on stdin in a background thread so dar's
    "missing slice" prompts auto-skip — disaster recovery from a
    partial disc set restores ~95% of files without intervention.
    With a complete slice set, no prompts fire and the ESC stream
    goes unused.

    Returns (exit_code, corrupted_files). corrupted_files contains
    the paths dar reported as "Bad CRC" during extract — these
    files were (partially) written to output and need attention.
    dar 2.7 exits with code 0 even when CRC errors occurred, so
    the caller must check this list, not just the exit code.
    """
    cmd = ["dar", "-x", str(base_path), "-R", str(output_dir),
           "-O", "--sequential-read"]
    if catalog_base is not None:
        # -A uses the isolated catalog as rescue source — handles
        # corruption of the in-archive catalog (PAR2 covers slice
        # bytes but the embedded catalog inside the slice can still
        # be lost past PAR2's repair threshold).
        cmd += ["-A", str(catalog_base)]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    assert proc.stdin is not None and proc.stdout is not None

    def _feed_esc():
        try:
            while True:
                proc.stdin.write("\x1b")
                proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass

    threading.Thread(target=_feed_esc, daemon=True).start()
    corrupted: list[str] = []
    for line in proc.stdout:
        print(f"  [dar] {line}", end="")
        m = _BAD_CRC_RE.search(line)
        if m:
            corrupted.append(m.group(1).strip())
    proc.wait()
    return proc.returncode, corrupted
