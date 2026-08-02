import contextlib
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
_BAD_CRC_RE = re.compile(r"Error while restoring (.+?) : Bad CRC")

# dar's -P masks are glob patterns, not literal paths: an unescaped
# "photos/[2024] trip/x.jpg" would exclude a *different* file matching
# the bracket expression and keep the intended one. Wrapping each glob
# metacharacter in [] (like Python's glob.escape) makes the mask match
# the path literally — verified against dar 2.7.
_GLOB_META_RE = re.compile(r"[*?\[]")


def _glob_escape(path: str) -> str:
    return _GLOB_META_RE.sub(lambda m: f"[{m.group(0)}]", path)


def create_sliced(
    base_path: Path,
    source: Path,
    slice_bytes: int,
    compression: str,
    comp_level: str | None,
    execute_hook: str | None = None,
    ref_catalog: Path | None = None,
    excludes: list[str] | None = None,
    first_slice_bytes: int | None = None,
):
    """Create a sliced dar archive with sha512 hashes.

    If execute_hook is set, dar invokes it via -E once each slice has
    been completed (verified against dar 2.7.17). This is used by
    cmd_create to run par2 on each slice while its bytes are still in
    the OS page cache.

    If ref_catalog is set, dar runs in incremental mode (`-A <ref>`):
    only files new or changed relative to that reference catalog are
    archived. Pass the basename of the catalog without the
    ``.NNNN.dar`` suffix (dar accepts the catalog basename and finds
    the slice files itself).

    If excludes is set, each entry is passed to dar as ``-P <path>``,
    excluding that exact relative subpath from the archive. Entries are
    treated as literal paths: glob metacharacters are escaped before
    being handed to dar (whose -P masks are glob patterns). Used by
    auto-defer to push specific files to a later generation.

    If first_slice_bytes is set and differs from slice_bytes, dar
    produces a first slice of that size and subsequent slices of
    slice_bytes (``-S <first> -s <rest>``). Used by --pack-with so the
    first slice fits the space a packed leftover ISO leaves on disc 1.
    """
    cmd = [
        "dar",
        "-c",
        str(base_path),
        "-R",
        str(source),
    ]
    if first_slice_bytes is not None and first_slice_bytes != slice_bytes:
        cmd += ["-S", str(first_slice_bytes)]
    cmd += [
        "-s",
        str(slice_bytes),
        "--hash",
        "sha512",
        "--min-digits",
        "4",
        "-Q",
    ]
    if compression != "none":
        flag = f"-z{compression}"
        if comp_level:
            flag += f":{comp_level}"
        cmd += [flag, "-am"]
    if ref_catalog is not None:
        cmd += ["-A", str(ref_catalog)]
    if excludes:
        for path in excludes:
            cmd += ["-P", _glob_escape(path)]
    if execute_hook is not None:
        cmd += ["-E", execute_hook]
    run(cmd, label="dar")


def list_catalog_paths(catalog_base: Path) -> set[str]:
    """Return the set of relative paths stored in a dar catalog.

    Runs ``dar -l <catalog_base>`` and parses the listing. dar's
    entry lines use tab separators between the user, group, size, date,
    and filename columns — the filename is always the last tab-separated
    field. Header and separator lines lack tabs entirely, so the
    "contains a tab" filter is sufficient to discard them.

    Deliberately no ``-as`` filter: an incremental (gen ≥ 2) catalog
    records unchanged files as unsaved reference entries, and ``-as``
    would hide those — making every file archived in an earlier
    generation look "new" to the delta preview and the auto-defer pool.
    Directories are included; the consumer treats the set as "anything
    dar already knows about", which keeps the filter conservative.
    """
    r = run(["dar", "-l", str(catalog_base), "-Q"], capture=True, check=True)
    paths: set[str] = set()
    for line in r.stdout.splitlines():
        if "\t" not in line:
            continue
        path = line.split("\t")[-1].rstrip()
        if path:
            paths.add(path)
    return paths


def isolate_catalog(base_path: Path):
    """Isolate the catalog into a separate dar archive with sha512 hashes."""
    run(
        [
            "dar",
            "-C",
            str(base_path) + "-catalog",
            "-A",
            str(base_path),
            "--hash",
            "sha512",
            "--min-digits",
            "4",
            "-Q",
        ],
        label="dar",
        check=True,
    )


def compress(archive_path: Path, source: Path, compression: str, comp_level: str | None):
    """Create an unsliced dar archive (used for compression-ratio sampling)."""
    cmd = ["dar", "-c", str(archive_path), "-R", str(source), "-Q"]
    if compression != "none":
        flag = f"-z{compression}"
        if comp_level:
            flag += f":{comp_level}"
        cmd += [flag, "-am"]
    run(cmd, label="dar")


def extract_sequential(
    base_path: Path,
    output_dir: Path,
    catalog_base: Path | None = None,
    overwrite: bool = False,
) -> tuple[int, list[str]]:
    """Extract a dar archive with --sequential-read.

    Feeds ESC bytes on stdin in a background thread so dar's
    "missing slice" prompts auto-skip — disaster recovery from a
    partial disc set restores ~95% of files without intervention.
    With a complete slice set, no prompts fire and the ESC stream
    goes unused.

    Set overwrite=True to make dar replace existing files without
    prompting (`-wa`). Required when extracting an incremental on
    top of a previously-extracted generation, where later gens
    update files that earlier gens already restored.

    Returns (exit_code, corrupted_files). corrupted_files contains
    the paths dar reported as "Bad CRC" during extract — these
    files were (partially) written to output and need attention.
    dar 2.7 exits with code 0 even when CRC errors occurred, so
    the caller must check this list, not just the exit code.
    """
    cmd = ["dar", "-x", str(base_path), "-R", str(output_dir), "-O", "--sequential-read"]
    if overwrite:
        cmd.append("-wa")
    if catalog_base is not None:
        # -A uses the isolated catalog as rescue source — handles
        # corruption of the in-archive catalog (PAR2 covers slice
        # bytes but the embedded catalog inside the slice can still
        # be lost past PAR2's repair threshold).
        cmd += ["-A", str(catalog_base)]

    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
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
    try:
        for line in proc.stdout:
            print(f"  [dar] {line}", end="")
            m = _BAD_CRC_RE.search(line)
            if m:
                corrupted.append(m.group(1).strip())
        proc.wait()
    except KeyboardInterrupt:
        # dar shares our process group → SIGINT already reached it.
        # Wait for it to die, then escalate if needed.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        raise
    finally:
        # dar has exited → its end of the stdin pipe is gone. The
        # ESC-feeder daemon thread may still hold a buffered write;
        # closing here triggers its except branch and lets us swallow
        # the BrokenPipeError instead of leaking it through the
        # TextIOWrapper finalizer at GC time.
        with contextlib.suppress(BrokenPipeError, OSError):
            proc.stdin.close()
    return proc.returncode, corrupted
