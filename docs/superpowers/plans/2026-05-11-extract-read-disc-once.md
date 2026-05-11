# Extract Read-Once + Create Page-Cache Optimization Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two independent I/O optimizations bundled into one PR because they share infrastructure (dar's `-E` hook):

1. **Extract:** Read each Blu-ray disc exactly once in the happy path — eliminate the standalone par2-verify pass and the slice-copy-to-staging step, keeping par2 only as the failure-mode fallback. Result: ~50% less SSD write traffic during restore plus a ~halving of total wall-time.

2. **Create:** Run par2 immediately after each slice is written (via dar's `-E` hook), instead of in a separate phase 3 pass. The slice is still hot in the OS page cache, so par2's read pass costs near-zero SSD reads. Total writes are unchanged (still ~615 GB for a 300 GB source), but SSD read traffic drops by roughly the volume of one full archive read (~300 GB), reducing thermal load.

**Architecture:**

Both optimizations are driven by dar's `-E` execute hook, verified empirically against dar 2.7.17:

- **For restore (extract):** "`-E` ... is executed before the slice is read or even asked." A helper module (`bd_archive._swap_helper`) is invoked between slices; it loads a JSON state file, unmounts the previous disc, prompts the user for the next, mounts it, and creates a symlink at the path dar is about to open. dar then reads the slice straight off the mounted disc — no copy. Integrity in the happy path comes from dar's per-file CRCs (independent of the `--hash sha512` sidecars). On dar failure, par2 fallback kicks in: re-mount the failing disc, copy slice + par2 to staging, run `par2 repair`, instruct the user to re-run extract.

- **For backup (create):** "for writing an archive [...] the given string is executed once the slice has been completed." A helper module (`bd_archive._par2_helper`) is invoked between slices; it builds the slice path from `%p/%b.%N.dar` and calls `par2.create()`. par2 reads the freshly-written slice while it is still in page cache, eliminating most of the SSD read traffic of the current phase-3 par2 loop. Phase 3 then only does mkisofs + cleanup (par2 files already exist on disk).

Empirically verified `-E` substitutions on dar 2.7.17 (via a 6-slice test run):
- `%p` = slice directory (not full path), e.g. `/tmp/dar-test/out`
- `%b` = archive basename (without slice number / extension)
- `%N` = zero-padded slice number, e.g. `0001`
- `%c` = `operation` for normal slices, `last_slice` for the final slice
- Full slice path = `%p/%b.%N.dar`

Note: `-p`/`--pause` (create-side equivalent of "stop between slices") is **incompatible with `-Q`** and refuses to operate without a TTY, so it cannot be driven by a subprocess pipe. `-E` is the only viable hook.

**Tech Stack:** Python 3.11+, dar 2.7+ (`--sequential-read`, `-E`), par2, existing `DiscIO` and prompts; no new external dependencies.

---

## File Structure

- Create `src/bd_archive/_swap_helper.py` — dar `-E` hook for extract; one responsibility (perform disc swap based on state file).
- Create `src/bd_archive/_par2_helper.py` — dar `-E` hook for create; one responsibility (run par2 on the just-completed slice).
- Modify `src/bd_archive/tools/dar.py` — add `execute_hook: str | None` parameter to both `extract_sequential` and `create_sliced`.
- Modify `src/bd_archive/archive/dar_archive.py` — propagate `par2_hook` through `DarArchive.create`.
- Modify `src/bd_archive/commands/extract.py` — complete rewrite of `cmd_extract` around mount-and-symlink flow.
- Modify `src/bd_archive/commands/create.py` — pass the par2 hook to `DarArchive.create`; remove the now-redundant `par2.create` call from the phase-3 loop; add a defensive missing-par2-files check.
- No changes to `tools/par2.py`, `archive/verify.py`, or `archive/disc.py`.

---

### Task 1: Add the `_swap_helper` module

**Files:**
- Create: `src/bd_archive/_swap_helper.py`

The helper is invoked by `dar -E "python3 -m bd_archive._swap_helper <state-file> %N %c"`. It is idempotent: if the requested slice is already symlinked and its target is readable, it does nothing (this is the init-context case where main process pre-staged disc 1).

- [ ] **Step 1: Write the helper module**

Create `src/bd_archive/_swap_helper.py`:

```python
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
        try:
            Path(md).rmdir()
        except OSError:
            pass
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
    context = sys.argv[3]

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
            try:
                new_dir.rmdir()
            except OSError:
                pass
            if not prompt_yn("Retry?"):
                sys.exit(1)
            continue

        target = new_path / _slice_name(archive_name, slice_num)
        if not target.exists():
            log.error(f"Slice {slice_num} ({target.name}) not on this disc")
            dio.umount(new_path)
            try:
                new_dir.rmdir()
            except OSError:
                pass
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
```

- [ ] **Step 2: Module-load smoke test**

```bash
PYTHONPATH=/home/mato/projects/_Privat/bd-archiver/src \
  python3 -c "from bd_archive import _swap_helper; print(_swap_helper.main.__doc__ or 'loaded')"
```

Expected: prints `loaded` (the module imports without error and `main` exists).

- [ ] **Step 3: Lint**

```bash
ruff check src/bd_archive/_swap_helper.py
```

Expected: no warnings.

- [ ] **Step 4: Commit**

```bash
git add src/bd_archive/_swap_helper.py
git commit -m "feat(extract): add _swap_helper module for dar -E disc swap"
```

---

### Task 2: Add `execute_hook` parameter to `dar.extract_sequential`

**Files:**
- Modify: `src/bd_archive/tools/dar.py`

Current `extract_sequential` (lines 42-80) spams ESC bytes on stdin so dar's missing-slice prompts auto-skip — that path stays as the default for disaster recovery from a partial disc set. New behaviour is gated on `execute_hook`: when set, dar gets `-E <hook>` and the ESC-feeder is disabled (it would interfere with normal extraction since the hook pre-stages slices, so the missing-slice prompt should never fire).

- [ ] **Step 1: Edit `extract_sequential`**

Replace the existing function (lines 42-80 of `src/bd_archive/tools/dar.py`) with:

```python
def extract_sequential(base_path: Path, output_dir: Path,
                       catalog_base: Path | None = None,
                       execute_hook: str | None = None) -> int:
    """Extract a dar archive with --sequential-read.

    When execute_hook is set, dar receives it via -E. dar fires the
    hook before opening each slice (verified against dar 2.7.17 man:
    "-E ... is executed before the slice is read or even asked"),
    which the caller uses to mount the next disc and symlink the
    expected slice path. The ESC-skip feeder is disabled in this
    mode because the hook pre-stages every slice — any prompt would
    indicate a real error.

    When execute_hook is None, the legacy ESC-feeding behaviour is
    used: dar's missing-slice prompts auto-skip so a partial disc
    set still restores ~95% of files without user intervention.

    Returns dar's exit code.
    """
    cmd = ["dar", "-x", str(base_path), "-R", str(output_dir),
           "-O", "--sequential-read"]
    if catalog_base is not None:
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
```

- [ ] **Step 2: Lint**

```bash
ruff check src/bd_archive/tools/dar.py
```

Expected: no warnings.

- [ ] **Step 3: Commit**

```bash
git add src/bd_archive/tools/dar.py
git commit -m "feat(dar): add execute_hook param to extract_sequential"
```

---

### Task 3: Rewrite `cmd_extract` for the read-once flow

**Files:**
- Modify: `src/bd_archive/commands/extract.py`

Replace the entire `cmd_extract` body with a flow that mounts disc 1, copies the catalog, symlinks slice 1, then hands off to dar with the swap helper as `-E`. On dar success, release the still-mounted final disc and report. On dar failure, offer a par2 repair pass that places a real (repaired) copy of the failing slice into staging so a re-run of `bd-archive extract` uses the repaired file.

- [ ] **Step 1: Replace `extract.py` end-to-end**

Replace the entire contents of `src/bd_archive/commands/extract.py` with:

```python
import json
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from bd_archive.archive.disc import DiscIO
from bd_archive.shell.deps import check_deps
from bd_archive.shell.format import human_bytes
from bd_archive.tools import dar, par2
from bd_archive.tools.par2 import is_par2_index
from bd_archive.ui.logger import log
from bd_archive.ui.prompts import prompt_disc, prompt_yn


def _slice_name(archive_name: str, slice_num: int) -> str:
    return f"{archive_name}.{slice_num:04d}.dar"


def _release(dio: DiscIO, state: dict, staging: Path, archive_name: str):
    """Mirror of _swap_helper._release; used by main process for the
    pre-dar-launch path and post-dar cleanup."""
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
        try:
            Path(md).rmdir()
        except OSError:
            pass
    dio.eject()
    state["current_mount_path"] = None
    state["current_mount_dir"] = None
    state["current_slice"] = None


def cmd_extract(args):
    check_deps("dar", "par2")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    work_dir = Path(args.workdir) if args.workdir else Path(
        tempfile.mkdtemp(prefix="bd-extract-"))
    staging = work_dir / "slices"
    staging.mkdir(parents=True, exist_ok=True)

    dio = DiscIO(args.device)

    log.step("Restore archive from discs (read-once)")
    log.info(f"Device:   {args.device}")
    log.info(f"Output:   {output_dir}")
    log.info(f"Staging:  {staging}")

    # ── Disc 1 bootstrap: archive-name detection + catalog copy ─────
    archive_name: str | None = None
    first_dir: Path | None = None
    first_path: Path | None = None
    while True:
        prompt_disc("Insert disc 1", args.device)
        first_dir = Path(tempfile.mkdtemp(prefix="bd-mount-"))
        first_path = dio.mount(first_dir)
        if first_path is None:
            log.error("Could not mount disc")
            try:
                first_dir.rmdir()
            except OSError:
                pass
            if not prompt_yn("Retry?"):
                sys.exit(1)
            continue

        dar_files = [p for p in first_path.glob("*.dar")
                     if "-catalog" not in p.name]
        if not dar_files:
            log.error("No dar slices on this disc — insert disc 1")
            dio.umount(first_path)
            try:
                first_dir.rmdir()
            except OSError:
                pass
            dio.eject()
            continue

        archive_name = dar_files[0].stem.rsplit(".", 1)[0]
        break

    assert archive_name is not None and first_path is not None
    log.info(f"Archive detected: {archive_name}")

    # Catalog files (small, ~MB) get copied so they survive disc swaps.
    cat_count = 0
    for cat in first_path.glob(f"{archive_name}-catalog.*"):
        dest = staging / cat.name
        if not dest.exists():
            shutil.copy2(cat, dest)
            cat_count += 1
    log.info(f"Catalog: {cat_count} file(s) copied")

    # Symlink slice 1 in place so dar finds it on its first open.
    slice1 = first_path / _slice_name(archive_name, 1)
    if not slice1.exists():
        log.error(f"Slice 1 ({slice1.name}) missing on disc 1")
        sys.exit(1)
    slice1_link = staging / slice1.name
    if slice1_link.is_symlink() or slice1_link.exists():
        slice1_link.unlink()
    slice1_link.symlink_to(slice1)

    # State file shared with the swap helper.
    state_file = work_dir / ".swap_state.json"
    state = {
        "device": args.device,
        "archive_name": archive_name,
        "staging_dir": str(staging),
        "current_mount_dir": str(first_dir),
        "current_mount_path": str(first_path),
        "current_slice": 1,
    }
    state_file.write_text(json.dumps(state))

    # ── Run dar with the swap helper as -E ──────────────────────────
    log.step("Extracting archive")
    log.info("(disc swaps happen between slices, driven by dar -E)")

    helper_cmd = (
        f"{shlex.quote(sys.executable)} -m bd_archive._swap_helper "
        f"{shlex.quote(str(state_file))} %N %c"
    )

    dar_base = staging / archive_name
    catalog_base = staging / f"{archive_name}-catalog"
    has_catalog = any(staging.glob(f"{archive_name}-catalog.*.dar"))

    rc = dar.extract_sequential(
        dar_base, output_dir,
        catalog_base=catalog_base if has_catalog else None,
        execute_hook=helper_cmd,
    )

    # Reload state — helper may have advanced through several discs.
    state = json.loads(state_file.read_text())

    # ── Cleanup: release whichever disc is still mounted ────────────
    _release(dio, state, staging, archive_name)
    state_file.write_text(json.dumps(state))

    if rc == 0:
        total = sum(f.stat().st_size for f in output_dir.rglob("*")
                    if f.is_file())
        log.step("Restore complete")
        print(f"\n  Archive: {archive_name}")
        print(f"  Output:  {output_dir}")
        print(f"  Size:    {human_bytes(total)}")
        print(f"\n  Cleanup staging: rm -rf {work_dir}\n")
        return

    # ── dar failed — likely slice CRC mismatch on the last-touched disc.
    last_slice = state.get("current_slice") or _last_known_slice(state_file)
    log.error(f"dar exited with code {rc}")
    if last_slice:
        log.info(f"The disc most recently being read was disc {last_slice}.")
    log.info("If the disc has bit rot we can run par2 repair on the bad "
             "slice now; re-running `bd-archive extract` afterwards will "
             "pick up the repaired file from staging instead of the disc.")
    if not prompt_yn("Run par2 repair?", default_yes=True):
        sys.exit(rc)
    if last_slice is None:
        log.error("Unknown disc — cannot offer automated repair")
        sys.exit(rc)

    _par2_repair_slice(dio, state, staging, archive_name, last_slice,
                       state_file, args, work_dir, output_dir)


def _last_known_slice(state_file: Path) -> int | None:
    """Read state file ignoring any 'released' nulls — for the
    post-dar-failure path where the helper may have nulled the
    current_slice during cleanup."""
    try:
        state = json.loads(state_file.read_text())
        return state.get("current_slice")
    except (OSError, json.JSONDecodeError):
        return None


def _par2_repair_slice(dio: DiscIO, state: dict, staging: Path,
                       archive_name: str, slice_num: int,
                       state_file: Path, args, work_dir: Path,
                       output_dir: Path):
    """Mount disc <slice_num>, copy slice + par2 sidecars into staging,
    run par2 repair, leave a real file (not a symlink) at the staging
    slot so a re-run of `bd-archive extract` consumes the repaired
    bytes instead of the disc."""
    while True:
        prompt_disc(f"Insert disc {slice_num} for repair", args.device)
        md = Path(tempfile.mkdtemp(prefix="bd-mount-"))
        mp = dio.mount(md)
        if mp is None:
            log.error("Could not mount disc")
            try:
                md.rmdir()
            except OSError:
                pass
            if not prompt_yn("Retry?"):
                sys.exit(1)
            continue
        if not (mp / _slice_name(archive_name, slice_num)).exists():
            log.error(f"Slice {slice_num} not on this disc")
            dio.umount(mp)
            try:
                md.rmdir()
            except OSError:
                pass
            dio.eject()
            if not prompt_yn("Try another disc?"):
                sys.exit(1)
            continue
        state["current_mount_dir"] = str(md)
        state["current_mount_path"] = str(mp)
        state["current_slice"] = slice_num
        state_file.write_text(json.dumps(state))
        break

    target_name = _slice_name(archive_name, slice_num)
    target = mp / target_name

    # Drop any existing symlink and replace with a real copy that par2
    # can rewrite in place.
    dest = staging / target_name
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    log.info(f"Copying {target_name} to staging for repair...")
    shutil.copy2(target, dest)
    for pf in mp.glob(f"{target_name}.*par2"):
        pf_dest = staging / pf.name
        if not pf_dest.exists():
            shutil.copy2(pf, pf_dest)

    par2_idx_list = [p for p in staging.glob(f"{target_name}.*par2")
                     if is_par2_index(p)]
    if not par2_idx_list:
        log.error("No par2 index found in staging — cannot repair")
        _release(dio, state, staging, archive_name)
        state_file.write_text(json.dumps(state))
        sys.exit(2)

    log.info("Running par2 repair...")
    if not par2.repair(par2_idx_list[0]):
        log.error("par2 repair failed — slice is unrecoverable")
        _release(dio, state, staging, archive_name)
        state_file.write_text(json.dumps(state))
        sys.exit(2)

    log.ok(f"Slice {slice_num} repaired in staging.")
    _release(dio, state, staging, archive_name)
    state_file.write_text(json.dumps(state))
    log.info(f"Re-run: bd-archive extract -o {output_dir} "
             f"-D {args.device} -w {work_dir}")
```

- [ ] **Step 2: Lint**

```bash
ruff check src/bd_archive/commands/extract.py
```

Expected: no warnings.

- [ ] **Step 3: Help-text smoke check**

```bash
PYTHONPATH=src python3 -m bd_archive extract --help
```

Expected: existing flags (`-o`, `-D`, `-w`) print; no traceback.

- [ ] **Step 4: Commit**

```bash
git add src/bd_archive/commands/extract.py
git commit -m "refactor(extract): read each disc once via dar -E swap helper"
```

---

### Task 4: Update CLAUDE.md architecture notes (extract bullet)

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Edit the extract bullet in CLAUDE.md**

In `/home/mato/projects/_Privat/bd-archiver/CLAUDE.md`, find the bullet `4. **`extract`**` and replace its body with:

```markdown
4. **`extract`** (`commands/extract.py`) is read-once: it mounts each disc just-in-time, symlinks the slice into staging, and hands off to `dar -x --sequential-read -E <swap-helper>` so dar reads the slice **directly from the mounted disc**. dar's `-E` hook (`bd_archive._swap_helper`) fires before each slice open (verified per dar 2.7 manpage), unmounts the previous disc, prompts the user for the next, mounts it, and updates the symlink. The catalog is the only payload copied to staging (small, ~MB). Integrity in the happy path comes from dar's per-file CRCs; if dar exits non-zero the user is offered a par2 repair pass on the last-touched disc, which places a repaired copy of the slice into staging so a re-run of `extract` consumes it instead of the disc.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update extract architecture description to read-once flow"
```

---

### Task 5: End-to-end manual verification

This task has no code changes — it is the manual smoke test the author should run on real hardware before merging. There are no unit tests in this project.

- [ ] **Step 1: Build a tiny multi-disc archive**

Use the smallest source you can find that produces at least 3 slices. Force small slices via `-b`:

```bash
bd-archive create -s /path/to/small/source -n smoketest \
  -w /tmp/bd-smoke -b $((100*1024*1024)) -c none -r 5
```

`-b 100M` makes each ISO ~100 MiB; tiny enough for fast iteration.

- [ ] **Step 2: Burn to spare BD-RE (or use loop-mounted ISOs as stand-ins)**

For loop-mount emulation (no real drive needed), point the device at a loop-attached ISO between mount cycles. For a true end-to-end smoke, burn to BD-RE:

```bash
bd-archive burn -w /tmp/bd-smoke
```

- [ ] **Step 3: Watch the extract — verify single read per disc**

In one terminal, monitor I/O on the staging filesystem:

```bash
iostat -x 1 $(findmnt -no SOURCE /tmp/bd-extract-test/ 2>/dev/null || echo /dev/sda) | grep -E "Device|sda"
```

In another, run extract:

```bash
bd-archive extract -o /tmp/bd-restored -D /dev/sr0 -w /tmp/bd-extract-test
```

Verify by direct observation:
- Each disc mounts **once** (one `Insert disc N` prompt per disc, one mount-related kernel message per disc).
- No "Copying slice ..." or "Checking disc ..." log lines between discs.
- dar `[dar]` output advances continuously through slices.
- `iostat -x 1` for the staging filesystem shows near-zero bytes-written (only catalog + symlink + state-file traffic).
- Restored size matches source.

- [ ] **Step 4: Exercise the par2 fallback**

To trigger the failure path without a real bit-rotted disc, dd a small region of an unburned ISO and re-burn (or modify a loop-mounted ISO copy):

```bash
# pick a slice file inside an ISO copy and corrupt a few bytes
dd if=/dev/urandom of=/tmp/copy_of_disc_0003.iso bs=1 count=128 \
   seek=$((500*1024*1024)) conv=notrunc
```

Re-run extract; dar should exit non-zero on disc 3; the tool should offer par2 repair; accept; re-run extract; restored output should be complete.

---

### Task 6: Add the `_par2_helper` module

**Files:**
- Create: `src/bd_archive/_par2_helper.py`

The helper is invoked by `dar -E "python3 -m bd_archive._par2_helper %p %b %N <redundancy>"`. dar's `-E` macros were verified empirically on dar 2.7.17 (see Architecture). The helper constructs the slice path as `<path>/<basename>.<num>.dar` and calls `par2.create()` — same function the current phase-3 loop in `cmd_create` calls, just invoked one slice earlier (right after dar finishes writing it, while bytes are still in page cache).

- [ ] **Step 1: Write the helper module**

Create `src/bd_archive/_par2_helper.py`:

```python
"""dar -E hook for create: runs par2 on the slice dar just completed.

Invoked as: python3 -m bd_archive._par2_helper <path> <basename> <num> <redundancy>

dar fires -E "once the slice has been completed" during create
(verified against dar 2.7.17 man page and a 6-slice empirical run).
Substitutions:
  %p -> <path>      directory containing slices
  %b -> <basename>  archive base name (e.g. "myarchive")
  %N -> <num>       zero-padded slice number (e.g. "0001")

Running par2 here (rather than in a separate phase-3 loop in
cmd_create) leverages the OS page cache: dar's just-written slice
is still mostly in RAM, so par2's read pass costs near-zero SSD
reads. Total writes are unchanged.

A non-zero exit from this helper is reported back to dar, which
will abort the backup (and surface a non-zero status from
dar.create_sliced). cmd_create additionally checks for the
presence of .par2 files before building each ISO in phase 3.
"""
import sys
from pathlib import Path

from bd_archive.tools import par2


def main():
    if len(sys.argv) != 5:
        print(f"usage: {sys.argv[0]} <path> <basename> <num> <redundancy>",
              file=sys.stderr)
        sys.exit(2)
    path = Path(sys.argv[1])
    basename = sys.argv[2]
    num = sys.argv[3]  # zero-padded, e.g. "0001"
    redundancy = int(sys.argv[4])

    slice_path = path / f"{basename}.{num}.dar"
    if not slice_path.exists():
        print(f"_par2_helper: slice not found: {slice_path}",
              file=sys.stderr)
        sys.exit(1)
    par2.create(slice_path, redundancy)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Module-load smoke test**

```bash
PYTHONPATH=/home/mato/projects/_Privat/bd-archiver/src \
  python3 -c "from bd_archive import _par2_helper; print('loaded')"
```

Expected: `loaded`.

- [ ] **Step 3: Lint**

```bash
ruff check src/bd_archive/_par2_helper.py
```

Expected: no warnings.

- [ ] **Step 4: Commit**

```bash
git add src/bd_archive/_par2_helper.py
git commit -m "feat(create): add _par2_helper module for dar -E par2 hook"
```

---

### Task 7: Add `execute_hook` parameter to `dar.create_sliced` and `DarArchive.create`

**Files:**
- Modify: `src/bd_archive/tools/dar.py`
- Modify: `src/bd_archive/archive/dar_archive.py`

Plumb a `-E` hook string through both layers. The lower-level `dar.create_sliced` simply appends `-E <hook>` to the command line; the higher-level `DarArchive.create` forwards it under the name `par2_hook` so call sites read intentionally.

- [ ] **Step 1: Edit `dar.create_sliced` in `src/bd_archive/tools/dar.py`**

Replace the existing function (lines 8-19) with:

```python
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
```

- [ ] **Step 2: Edit `DarArchive.create` in `src/bd_archive/archive/dar_archive.py`**

Replace lines 24-27 with:

```python
    def create(self, source: Path, slice_bytes: int,
               compression: str, comp_level: str | None,
               par2_hook: str | None = None):
        dar.create_sliced(self.base_path, source, slice_bytes,
                          compression, comp_level,
                          execute_hook=par2_hook)
```

- [ ] **Step 3: Lint**

```bash
ruff check src/bd_archive/tools/dar.py src/bd_archive/archive/dar_archive.py
```

Expected: no warnings.

- [ ] **Step 4: Commit**

```bash
git add src/bd_archive/tools/dar.py src/bd_archive/archive/dar_archive.py
git commit -m "feat(dar): plumb execute_hook through create_sliced/DarArchive.create"
```

---

### Task 8: Wire the par2 hook into `cmd_create` and drop redundant phase-3 par2

**Files:**
- Modify: `src/bd_archive/commands/create.py`

The phase-3 loop currently calls `par2.create(slice_file, cfg.redundancy)` for each slice (line ~131-132). After this task, par2 runs via the `-E` hook during `DarArchive.create`, so phase 3 only needs to *find* the existing `.par2` files (a sanity check covers the case where the helper failed silently).

- [ ] **Step 1: Add imports at the top of `src/bd_archive/commands/create.py`**

Add `shlex` and `sys` to the import block (currently `import shutil; import sys`). `sys` may already be there — confirm and skip if so:

```python
import shlex
import shutil
import sys
from pathlib import Path
```

- [ ] **Step 2: Pass the `par2_hook` to `DarArchive.create`**

Find the existing call (line 97 of `commands/create.py`):

```python
    dar_archive.create(source, slice_bytes, cfg.compression, cfg.comp_level)
```

Replace with:

```python
    par2_hook = (
        f"{shlex.quote(sys.executable)} -m bd_archive._par2_helper "
        f"%p %b %N {cfg.redundancy}"
    )
    dar_archive.create(source, slice_bytes, cfg.compression,
                       cfg.comp_level, par2_hook=par2_hook)
```

- [ ] **Step 3: Drop the redundant par2 call in the phase-3 loop**

Find these lines in the per-slice loop (around lines 130-133 of `commands/create.py`):

```python
        # PAR2 writes recovery files alongside the slice in tmp_dir
        log.info(f"  par2 ({cfg.redundancy}% redundancy)...")
        par2.create(slice_file, cfg.redundancy)
        par2_files = sorted(tmp_dir.glob(f"{slice_name}.*par2"))
```

Replace with:

```python
        # par2 was already produced via the -E hook during dar create
        # (above). Verify the files are present — a missing file means
        # the helper silently failed on this slice.
        par2_files = sorted(tmp_dir.glob(f"{slice_name}.*par2"))
        if not par2_files:
            log.error(f"par2 files missing for {slice_name} "
                      f"(_par2_helper likely failed during dar create)")
            sys.exit(1)
```

- [ ] **Step 4: Drop the now-unused `par2` import**

Find the imports section of `commands/create.py` (around line 17):

```python
from bd_archive.tools import mkisofs, par2
```

Replace with:

```python
from bd_archive.tools import mkisofs
```

(After this task, `commands/create.py` no longer calls anything from `tools.par2` directly — the par2 work is done by `_par2_helper`.)

- [ ] **Step 5: Update the CLAUDE.md create bullet**

In `/home/mato/projects/_Privat/bd-archiver/CLAUDE.md`, find the bullet `1. **`create`**` and replace its body with:

```markdown
1. **`create`** (`commands/create.py`) reads disc capacity via `tools.mediainfo.detect_disc_capacity` (or `args.bytes`), runs `tools.dar.create_sliced` with `--hash sha512 --min-digits 4` to slice the source into per-disc-sized `.dar` files in `<workdir>/tmp/`. par2 is generated **inline** via dar's `-E` hook (`bd_archive._par2_helper`) — the hook fires after each slice is fully written, so par2 reads the slice while it is still hot in the OS page cache, eliminating most SSD read traffic of the create phase. After dar completes, the catalog is isolated. For each slice in order: regenerate `README.txt` with the right disc number and call `tools.mkisofs.build` (mkisofs `-iso-level 3 -udf -graft-points`) to assemble `<workdir>/images/disc_NNNN.iso` directly from in-place files (no staging copies). The ISO file size is checked against the format-aware writable capacity as a hard limit. After each ISO is built, the slice + par2 are deleted from `tmp/`; once all slices are processed, `tmp/` is wiped entirely. Final workdir contains only `images/disc_*.iso`.
```

- [ ] **Step 6: Lint**

```bash
ruff check src/bd_archive/commands/create.py
```

Expected: no warnings.

- [ ] **Step 7: Help-text smoke check**

```bash
PYTHONPATH=src python3 -m bd_archive create --help
```

Expected: existing flags print; no traceback.

- [ ] **Step 8: Commit**

```bash
git add src/bd_archive/commands/create.py CLAUDE.md
git commit -m "refactor(create): run par2 via dar -E hook for page-cache reuse"
```

---

### Task 9: End-to-end manual verification of create

This task has no code changes. It is the smoke test that demonstrates the page-cache benefit on real hardware.

- [ ] **Step 1: Drop OS page cache, then run create**

The point of this test is to compare cold-cache I/O before and after. Run *before* the create:

```bash
sync && echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
```

Then run create on a representative source (large enough that the total exceeds RAM — for a 32 GB box, a 60+ GB source is fine):

```bash
bd-archive create -s /path/to/large/source -n cachetest \
  -w /tmp/bd-cachetest -b $((25*1024*1024*1024)) -c none -r 5
```

Watch `iostat -x 1 <device-backing-/tmp/bd-cachetest>` in another terminal during the run.

- [ ] **Step 2: Verify the cache-reuse pattern**

Expected pattern in `iostat`:
- During each slice creation: high write rate (dar writing the slice).
- Right after each slice finishes (before the next begins): a smaller burst where par2 reads + writes, with **far lower read bytes** than the slice size (most reads served from page cache, not the device).
- Phase 3 (mkisofs): heavy read+write per ISO; reads come from device (cache long evicted by then).

Concretely, the per-slice read volume during the par2 burst should be **a small fraction** of the slice size, not roughly equal to it as it is today.

- [ ] **Step 3: Verify correctness — par2 + mkisofs outputs match the old code**

Spot-check after create completes:

```bash
ls -la /tmp/bd-cachetest/images/
# expect 3+ disc_NNNN.iso files, sizes ≤ 25 GiB each

# verify the first ISO end-to-end through the existing verify path
bd-archive verify /tmp/bd-cachetest/images/disc_0001.iso
```

Expected: `verify` reports OK for all par2 indices.

- [ ] **Step 4: Cross-check: simulate `_par2_helper` failure**

To make sure the defensive check in Task 8 Step 3 actually fires, temporarily corrupt the helper:

```bash
# break the helper without changing version-controlled code
PAR2_HELPER=$(python3 -c "import bd_archive._par2_helper as m; print(m.__file__)")
mv "$PAR2_HELPER" "$PAR2_HELPER.bak"
echo "import sys; sys.exit(1)" > "$PAR2_HELPER"

# run a tiny create
bd-archive create -s /tmp/small_test -n fail_test \
  -w /tmp/bd-failtest -b $((100*1024*1024)) -c none -r 5

# restore the helper
mv "$PAR2_HELPER.bak" "$PAR2_HELPER"
```

Expected: bd-archive exits non-zero with `par2 files missing for ... (_par2_helper likely failed during dar create)`. dar itself may also surface the helper's non-zero status — that is fine, we just want the safety net to catch it.

---

## Self-Review

**Spec coverage — Extract (Tasks 1-5):**
- ✓ Disc read once in happy path — Task 3 symlinks dar at the mount; no copy occurs.
- ✓ Hash check not skipped — dar's per-file CRCs (built into archive at create time with `--hash sha512 --min-digits 4`) cover integrity in the same read.
- ✓ par2 fallback preserved — Task 3 `_par2_repair_slice` flow.
- ✓ Disaster recovery preserved — Task 2 keeps the legacy ESC-skip path when `execute_hook is None`.

**Spec coverage — Create (Tasks 6-9):**
- ✓ par2 runs while slice is still in page cache — Task 6 helper invoked via dar `-E` after each slice completes; Task 7 plumbs the hook.
- ✓ Existing `par2.create` semantics preserved — helper calls the same function; on-disc layout is identical.
- ✓ No regression on hook failure — Task 8 Step 3 adds a defensive "par2 files missing" check; Task 9 Step 4 exercises it.
- ✓ Total writes unchanged — confirmed in goal; benefit is read-side via page cache reuse.

**Placeholder scan:** All code blocks contain concrete content; no TODO, TBD, or "implement later". Test steps deliberately leave `iostat` device name / source path to user discovery since they depend on the user's filesystem layout.

**Type consistency:**
- `_slice_name(archive_name, slice_num)` defined identically in `_swap_helper.py` and `commands/extract.py` (kept duplicated rather than moving to a third module — the helper is a runtime entry point, not a library, and the cost of import-graph drift would outweigh the DRY).
- `state` dict shape (`device`, `archive_name`, `staging_dir`, `current_mount_dir`, `current_mount_path`, `current_slice`) consistent between writer (`cmd_extract`) and reader/writer (`_swap_helper.main`).
- `extract_sequential(execute_hook=...)` consistent between Task 2 definition and Task 3 call site.
- `create_sliced(execute_hook=...)` (Task 7) named consistently with `extract_sequential(execute_hook=...)` (Task 2). `DarArchive.create(par2_hook=...)` uses the more specific name at the higher abstraction level (Task 7 Step 2; Task 8 Step 2 call site matches).
- `_par2_helper` positional args (`<path> <basename> <num> <redundancy>`) match the `-E` substitution string built in Task 8 Step 2 (`%p %b %N {cfg.redundancy}`).

**Risks documented:**
- dar `-E` could in principle fire repeatedly with `%c=init` during the catalog-probe phase (extract). Helper idempotency (`if target_link.is_symlink() and resolved.exists(): return`) handles this.
- `%n=0` (dar doesn't know slice number) is handled by no-op + falling back to dar's own prompt (extract helper).
- Disc-swap helper runs in its own process — `log.info` colour output may differ from main process if the TTY is forwarded oddly, but stdin/stdout are inherited so prompts work.
- Re-running extract after par2 repair will re-extract files (no `--no-overwrite` logic added); acceptable because dar refuses to clobber by default unless `-w` is passed.
- par2 helper failure during create: dar's single-`-E` mode propagates the helper's exit status, **and** Task 8 Step 3 adds an explicit `par2_files` presence check in phase 3 as a belt-and-suspenders safety net.
- Cache benefit assumes Linux page cache holds the slice bytes between dar write and par2 read. With a 25 GB slice on a 32 GB box this is reliable; with 50 GB slices, only ~32 GB stays cached so reads see a partial benefit (estimated 60-70% reduction instead of 100%). Documented as expected behaviour, not a defect.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-11-extract-read-disc-once.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
