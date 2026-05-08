# Disc Capacity Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hardcoded BD-25/50/100 disc sizes with runtime detection via `dvd+rw-mediainfo`, add a pre-burn fit check, and remove the now-redundant `bd-archive.json` metadata file.

**Architecture:** Single Python file (`bd-archive.py`). Capacity flows from one new helper `detect_disc_capacity()` into both `cmd_create` (compute slice size) and `cmd_burn` (verify staging fits the inserted disc within 5%). All metadata `cmd_burn` needs is derived from the staging directory contents.

**Tech Stack:** Python 3.10+, `dvd+rw-mediainfo` (already shipped in `dvd+rw-tools` runtime dep), `dar`, `par2`, `growisofs`. No test framework — verification is via running the CLI and observing output.

**Spec:** `docs/superpowers/specs/2026-05-08-disc-capacity-detection-design.md`

---

## File Structure

Single-file project. All edits target `bd-archive.py`. Anchors below refer to existing functions/sections (line numbers drift as we edit, so use string anchors).

| Anchor | Current line | Role |
|---|---|---|
| `import json` | 18 | Will be removed |
| `DISC_CAPACITY = {` | 34 | Will be removed |
| `METADATA_FILE = "bd-archive.json"` | 41 | Will be removed |
| `def run(cmd:` | 104 | Subprocess wrapper (unchanged) |
| `def check_deps(` | 121 | Will gain `dvd+rw-mediainfo` |
| `class DiscIO:` | 149 | Unchanged |
| `def save_metadata(` | 364 | Will be removed |
| `def load_metadata(` | 369 | Will be removed |
| `def generate_readme(` | 382 | Signature changes (`disc_size` → `disc_bytes`) |
| `def cmd_create(` | 398 | Major rewrite (CLI + capacity logic) |
| `def cmd_burn(` | 525 | Major rewrite (drop metadata + fit check) |
| `def build_parser(` | 782 | CLI flags change |
| `VERSION = "3.0.0"` | 28 | Bump to `4.0.0` |

A new top-level helper `detect_disc_capacity()` is added in the **Helpers** section near `human_bytes()` and `run()`.

---

## Task 1: Add `detect_disc_capacity` helper and update dependency check

**Files:**
- Modify: `bd-archive.py`

- [ ] **Step 1: Add `re` import**

Find the import block at the top:

```python
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
```

Replace with (adds `re`):

```python
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
```

- [ ] **Step 2: Add `detect_disc_capacity` function**

Insert this new function in the **Helpers** section, right after `def check_deps(*commands: str):` and before `def prompt_disc(`:

```python
def detect_disc_capacity(device: str) -> int | None:
    """Read raw writable bytes from the inserted disc via dvd+rw-mediainfo.

    Returns None if no disc is present, the command fails, or the
    output cannot be parsed. The caller decides how to handle that
    (hard error in create, soft warn in burn).
    """
    r = subprocess.run(
        ["dvd+rw-mediainfo", device],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    m = re.search(r"Free Blocks:\s+(\d+)\*2KB", r.stdout)
    if not m:
        return None
    return int(m.group(1)) * 2048
```

`Free Blocks: NNNN*2KB` is the standard `dvd+rw-mediainfo` output for blank or partially-written rewritable media; the value is in 2-KiB units, hence `* 2048`.

- [ ] **Step 3: Smoke-test the helper in isolation**

Run an inline check that the function imports cleanly and behaves predictably when no device is present:

```bash
python3 -c "
import sys
sys.path.insert(0, '.')
import importlib.util
spec = importlib.util.spec_from_file_location('bda', 'bd-archive.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print('detect ok:', m.detect_disc_capacity('/dev/null'))
"
```

Expected: prints `detect ok: None` (no error). The script doesn't run main when imported because of `if __name__ == "__main__":`.

- [ ] **Step 4: Commit**

```bash
git add bd-archive.py
git commit -m "Add detect_disc_capacity helper for runtime disc size detection."
```

---

## Task 2: Switch `cmd_create` to runtime detection (drop `-d`, add `-D` and `-b`)

**Files:**
- Modify: `bd-archive.py`

This task removes `DISC_CAPACITY`, the `-d/--disc-size` flag, and the disc-size-label format strings from `cmd_create` and `generate_readme`. After this task, `bd-archive.json` is still written by `cmd_create` (we tear that down in Task 4) so `cmd_burn` keeps working as-is.

- [ ] **Step 1: Remove `DISC_CAPACITY` block**

Find and delete this whole block (including the comment header above it):

```python
# ════════════════════════════════════════════════════════════════════════════
# Disc capacities (raw data area minus 2 MiB filesystem overhead)
# ════════════════════════════════════════════════════════════════════════════

DISC_CAPACITY = {
    25:  25_025_314_816 - 2 * 1024 * 1024,
    50:  50_050_629_632 - 2 * 1024 * 1024,
    100: 100_101_259_264 - 2 * 1024 * 1024,
}
```

Leave `MiB = 1024 * 1024` and `METADATA_FILE = "bd-archive.json"` untouched (the second is still used until Task 4). Add a single blank line so the file structure stays readable.

- [ ] **Step 2: Update `generate_readme` signature**

Find the existing function:

```python
def generate_readme(stage_dir: Path, archive_name: str, disc_num: int,
                    total_discs: int, slice_name: str, disc_size: int,
                    redundancy: int, compression: str,
                    comp_level: str | None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    comp_str = compression + (f" ({comp_level})" if comp_level else "")
    (stage_dir / "README.txt").write_text(
        f"BD-ARCHIVE | {archive_name} | Disc {disc_num}/{total_discs}"
        f" | {ts} | BD-{disc_size} | PAR2 {redundancy}% | {comp_str}\n\n"
        f"RESTORE:  dar -x {archive_name} -R /target\n"
        f"VERIFY:   par2 verify {slice_name}.par2\n"
        f"REPAIR:   par2 repair {slice_name}.par2\n"
        f"DEPENDS:  pacman -S dar par2cmdline  |  apt install dar par2\n"
    )
```

Replace with (renames `disc_size: int` → `disc_bytes: int`, formats with `human_bytes`):

```python
def generate_readme(stage_dir: Path, archive_name: str, disc_num: int,
                    total_discs: int, slice_name: str, disc_bytes: int,
                    redundancy: int, compression: str,
                    comp_level: str | None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    comp_str = compression + (f" ({comp_level})" if comp_level else "")
    (stage_dir / "README.txt").write_text(
        f"BD-ARCHIVE | {archive_name} | Disc {disc_num}/{total_discs}"
        f" | {ts} | Capacity {human_bytes(disc_bytes)}"
        f" | PAR2 {redundancy}% | {comp_str}\n\n"
        f"RESTORE:  dar -x {archive_name} -R /target\n"
        f"VERIFY:   par2 verify {slice_name}.par2\n"
        f"REPAIR:   par2 repair {slice_name}.par2\n"
        f"DEPENDS:  pacman -S dar par2cmdline  |  apt install dar par2\n"
    )
```

- [ ] **Step 3: Update `cmd_create` capacity resolution and dependency check**

Find the start of `cmd_create`:

```python
def cmd_create(args):
    check_deps("dar", "par2")

    source = Path(args.source).resolve()
    if not source.is_dir():
        log.error(f"Does not exist: {source}")
        sys.exit(1)

    work_dir = Path(args.workdir)
    work_dir.mkdir(parents=True, exist_ok=True)

    disc_bytes = DISC_CAPACITY[args.disc_size]
```

Replace with:

```python
def cmd_create(args):
    check_deps("dar", "par2", "dvd+rw-mediainfo")

    source = Path(args.source).resolve()
    if not source.is_dir():
        log.error(f"Does not exist: {source}")
        sys.exit(1)

    work_dir = Path(args.workdir)
    work_dir.mkdir(parents=True, exist_ok=True)

    if args.bytes is not None:
        raw_capacity = args.bytes
        log.info(f"Using manual capacity: {human_bytes(raw_capacity)}")
    else:
        raw_capacity = detect_disc_capacity(args.device)
        if raw_capacity is None:
            log.error(f"No disc detected at {args.device}.")
            log.info("Insert a blank disc, or specify capacity manually "
                     "with -b/--bytes <int>.")
            sys.exit(1)
        log.info(f"Detected {human_bytes(raw_capacity)} free space, "
                 f"splitting with this size")

    disc_bytes = raw_capacity - 2 * MiB  # ISO/UDF filesystem overhead
```

- [ ] **Step 4: Update the Configuration log block in `cmd_create`**

Find:

```python
    log.step("Configuration")
    log.info(f"Disc type:    BD-{args.disc_size} ({human_bytes(disc_bytes)})")
    log.info(f"Slice size:   {human_bytes(slice_bytes)}")
    log.info(f"PAR2:         {args.redundancy}% (~{human_bytes(par2_est)})")
    log.info(f"Compression:  {comp_str}")
    log.info(f"Source:       {source}")
    log.info(f"Workdir:      {work_dir}")
```

Replace with (drops `BD-{n}` label):

```python
    log.step("Configuration")
    log.info(f"Disc capacity: {human_bytes(disc_bytes)}")
    log.info(f"Slice size:    {human_bytes(slice_bytes)}")
    log.info(f"PAR2:          {args.redundancy}% (~{human_bytes(par2_est)})")
    log.info(f"Compression:   {comp_str}")
    log.info(f"Source:        {source}")
    log.info(f"Workdir:       {work_dir}")
```

- [ ] **Step 5: Update the `generate_readme` call site in `cmd_create`**

Find inside the `for i, slice_file in enumerate(slices, 1):` loop:

```python
        # README
        generate_readme(stage, args.name, i, slice_count, slice_name,
                        args.disc_size, args.redundancy,
                        args.compression, args.level)
```

Replace with:

```python
        # README
        generate_readme(stage, args.name, i, slice_count, slice_name,
                        disc_bytes, args.redundancy,
                        args.compression, args.level)
```

- [ ] **Step 6: Update the per-disc size summary log in `cmd_create`**

Find:

```python
        # Size check
        stage_size = sum(f.stat().st_size for f in stage.iterdir()
                         if f.is_file())
        pct = stage_size * 100 // disc_bytes
        log.ok(f"Disc {i}/{slice_count}: {human_bytes(stage_size)} "
               f"({pct}% of BD-{args.disc_size}), "
               f"{file_count} files")
```

Replace with:

```python
        # Size check
        stage_size = sum(f.stat().st_size for f in stage.iterdir()
                         if f.is_file())
        pct = stage_size * 100 // disc_bytes
        log.ok(f"Disc {i}/{slice_count}: {human_bytes(stage_size)} "
               f"({pct}% of {human_bytes(disc_bytes)}), "
               f"{file_count} files")
```

- [ ] **Step 7: Update `save_metadata` call to drop `disc_size` field**

Find:

```python
    save_metadata(
        work_dir,
        version=VERSION,
        archive_name=args.name,
        source=str(source),
        source_size=source_size,
        archive_size=total_archive,
        disc_count=slice_count,
        disc_size=args.disc_size,
        redundancy=args.redundancy,
        compression=args.compression,
        comp_level=args.level,
        created=datetime.now().isoformat(),
    )
```

Replace with (`disc_size` → `disc_bytes` in raw bytes; the file is still written here but Task 4 removes it entirely):

```python
    save_metadata(
        work_dir,
        version=VERSION,
        archive_name=args.name,
        source=str(source),
        source_size=source_size,
        archive_size=total_archive,
        disc_count=slice_count,
        disc_bytes=disc_bytes,
        redundancy=args.redundancy,
        compression=args.compression,
        comp_level=args.level,
        created=datetime.now().isoformat(),
    )
```

- [ ] **Step 8: Update the final Summary block in `cmd_create`**

Find:

```python
    log.step("Summary")
    print(f"\n  Source:       {human_bytes(source_size)}")
    print(f"  Archive:      {human_bytes(total_archive)} ({ratio}%)")
    print(f"  Discs:        {slice_count} x BD-{args.disc_size}")
    print(f"  PAR2:         {args.redundancy}% per disc")
    print(f"  Compression:  {comp_str}")
    print(f"  Staging:      {work_dir / 'staging'}")
    print(f"\n  Next step:    bd-archive.py burn -w {work_dir}")
    print(f"  Cleanup:      rm -rf {work_dir}\n")
```

Replace with:

```python
    log.step("Summary")
    print(f"\n  Source:       {human_bytes(source_size)}")
    print(f"  Archive:      {human_bytes(total_archive)} ({ratio}%)")
    print(f"  Discs:        {slice_count} x {human_bytes(disc_bytes)}")
    print(f"  PAR2:         {args.redundancy}% per disc")
    print(f"  Compression:  {comp_str}")
    print(f"  Staging:      {work_dir / 'staging'}")
    print(f"\n  Next step:    bd-archive.py burn -w {work_dir}")
    print(f"  Cleanup:      rm -rf {work_dir}\n")
```

- [ ] **Step 9: Update the `create` argparse subparser**

In `build_parser()`, find:

```python
    cr.add_argument("-r", "--redundancy", type=int, default=5,
                    help="PAR2 redundancy in %% (default: 5)")
    cr.add_argument("-d", "--disc-size", type=int, default=25,
                    choices=[25, 50, 100],
                    help="Disc size in GB (default: 25)")
    cr.add_argument("-c", "--compression", default="zstd",
```

Replace with (drops `-d`, adds `-D` and `-b`):

```python
    cr.add_argument("-r", "--redundancy", type=int, default=5,
                    help="PAR2 redundancy in %% (default: 5)")
    cr.add_argument("-D", "--device", default="/dev/sr0",
                    help="Optical drive for capacity detection "
                         "(default: /dev/sr0)")
    cr.add_argument("-b", "--bytes", type=int, default=None,
                    help="Manual disc capacity in raw bytes "
                         "(overrides detection)")
    cr.add_argument("-c", "--compression", default="zstd",
```

- [ ] **Step 10: Smoke-test `cmd_create` argument parsing**

Without running the full pipeline, verify the parser changes accept new flags and reject the old one:

```bash
./bd-archive.py create -h
```

Expected: help output shows `-D/--device`, `-b/--bytes`, and **no** `-d/--disc-size` line.

```bash
./bd-archive.py create -s /tmp -n x -w /tmp/x -d 25 2>&1 | head -3
```

Expected: argparse error mentioning unrecognized argument `-d/--disc-size`.

- [ ] **Step 11: Smoke-test `cmd_create` end-to-end with `-b`**

Create a tiny source tree and a manual capacity that produces 2 small slices:

```bash
mkdir -p /tmp/bda-src && head -c 5M /dev/urandom > /tmp/bda-src/data.bin
rm -rf /tmp/bda-work
./bd-archive.py create -s /tmp/bda-src -n test -w /tmp/bda-work -b 4194304 -r 5 -c none
```

Expected: log line `[INFO]  Using manual capacity: 4.0 MiB`, "Configuration" block shows `Disc capacity: ~2.0 MiB`, two `staging/disc_*/` directories created, `bd-archive.json` exists in `/tmp/bda-work` (still written; Task 4 removes it).

Inspect a README:

```bash
cat /tmp/bda-work/staging/disc_1/README.txt | head -1
```

Expected: line includes `Capacity 2.X MiB` instead of `BD-25`.

- [ ] **Step 12: Smoke-test detection failure path**

```bash
rm -rf /tmp/bda-work
./bd-archive.py create -s /tmp/bda-src -n test -w /tmp/bda-work -D /dev/null -r 5 -c none 2>&1 | tail -5
```

Expected: `[ ERR]  No disc detected at /dev/null.` followed by the manual `-b` hint, exit code 1.

- [ ] **Step 13: Commit**

```bash
git add bd-archive.py
git commit -m "Switch cmd_create to runtime disc capacity detection."
```

---

## Task 3: Refactor `cmd_burn` — derive metadata from staging, add fit check

**Files:**
- Modify: `bd-archive.py`

After this task, `cmd_burn` no longer reads `bd-archive.json`. The fit check is wired in. Old workdirs from Task 2 still work because the staging layout is identical.

- [ ] **Step 1: Add `--skip-fit-check` to the `burn` argparse subparser**

In `build_parser()`, find:

```python
    bu.add_argument("--start", type=int, default=1,
                    help="Start from disc N (default: 1)")
    bu.add_argument("--no-verify", action="store_true",
                    help="Skip post-burn verification")
```

Replace with (adds `--skip-fit-check`):

```python
    bu.add_argument("--start", type=int, default=1,
                    help="Start from disc N (default: 1)")
    bu.add_argument("--no-verify", action="store_true",
                    help="Skip post-burn verification")
    bu.add_argument("--skip-fit-check", action="store_true",
                    help="Skip pre-burn disc capacity check")
```

- [ ] **Step 2: Replace `cmd_burn` body**

Replace the entire `cmd_burn` function:

```python
def cmd_burn(args):
    check_deps("growisofs")

    work_dir = Path(args.workdir)
    meta = load_metadata(work_dir)

    disc_count = meta["disc_count"]
    disc_size = meta["disc_size"]
    archive_name = meta["archive_name"]
    start = args.start

    if start < 1 or start > disc_count:
        log.error(f"--start must be between 1 and {disc_count}")
        sys.exit(1)

    dio = DiscIO(args.device)

    log.step("Burn staged discs")
    log.info(f"Archive:  {archive_name}")
    log.info(f"Discs:    {disc_count} x BD-{disc_size}")
    log.info(f"Device:   {args.device}")
    if start > 1:
        log.info(f"Resuming from disc {start}")

    for i in range(start, disc_count + 1):
        stage = work_dir / "staging" / f"disc_{i}"
        if not stage.exists():
            log.error(f"Staging directory not found: {stage}")
            log.info("Run 'create' first to prepare the archive.")
            sys.exit(1)

        log.step(f"Disc {i}/{disc_count}")

        # Show contents
        stage_size = sum(f.stat().st_size for f in stage.iterdir()
                         if f.is_file())
        log.info(f"Size: {human_bytes(stage_size)}")

        prompt_disc(f"Insert blank BD-{disc_size} — "
                    f"Disc {i}/{disc_count}", args.device)

        # Burn
        log.info("Burning...")
        dio.burn(stage, f"{archive_name}_{i}", args.speed)
        log.ok(f"Disc {i} burned")

        # Post-burn verify
        if not args.no_verify:
            log.info("Post-burn verification...")
            time.sleep(5)
            mount_dir = Path(tempfile.mkdtemp(prefix="bd-verify-"))
            if dio.mount(mount_dir):
                try:
                    result = verify_disc(mount_dir,
                                         f"Disc {i} (post-burn)", quiet=True)
                    if result == VerifyResult.BROKEN:
                        log.error("Post-burn verification failed!")
                        if not prompt_yn("Continue?", default_yes=False):
                            log.info(f"Resume later with: "
                                     f"bd-archive.py burn -w {work_dir} "
                                     f"--start {i}")
                            sys.exit(1)
                finally:
                    dio.umount(mount_dir)
                    mount_dir.rmdir()
            else:
                log.warn("Could not mount — verify manually")
                mount_dir.rmdir()

        dio.eject()
        log.ok(f"Disc {i}/{disc_count} done")

        # Show resume hint if not last disc
        if i < disc_count:
            remaining = disc_count - i
            log.info(f"{remaining} disc(s) remaining. "
                     f"Resume: bd-archive.py burn -w {work_dir} --start {i + 1}")

    log.step("All discs burned")
    print(f"\n  Archive:  {archive_name}")
    print(f"  Discs:    {disc_count} x BD-{disc_size}")
    print(f"  Cleanup:  rm -rf {work_dir}\n")
```

with this version:

```python
def cmd_burn(args):
    check_deps("growisofs", "dvd+rw-mediainfo")

    work_dir = Path(args.workdir)
    staging_root = work_dir / "staging"

    if not staging_root.is_dir():
        log.error(f"No staging directory found at {staging_root}")
        log.info("Run 'create' first to prepare the archive.")
        sys.exit(1)

    disc_dirs = sorted(
        d for d in staging_root.iterdir()
        if d.is_dir() and d.name.startswith("disc_")
    )
    disc_count = len(disc_dirs)
    if disc_count == 0:
        log.error(f"No disc_* subdirectories under {staging_root}")
        log.info("Run 'create' first to prepare the archive.")
        sys.exit(1)

    # Derive archive name from the first non-catalog .dar in disc_1.
    # Filename is "<name>.NNN.dar"; strip the slice number.
    first_disc = staging_root / "disc_1"
    try:
        first_dar = next(p for p in first_disc.glob("*.dar")
                         if "-catalog" not in p.name)
    except StopIteration:
        log.error(f"No dar slice found in {first_disc}")
        sys.exit(1)
    archive_name = first_dar.stem.rsplit(".", 1)[0]

    start = args.start
    if start < 1 or start > disc_count:
        log.error(f"--start must be between 1 and {disc_count}")
        sys.exit(1)

    dio = DiscIO(args.device)

    log.step("Burn staged discs")
    log.info(f"Archive:  {archive_name}")
    log.info(f"Discs:    {disc_count}")
    log.info(f"Device:   {args.device}")
    if start > 1:
        log.info(f"Resuming from disc {start}")

    for i in range(start, disc_count + 1):
        stage = staging_root / f"disc_{i}"
        if not stage.exists():
            log.error(f"Staging directory not found: {stage}")
            log.info("Run 'create' first to prepare the archive.")
            sys.exit(1)

        log.step(f"Disc {i}/{disc_count}")

        stage_size = sum(f.stat().st_size for f in stage.iterdir()
                         if f.is_file())
        log.info(f"Size: {human_bytes(stage_size)}")

        prompt_disc(f"Insert blank disc {i}/{disc_count}", args.device)

        # Pre-burn fit check
        if not args.skip_fit_check:
            actual = detect_disc_capacity(args.device)
            if actual is None:
                log.warn("Could not detect disc capacity — skipping fit check")
            elif actual < stage_size:
                log.error(
                    f"Disc too small: {human_bytes(actual)} < "
                    f"staging {human_bytes(stage_size)}"
                )
                log.info(f"Resume later with: bd-archive.py burn "
                         f"-w {work_dir} --start {i}")
                sys.exit(1)
            elif actual > stage_size * 1.05:
                log.error(
                    f"Disc too large: {human_bytes(actual)} > "
                    f"{human_bytes(stage_size)} + 5% — refusing to "
                    f"waste space"
                )
                log.info("Insert a smaller disc, or pass --skip-fit-check "
                         "to override.")
                log.info(f"Resume later with: bd-archive.py burn "
                         f"-w {work_dir} --start {i}")
                sys.exit(1)
            else:
                log.ok(f"Disc capacity {human_bytes(actual)} fits "
                       f"staging {human_bytes(stage_size)}")

        # Burn
        log.info("Burning...")
        dio.burn(stage, f"{archive_name}_{i}", args.speed)
        log.ok(f"Disc {i} burned")

        # Post-burn verify
        if not args.no_verify:
            log.info("Post-burn verification...")
            time.sleep(5)
            mount_dir = Path(tempfile.mkdtemp(prefix="bd-verify-"))
            if dio.mount(mount_dir):
                try:
                    result = verify_disc(mount_dir,
                                         f"Disc {i} (post-burn)", quiet=True)
                    if result == VerifyResult.BROKEN:
                        log.error("Post-burn verification failed!")
                        if not prompt_yn("Continue?", default_yes=False):
                            log.info(f"Resume later with: "
                                     f"bd-archive.py burn -w {work_dir} "
                                     f"--start {i}")
                            sys.exit(1)
                finally:
                    dio.umount(mount_dir)
                    mount_dir.rmdir()
            else:
                log.warn("Could not mount — verify manually")
                mount_dir.rmdir()

        dio.eject()
        log.ok(f"Disc {i}/{disc_count} done")

        if i < disc_count:
            remaining = disc_count - i
            log.info(f"{remaining} disc(s) remaining. "
                     f"Resume: bd-archive.py burn -w {work_dir} "
                     f"--start {i + 1}")

    log.step("All discs burned")
    print(f"\n  Archive:  {archive_name}")
    print(f"  Discs:    {disc_count}")
    print(f"  Cleanup:  rm -rf {work_dir}\n")
```

Key differences from the old version:
- No `load_metadata()` call. `disc_count` and `archive_name` are derived from the staging directory.
- No `disc_size` (BD-25 label) in prompts/logs — uses plain "Disc N/M" and `human_bytes`.
- Fit check inserted after `prompt_disc` and before `dio.burn`.
- `check_deps` adds `dvd+rw-mediainfo`.

- [ ] **Step 3: Smoke-test `cmd_burn` argument parsing**

```bash
./bd-archive.py burn -h
```

Expected: help output shows `--skip-fit-check`.

- [ ] **Step 4: Smoke-test `cmd_burn` startup against the workdir from Task 2**

The workdir `/tmp/bda-work` from Task 2 step 11 still has staging dirs. Burn won't actually run because there's no disc, but the metadata derivation and dependency check should work up to the point where it prompts for the disc.

```bash
echo q | ./bd-archive.py burn -w /tmp/bda-work -D /dev/null 2>&1 | head -10
```

Expected: log shows `Archive:  test`, `Discs:    2`, then prompts to insert disc and exits because of the `q` input. No mention of metadata loading errors.

- [ ] **Step 5: Smoke-test missing-staging error**

```bash
mkdir -p /tmp/bda-empty
./bd-archive.py burn -w /tmp/bda-empty 2>&1 | head -3
```

Expected: `[ ERR]  No staging directory found at /tmp/bda-empty/staging`, exit 1.

- [ ] **Step 6: Commit**

```bash
git add bd-archive.py
git commit -m "Refactor cmd_burn to derive metadata from staging and add fit check."
```

---

## Task 4: Remove the now-dead metadata code and bump VERSION

**Files:**
- Modify: `bd-archive.py`

After Task 3, `save_metadata` is the only thing still writing `bd-archive.json` and `load_metadata` has no callers. Plus `import json` and `METADATA_FILE` are orphans. Sweep them up and bump the version.

- [ ] **Step 1: Remove `save_metadata` call from `cmd_create`**

Find in `cmd_create`:

```python
    # ── Save metadata ───────────────────────────────────────────────────
    source_size = sum(f.stat().st_size for f in source.rglob("*")
                      if f.is_file())
    save_metadata(
        work_dir,
        version=VERSION,
        archive_name=args.name,
        source=str(source),
        source_size=source_size,
        archive_size=total_archive,
        disc_count=slice_count,
        disc_bytes=disc_bytes,
        redundancy=args.redundancy,
        compression=args.compression,
        comp_level=args.level,
        created=datetime.now().isoformat(),
    )
```

Replace with (drop the `save_metadata` call, keep `source_size` since the Summary block uses it):

```python
    # ── Compute source size for summary ─────────────────────────────────
    source_size = sum(f.stat().st_size for f in source.rglob("*")
                      if f.is_file())
```

- [ ] **Step 2: Remove the metadata helpers and constant**

Find and delete this entire block (the section header comment, both functions, and the trailing blank lines):

```python
# ════════════════════════════════════════════════════════════════════════════
# Metadata — persisted in workdir so burn knows what create prepared
# ════════════════════════════════════════════════════════════════════════════

def save_metadata(work_dir: Path, **kwargs):
    meta_path = work_dir / METADATA_FILE
    meta_path.write_text(json.dumps(kwargs, indent=2) + "\n")


def load_metadata(work_dir: Path) -> dict:
    meta_path = work_dir / METADATA_FILE
    if not meta_path.exists():
        log.error(f"No {METADATA_FILE} found in {work_dir}")
        log.info("Run 'create' first to prepare the archive.")
        sys.exit(1)
    return json.loads(meta_path.read_text())
```

Find and delete the standalone constant line:

```python
METADATA_FILE = "bd-archive.json"
```

Find and delete the `import json` line at the top:

```python
import json
```

- [ ] **Step 3: Bump VERSION**

Find:

```python
VERSION = "3.0.0"
```

Replace with:

```python
VERSION = "4.0.0"
```

- [ ] **Step 4: Smoke-test that the script still parses and runs `--version`**

```bash
./bd-archive.py --version
```

Expected: `bd-archive 4.0.0`.

- [ ] **Step 5: Smoke-test full create→burn-startup flow end-to-end**

```bash
rm -rf /tmp/bda-work
./bd-archive.py create -s /tmp/bda-src -n test -w /tmp/bda-work -b 4194304 -r 5 -c none
```

Expected: completes without error. **No** `bd-archive.json` in `/tmp/bda-work`:

```bash
ls /tmp/bda-work
```

Expected: only `dar/` and `staging/` directories — no `bd-archive.json`.

```bash
echo q | ./bd-archive.py burn -w /tmp/bda-work -D /dev/null 2>&1 | head -10
```

Expected: still works — `Archive: test`, `Discs: 2`, prompts for disc.

- [ ] **Step 6: Smoke-test create's manual mode**

```bash
rm -rf /tmp/bda-work
./bd-archive.py create -s /tmp/bda-src -n manual -w /tmp/bda-work -b 8388608 -r 10 -c none 2>&1 | head -20
```

Expected: log line `[INFO]  Using manual capacity: 8.0 MiB`, configuration block, slices created.

- [ ] **Step 7: Smoke-test create's detection-failure error path**

```bash
rm -rf /tmp/bda-work-fail
./bd-archive.py create -s /tmp/bda-src -n fail -w /tmp/bda-work-fail -D /dev/null 2>&1 | tail -5
```

Expected: `[ ERR]  No disc detected at /dev/null.` and the manual hint.

- [ ] **Step 8: Clean up smoke-test artifacts**

```bash
rm -rf /tmp/bda-src /tmp/bda-work /tmp/bda-work-fail /tmp/bda-empty
```

- [ ] **Step 9: Commit**

```bash
git add bd-archive.py
git commit -m "Remove bd-archive.json metadata and bump VERSION to 4.0.0."
```

---

## Task 5: Final integration check on real hardware

**Files:**
- None (verification only)

This task is the user's hands-on verification. The agent runs through the checklist and reports findings; the user supplies the disc.

- [ ] **Step 1: Capacity detection on real disc**

With a blank BD-R inserted in `/dev/sr0`:

```bash
./bd-archive.py create -s <real-source> -n integration -w /tmp/bda-int -r 5 -c zstd
```

Expected:
- Log shows `[INFO]  Detected XX.X GiB free space, splitting with this size` with a value close to the disc spec (e.g. ~23 GiB for BD-25, ~46 GiB for BD-50).
- Configuration block uses `human_bytes` (no `BD-25` text).
- Staging dirs created.

- [ ] **Step 2: Pre-burn fit check on the matching disc**

With a blank disc of the same size still inserted:

```bash
./bd-archive.py burn -w /tmp/bda-int
```

When prompted, press Enter. Expected:
- `[  OK]  Disc capacity X.X GiB fits staging Y.Y GiB`
- Burn proceeds.

If you don't want to burn for real, abort with `q` after seeing the fit-check pass message.

- [ ] **Step 3: Pre-burn fit check rejects wrong-size disc (optional)**

If you have both a BD-25 and BD-50 (or BD-100): create with the smaller one, then attempt to burn with the larger one inserted.

Expected: `[ ERR]  Disc too large: ... > ... + 5% — refusing to waste space`, exit 1.

- [ ] **Step 4: `--skip-fit-check` overrides the rejection (optional)**

```bash
./bd-archive.py burn -w /tmp/bda-int --skip-fit-check
```

Expected: no fit-check logs, burn proceeds (or reaches prompt).

- [ ] **Step 5: Report results to user**

Summarize what was tested, what passed, what wasn't tested (and why). No commit unless something failed and a fix was needed.

---

## Self-Review

**Spec coverage:**
- "Auto-detect raw writable disc capacity at create" → Tasks 1, 2.
- "Manual override via -b/--bytes" → Task 2 step 9, smoke test step 11.
- "Pre-burn fit check (5% tolerance)" → Task 3 step 2, smoke test in Task 5 step 3.
- "--skip-fit-check escape hatch" → Task 3 step 1, smoke test in Task 5 step 4.
- "Remove bd-archive.json and DISC_CAPACITY" → DISC_CAPACITY in Task 2 step 1, JSON in Task 4 steps 1-2.
- "README shows actual capacity bytes" → Task 2 step 2 (`generate_readme` rewrite), smoke test in Task 2 step 11.
- "VERSION bumps to 4.0.0" → Task 4 step 3.
- "check_deps adds dvd+rw-mediainfo" → Task 1 (helper added), Task 2 step 3 (cmd_create), Task 3 step 2 (cmd_burn).
- "Detection failure: hard at create, soft at burn" → Task 2 step 3 (hard error), Task 3 step 2 (warn-and-skip).

**Placeholder scan:** No TBD/TODO/"add appropriate"/etc. Every code-changing step shows the exact code.

**Type/identifier consistency:**
- `detect_disc_capacity(device: str) -> int | None` declared in Task 1, called with `args.device` in both Task 2 and Task 3 — consistent.
- `disc_bytes` used as the post-2-MiB-overhead capacity throughout `cmd_create`, `generate_readme`, the README format string. Same name in all three places.
- `staging_root` and `disc_dirs` introduced in Task 3 step 2 only, no cross-task references.
- `args.bytes` from `-b/--bytes` flag (Task 2 step 9) used in Task 2 step 3 — consistent.
- `args.skip_fit_check` from `--skip-fit-check` (Task 3 step 1) used in Task 3 step 2 — consistent (argparse maps `--skip-fit-check` → `args.skip_fit_check`).
