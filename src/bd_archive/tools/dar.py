import contextlib
import os
import re
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from bd_archive.shell.runner import run

# dar 2.7 prints "Error while restoring <path> : Bad CRC, data corruption
# occurred" when a file's per-file CRC fails during extract. dar logs this
# and **continues**, writing the partial/corrupt bytes to disk and exiting
# with code 0 anyway — so we cannot rely on the exit code to detect
# corruption. We parse the message instead.
_BAD_CRC_RE = re.compile(r"Error while restoring (.+?) : Bad CRC")

# Printed per file when dar wanted a slice we don't have and the missing
# slice question was answered "no" (see the no-terminal note on extract()).
# dar still creates the entry, so these paths exist in the output as
# 0-byte placeholders — the caller has to deal with them.
_SKIPPED_RE = re.compile(r"^(.+) not restored \(user choice\)$")

# A slice dar cannot do without: sequential read stops at the first hole
# (first pattern), random access needs the final slice because the slice
# layout lives in its trailer (second pattern). Either way dar aborts the
# run with exit code 4 after restoring whatever came before.
_MISSING_SLICE_RE = re.compile(r"User refused to continue while asking: (\S+) is required")
_MISSING_LAST_RE = re.compile(r"The last file of the set is not present")

# Stands in for the final slice, whose name dar does not print (it does
# not know it either — that is the point).
LAST_SLICE = "the last slice of the set"

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


def _kill_group(proc: subprocess.Popen, grace_s: int = 5) -> None:
    """Terminate a start_new_session child and everything it spawned.

    proc.terminate() would only reach the child itself; dar shells out
    (e.g. for -E hooks), so we signal the process group it leads. Falls
    back to the plain child signals if the group is already gone.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        try:
            proc.wait(timeout=grace_s)
            return
        except subprocess.TimeoutExpired:
            continue
    proc.wait()


@dataclass
class ExtractResult:
    """Outcome of one `dar -x` run.

    `returncode` alone is not enough to judge a restore: dar exits 0
    even when per-file CRC errors occurred, and exits 4 when it gave up
    on a missing slice after already restoring part of the archive.
    """

    returncode: int
    corrupted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    missing_slice: str | None = None


def extract(
    base_path: Path,
    output_dir: Path,
    catalog_base: Path | None = None,
    overwrite: bool = False,
    sequential: bool = True,
) -> ExtractResult:
    """Extract a dar archive, skipping whatever slices are absent.

    dar asks the user to provide missing slices. It reads that answer
    from `/dev/tty`, **not** from stdin, so piping anything into the
    child cannot answer it — with a terminal attached dar simply blocks
    forever. Running it in its own session (`start_new_session=True`)
    removes the controlling terminal, which puts dar in its documented
    "No terminal found for user interaction" mode: every question is
    answered negatively, i.e. missing slices are skipped instead of
    waited for. That is what makes a partial slice set restorable
    unattended.

    Side effect of that isolation: dar no longer receives the tty's
    SIGINT, so KeyboardInterrupt has to terminate it explicitly.

    `sequential` picks the read mode:

    * True (default) — `--sequential-read`, the tape-like mode that
      works without a usable catalog but must start at slice 1 and
      cannot skip a gap: refusing a missing slice aborts the run
      (exit 4, `missing_slice` set) with whatever came before restored.
    * False — random-access mode, which needs a catalog (`catalog_base`
      or the one at the end of the last slice) plus the archive's final
      slice, whose trailer holds the slice layout. In exchange it
      restores from any subset of the remaining slices, reporting each
      unreachable file in `skipped` (dar leaves those behind as 0-byte
      placeholders). Without the final slice dar refuses to open the
      archive at all and `missing_slice` comes back as LAST_SLICE.

    Set overwrite=True to make dar replace existing files without
    prompting (`-wa`). Required when extracting an incremental on
    top of a previously-extracted generation, where later gens
    update files that earlier gens already restored.
    """
    cmd = ["dar", "-x", str(base_path), "-R", str(output_dir), "-O"]
    if sequential:
        cmd.append("--sequential-read")
    if overwrite:
        cmd.append("-wa")
    if catalog_base is not None:
        # -A uses the isolated catalog as rescue source — handles
        # corruption of the in-archive catalog (PAR2 covers slice
        # bytes but the embedded catalog inside the slice can still
        # be lost past PAR2's repair threshold), and is what makes
        # random-access mode work on a partial slice set.
        cmd += ["-A", str(catalog_base)]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    assert proc.stdout is not None

    result = ExtractResult(returncode=0)
    try:
        for line in proc.stdout:
            print(f"  [dar] {line}", end="")
            if m := _BAD_CRC_RE.search(line):
                result.corrupted.append(m.group(1).strip())
            elif m := _SKIPPED_RE.match(line.strip()):
                result.skipped.append(m.group(1).strip())
            elif m := _MISSING_SLICE_RE.search(line):
                result.missing_slice = m.group(1).strip()
            elif _MISSING_LAST_RE.search(line):
                result.missing_slice = LAST_SLICE
        proc.wait()
    except KeyboardInterrupt:
        # Own session → the tty's SIGINT never reached dar; kill it here.
        # Signal the whole process group (dar is its leader, see
        # start_new_session) so nothing it spawned outlives the cancel.
        _kill_group(proc)
        raise
    result.returncode = proc.returncode
    return result
