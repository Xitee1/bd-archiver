# Merge `estimate` into `create` Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate the standalone `estimate` subcommand by folding its preview output and `--sample`/`--ratio` flags into `create`, with a confirmation prompt before any heavy work runs.

**Architecture:** `cmd_create` reorders so all cheap planning (capacity detection, source scan, slice sizing, optional sample-based ratio measurement) happens up-front and is presented to the user as an estimate-style block. A `prompt_yn("Proceed?")` then gates the actual dar/par2/mkisofs run. `--yes` skips the prompt for non-interactive use. The shared `2 * MiB` vs `DISC_END_MARGIN = 1 MiB` inconsistency between the two commands is unified on `DISC_END_MARGIN`. `commands/estimate.py` and the `estimate` subparser/dispatch case are deleted; `measure_compression_ratio` stays where it is in `archive/sizing.py`.

**Tech Stack:** Python 3.11, argparse, existing `bd_archive.ui.prompts.prompt_yn`, existing `archive/sizing.py` and `archive/source_scan.py`. No new dependencies. No tests in this project — verification is manual via `bd-archive` CLI runs and `ruff check`.

**Project conventions to honor:**
- Always use `.venv/bin/<tool>` (project venv only).
- All user-facing output via `ui.logger.Logger` (`log.info/ok/warn/error/step/banner`), never bare `print()` for status.
- Layering rule: `commands/` → `archive/` → `tools/` → `shell/`. `ui/` is a leaf usable anywhere.
- Tool wrappers stay thin; domain decisions belong in `archive/` or `commands/`.
- Bump `bd_archive.__version__` only when on-disc layout changes — this change does not, so leave it alone.

---

## Task 1: Add `--sample`, `--ratio`, `--yes` flags to `create` subparser

**Files:**
- Modify: `src/bd_archive/cli.py:22-47` (the `cr = sub.add_parser("create", ...)` block)

**Step 1: Add the three new args to the create parser**

Edit `src/bd_archive/cli.py`. Inside the `cr` block, immediately after the `cr.add_argument("-l", "--level", ...)` line at `src/bd_archive/cli.py:46-47`, add:

```python
    ratio_group = cr.add_mutually_exclusive_group()
    ratio_group.add_argument("--ratio", type=float, default=None,
                             help="Manual compression ratio "
                                  "(1.0 = none, 0.5 = 50%% reduction). "
                                  "Used for the disc-count preview only. "
                                  "Default: 1.0 if --sample also omitted")
    ratio_group.add_argument("--sample", default=None,
                             help="Run dar on this directory with -c/-l "
                                  "and use the measured output/input ratio "
                                  "for the disc-count preview")
    cr.add_argument("-y", "--yes", action="store_true",
                    help="Skip the pre-archive confirmation prompt")
```

These mirror the estimate subparser's `--ratio`/`--sample` (`src/bd_archive/cli.py:62-69`) so the help text is consistent.

**Step 2: Verify the parser still loads and shows the new flags**

Run: `.venv/bin/bd-archive create --help`

Expected output includes lines for `--ratio`, `--sample`, and `-y, --yes`. The mutually-exclusive pair shows up as separate help entries (argparse renders them individually).

If `.venv/` does not exist yet, create it first per CLAUDE.md:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

**Step 3: Lint**

Run: `.venv/bin/ruff check src/bd_archive/cli.py`
Expected: clean (no findings).

**Step 4: Commit**

```bash
git add src/bd_archive/cli.py
git commit -m "feat(create): add --sample, --ratio, --yes flags

Mirrors the estimate subparser's preview-related args. Wired up
in cmd_create in the next commit."
```

---

## Task 2: Reorder `cmd_create` to do all planning before any side effects, then prompt

**Files:**
- Modify: `src/bd_archive/commands/create.py` (rewrite the top half)
- Reference (do not modify): `src/bd_archive/commands/estimate.py` (the math/output to fold in)
- Reference (do not modify): `src/bd_archive/archive/sizing.py:22-60` (`measure_compression_ratio`)
- Reference (do not modify): `src/bd_archive/ui/prompts.py:16-19` (`prompt_yn`)
- Reference (do not modify): `src/bd_archive/constants.py` for `DISC_END_MARGIN`, `MiB`, `PAR2_AND_MISC_OVERHEAD`

**Background — what changes vs. the current `cmd_create` at `src/bd_archive/commands/create.py:24-99`:**

1. The "Configuration" section (currently emitted at lines 89-99) is split into two: a **Source/Disc-layout** preview block (estimate-style) printed *before* the prompt, and a **Configuration** block printed after, just before dar starts.
2. After the preview, `prompt_yn("Proceed with creation?")` runs (unless `args.yes`).
3. On `n`, clean up empty default workdir + output dirs and `sys.exit(0)`.
4. Compression-ratio source: `--sample` → `measure_compression_ratio(...)`; `--ratio` → manual; else 1.0. Same precedence as `cmd_estimate` at `src/bd_archive/commands/estimate.py:54-64`.
5. Workdir is created **before** the sample run (so `--sample` writes its tempdir into `-w` if supplied — required for tmpfs consistency).
6. `compute_slice_bytes` already uses `sizing_target = raw_capacity - DISC_END_MARGIN` (`src/bd_archive/commands/create.py:67`). Estimate's `2 * MiB` margin is the inconsistency — fixed in Task 3 by deleting estimate.py outright, no action needed here.

**Step 1: Update imports**

In `src/bd_archive/commands/create.py:1-21`, add to the existing imports:

- Add `from bd_archive.archive.sizing import compute_slice_bytes, measure_compression_ratio` (currently only `compute_slice_bytes` is imported on line 10 — extend the same line).
- Add `from bd_archive.ui.prompts import prompt_yn` (new import line, place after the existing `from bd_archive.ui.logger import log` at line 21).

**Step 2: Rewrite the body of `cmd_create` between the dep check and the dar invocation**

Replace `src/bd_archive/commands/create.py:24-99` (everything from `def cmd_create(args):` through the end of the existing "Configuration" log block, i.e. the line `log.info(f"Workdir:       {work_dir}" ...)` and its continuation) with the structure below. The `dar_archive = DarArchive(cfg.name, work_dir)` line at the current `src/bd_archive/commands/create.py:101` and everything after it stays untouched.

```python
def cmd_create(args):
    check_deps("dar", "par2", "mkisofs", "dvd+rw-mediainfo")

    max_name_len = ISO9660_VOLUME_LABEL_MAX - 5  # "_NNNN" suffix
    if len(args.name) > max_name_len:
        log.error(f"--name '{args.name}' is {len(args.name)} chars; "
                  f"max {max_name_len} (ISO9660 volume label limit "
                  f"{ISO9660_VOLUME_LABEL_MAX} minus 5-char disc suffix)")
        sys.exit(1)

    source = Path(args.source).resolve()
    if not source.is_dir():
        log.error(f"Does not exist: {source}")
        sys.exit(1)

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
        log.info(f"Detected {human_bytes(raw_capacity)} writable, "
                 f"sizing ISOs accordingly")

    # raw_capacity is the format-aware writable extent (post-OSA
    # reservation). DISC_END_MARGIN reserves a tiny bit more to absorb
    # ISO9660+UDF metadata growth that exceeds compute_slice_bytes's
    # estimate; the ISO file size is then re-checked against the full
    # raw_capacity below as the hard limit.
    sizing_target = raw_capacity - DISC_END_MARGIN

    log.info("Scanning source...")
    scan = scan_source(source)

    slice_bytes = compute_slice_bytes(sizing_target, scan.catalog_est,
                                      args.redundancy)
    if slice_bytes == 0:
        log.error(f"Per-disc overhead "
                  f"({human_bytes(scan.catalog_est + PAR2_AND_MISC_OVERHEAD)}) "
                  f"exceeds disc capacity ({human_bytes(raw_capacity)})")
        sys.exit(1)

    # Workdir must exist before --sample so the sample tempdir lives
    # in the user-chosen location (e.g. tmpfs). Default-pathed workdir
    # also implies output_dir/images_dir creation here, since the
    # default workdir lives inside output_dir.
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    workdir_is_default = args.workdir is None
    work_dir = (Path(args.workdir) if args.workdir
                else output_dir / ".bd-archive-work")
    work_dir.mkdir(parents=True, exist_ok=True)

    # ── Compression-ratio preview ───────────────────────────────────────
    if args.sample:
        ratio = measure_compression_ratio(
            Path(args.sample).resolve(), args.compression, args.level,
            work_dir)
        ratio_source = f"measured from {args.sample}"
    elif args.ratio is not None:
        ratio = args.ratio
        ratio_source = "manual"
    else:
        ratio = 1.0
        ratio_source = "default (no compression assumed)"

    archive_est = int(scan.total_bytes * ratio)
    n_discs = max(1, (archive_est + slice_bytes - 1) // slice_bytes)
    last_slice = archive_est - (n_discs - 1) * slice_bytes
    if last_slice == 0:
        last_slice = slice_bytes
    last_disc_content = (
        last_slice
        + last_slice * args.redundancy // 100
        + scan.catalog_est
        + PAR2_AND_MISC_OVERHEAD
    )
    last_disc_free = max(0, sizing_target - last_disc_content)
    last_disc_free_raw = int(last_disc_free / max(ratio, 0.001))

    par2_est = slice_bytes * args.redundancy // 100
    cfg = ArchiveConfig(
        name=args.name,
        disc_bytes=raw_capacity,
        redundancy=args.redundancy,
        compression=args.compression,
        comp_level=args.level,
    )

    log.step("Source")
    log.info(f"Path:             {source}")
    log.info(f"Size:             {human_bytes(scan.total_bytes)} "
             f"({scan.entry_count} entries)")
    log.info(f"Catalog:          ~{human_bytes(scan.catalog_est)} (estimated)")

    log.step("Disc layout")
    log.info(f"Disc capacity:    {human_bytes(raw_capacity)} (writable)")
    log.info(f"Slice size:       {human_bytes(slice_bytes)}")
    log.info(f"PAR2 redundancy:  {cfg.redundancy}% (~{human_bytes(par2_est)})")
    log.info(f"Compression:      {cfg.comp_str} (ratio {ratio:.3f}, "
             f"{ratio_source})")
    log.info(f"Estimated archive: {human_bytes(archive_est)}")

    log.step("Estimate")
    fill_pct = last_disc_content * 100 // sizing_target
    log.info(f"Discs needed:     {n_discs}")
    log.info(f"Last disc fill:   {human_bytes(last_disc_content)} / "
             f"{human_bytes(sizing_target)}  ({fill_pct}%)")
    log.info(f"Free on last:     {human_bytes(last_disc_free)} archive")
    if abs(ratio - 1.0) > 0.001:
        log.info(f"                  ~{human_bytes(last_disc_free_raw)} raw "
                 f"(at ratio {ratio:.3f})")

    log.step("Configuration")
    log.info(f"Source:        {source}")
    log.info(f"Output:        {output_dir}")
    log.info(f"Workdir:       {work_dir}"
             f"{' (default)' if workdir_is_default else ' (custom)'}")

    if not args.yes:
        if not prompt_yn("Proceed with creation?"):
            log.warn("Cancelled by user")
            if workdir_is_default:
                with contextlib.suppress(OSError):
                    work_dir.rmdir()
                with contextlib.suppress(OSError):
                    images_dir.rmdir()
                with contextlib.suppress(OSError):
                    output_dir.rmdir()
            sys.exit(0)

    dar_archive = DarArchive(cfg.name, work_dir)
    tmp_dir = dar_archive.tmp_dir
```

Everything from `src/bd_archive/commands/create.py:104` onward (the `# ── Create dar archive ──` comment and below) stays exactly as it is.

**Step 3: Remove the now-duplicated old "Configuration" block**

After the rewrite in Step 2, the old `log.step("Configuration")` block at the original lines 89-99 should already be gone (replaced by the new Source/Disc layout/Estimate/Configuration sequence). Re-read the file to confirm there's exactly one `log.step("Configuration")` call in `cmd_create`, and it sits *before* the prompt.

**Step 4: Verify the rewrite parses and runs (cancel path)**

Run a non-destructive test that exercises the new prompt path with manual capacity (no drive needed) and sample skipped, then answer `n`:

```bash
mkdir -p /tmp/bd-create-test/src
echo "hello" > /tmp/bd-create-test/src/a.txt
echo "world" > /tmp/bd-create-test/src/b.txt
echo "n" | .venv/bin/bd-archive create \
    -s /tmp/bd-create-test/src \
    -n test-archive \
    -o /tmp/bd-create-test/out \
    -b 26843531264
```

Expected output includes:
- `Scanning source...`
- A `Source` step with size + entry count
- A `Disc layout` step with slice size + ratio `1.000`
- An `Estimate` step with `Discs needed: 1`
- A `Configuration` step
- `Proceed with creation? (Y/n):` prompt
- `Cancelled by user`

Then verify cleanup:
```bash
ls /tmp/bd-create-test/out 2>&1
```
Expected: `ls: cannot access '/tmp/bd-create-test/out': No such file or directory` — the empty default workdir/output should have been removed.

**Step 5: Verify the `-y` skip path runs end-to-end**

```bash
echo "" | .venv/bin/bd-archive create \
    -s /tmp/bd-create-test/src \
    -n test-archive \
    -o /tmp/bd-create-test/out \
    -b 26843531264 \
    -y
```

Expected: no prompt, full pipeline runs (dar, par2, mkisofs), summary printed. The `Proceed with creation?` line must NOT appear. Final `/tmp/bd-create-test/out/images/disc_0001.iso` exists.

Cleanup: `rm -rf /tmp/bd-create-test`

**Step 6: Lint**

Run: `.venv/bin/ruff check src/bd_archive/commands/create.py`
Expected: clean.

**Step 7: Commit**

```bash
git add src/bd_archive/commands/create.py
git commit -m "feat(create): preview disc count + prompt before running

Adds an estimate-style preview block (source size, disc layout,
disc count, last-disc fill) before any heavy work runs. Gates
the dar/par2/mkisofs pipeline behind prompt_yn unless --yes is
passed. --sample runs dar on a subset to measure compression ratio
for the preview; --ratio supplies one manually; otherwise 1.0.

The standalone 'estimate' subcommand becomes redundant and is
removed in the next commit."
```

---

## Task 3: Delete the `estimate` subcommand

**Files:**
- Delete: `src/bd_archive/commands/estimate.py`
- Modify: `src/bd_archive/cli.py` (remove import, subparser, dispatch case)

**Step 1: Delete the estimate command module**

```bash
rm src/bd_archive/commands/estimate.py
```

**Step 2: Remove the estimate import from cli.py**

Edit `src/bd_archive/cli.py`. Delete the line:

```python
from bd_archive.commands.estimate import cmd_estimate
```

(currently `src/bd_archive/cli.py:6`).

**Step 3: Remove the estimate subparser block**

Edit `src/bd_archive/cli.py`. Delete the entire estimate subparser block from the `# ── estimate ───...` comment through the last `es.add_argument(...)` call (currently `src/bd_archive/cli.py:49-77`, ending with the `es.add_argument("-w", "--workdir", ...)` block whose help mentions "Directory for sample tempdir").

**Step 4: Remove the estimate dispatch case**

Edit `src/bd_archive/cli.py`. In the `match args.command:` block (currently `src/bd_archive/cli.py:123-128`), delete the line:

```python
        case "estimate": cmd_estimate(args)
```

**Step 5: Verify the parser still loads and `estimate` is gone**

Run: `.venv/bin/bd-archive --help`
Expected: subcommand list shows `create`, `burn`, `verify`, `extract` — but NOT `estimate`.

Run: `.venv/bin/bd-archive estimate --help`
Expected: argparse error `invalid choice: 'estimate'`.

**Step 6: Lint**

Run: `.venv/bin/ruff check src/`
Expected: clean.

**Step 7: Commit**

```bash
git add -A src/bd_archive/cli.py src/bd_archive/commands/
git commit -m "refactor(cli): remove standalone estimate subcommand

Its preview and --sample/--ratio functionality is now part of
'create' itself, so a separate command would just duplicate code."
```

---

## Task 4: Update CLAUDE.md and README.md

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

**Step 1: Update CLAUDE.md command-line summary**

Edit `CLAUDE.md`. In the "Running" section, locate the four-line command summary block (the one starting with `bd-archive create   -s <source> ...`).

- Update the `create` line to include the new flags. Replace:
  ```
  bd-archive create   -s <source> -n <name> -o <output> [-w <workdir>] [-D /dev/sr0] [-b BYTES] [-r %] [-c zstd|lzma|...] [-l <level>]
  ```
  with:
  ```
  bd-archive create   -s <source> -n <name> -o <output> [-w <workdir>] [-D /dev/sr0] [-b BYTES] [-r %] [-c zstd|lzma|...] [-l <level>] [--ratio R | --sample <path>] [-y]
  ```
- Delete the entire `bd-archive estimate ...` line.

**Step 2: Update CLAUDE.md architecture description**

In CLAUDE.md, locate the paragraph "Four subcommands form a pipeline; `estimate` is a side-tool that previews the same math without running anything." and rewrite to:

> Four subcommands form a pipeline. `create` previews disc count + last-disc fill before prompting for confirmation, so users can dry-run sizing without committing.

Then in the numbered architecture list (the section "Architecture (v5: build-then-burn separation)"), delete the entire item 5 about `estimate` (the paragraph starting with "**`estimate`** (`commands/estimate.py`) walks the source...") since estimate no longer exists.

In item 1 about `create`, add a sentence at the start: "Reads disc capacity, scans the source, computes slice sizing and a disc-count estimate (optionally measuring the compression ratio via `--sample`), then prompts the user via `prompt_yn` before any heavy work begins (skip with `-y`)."

**Step 3: Update CLAUDE.md package-layout block**

In the "Package layout" section, delete the line:
```
    ├── estimate.py
```
from inside the `commands/` listing.

**Step 4: Update README.md command list**

Edit `README.md`. In the bulleted subcommand summary near the top (currently `README.md:5-11`), delete the line:
```
- `estimate` — Preview disc count and per-disc fill without running dar/par2.
```
And change "Five subcommands form a build-then-burn pipeline:" to "Four subcommands form a build-then-burn pipeline:".

**Step 5: Update README.md create section**

In the create options table (`README.md:49-59`), add three rows after the `-l, --level` row:

```
| `--ratio`          | —                 | Manual compression ratio for the disc-count preview (1.0 = none, 0.5 = 50% reduction). Mutually exclusive with `--sample`. |
| `--sample <path>`  | —                 | Run dar on this subset with `-c/-l` and use the measured ratio for the preview. Mutually exclusive with `--ratio`. |
| `-y, --yes`        | off               | Skip the pre-archive confirmation prompt. |
```

Add a sentence after the table (before the "After completion, ISOs sit in..." paragraph at `README.md:61`):

> `create` prints a disc-count + last-disc-fill preview and asks for confirmation before running. Pass `-y` to skip the prompt for scripts.

**Step 6: Delete the estimate section from README.md**

Delete the entire `### estimate` section (currently `README.md:67-76`, from the heading through the bulleted list of ratio sources).

**Step 7: Update README.md project-structure block**

In the project structure tree (`README.md:126-156`), delete the line:
```
    ├── estimate.py
```
inside the `commands/` listing.

**Step 8: Verify no stale references remain**

Run: `.venv/bin/python -c "import bd_archive.cli; bd_archive.cli.build_parser().parse_args(['--help'])"` — should print help and exit cleanly (or use `bd-archive --help`, both work).

Then grep for stale references (these should all return no results except in the new plan file itself):

```bash
grep -rn "cmd_estimate\|estimate.py" src/ README.md CLAUDE.md
```

Expected: no matches in `src/`, `README.md`, or `CLAUDE.md`. Matches in `docs/plans/` are fine.

**Step 9: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: reflect estimate→create merge

Remove standalone estimate from command list, add --sample/--ratio/-y
to create's docs, mention the new confirmation prompt."
```

---

## Task 5: Final integration check

**Files:** none modified

**Step 1: Full lint pass**

Run: `.venv/bin/ruff check src/`
Expected: clean.

**Step 2: Help output sanity**

Run: `.venv/bin/bd-archive --help`
Expected: four subcommands listed — `create`, `burn`, `verify`, `extract`. No `estimate`.

Run: `.venv/bin/bd-archive create --help`
Expected: shows `-s, -n, -o, -w, -r, -D, -b, -c, -l, --ratio, --sample, -y`. The mutually-exclusive group displays `--ratio` and `--sample` as separate entries (argparse default rendering).

**Step 3: End-to-end smoke test (cancel + proceed paths)**

```bash
mkdir -p /tmp/bd-smoke/src
for i in 1 2 3 4 5; do
  dd if=/dev/urandom of=/tmp/bd-smoke/src/file$i.bin bs=1M count=10 status=none
done
```

Cancel path:
```bash
echo "n" | .venv/bin/bd-archive create -s /tmp/bd-smoke/src -n smoke \
    -o /tmp/bd-smoke/out -b 26843531264
ls /tmp/bd-smoke/out 2>&1   # should NOT exist
```

Proceed path with `-y`:
```bash
.venv/bin/bd-archive create -s /tmp/bd-smoke/src -n smoke \
    -o /tmp/bd-smoke/out -b 26843531264 -y
ls /tmp/bd-smoke/out/images/   # should contain disc_0001.iso
```

`--sample` path (uses the same source as the sample for simplicity):
```bash
rm -rf /tmp/bd-smoke/out
.venv/bin/bd-archive create -s /tmp/bd-smoke/src -n smoke \
    -o /tmp/bd-smoke/out -b 26843531264 \
    --sample /tmp/bd-smoke/src -c zstd -y
```
Expected: a `Test-compressing 50 MiB sample with zstd...` line appears, followed by `Measured ratio X.XXX`. Run completes; `disc_0001.iso` is created.

`--sample` writes its tempdir into the workdir — verify it lives at the right path. With `-w /tmp/bd-smoke/wd`:
```bash
rm -rf /tmp/bd-smoke/out /tmp/bd-smoke/wd
.venv/bin/bd-archive create -s /tmp/bd-smoke/src -n smoke \
    -o /tmp/bd-smoke/out -w /tmp/bd-smoke/wd -b 26843531264 \
    --sample /tmp/bd-smoke/src -y
```
Expected: pipeline runs to completion. The custom `-w` is preserved (not auto-removed); run `ls /tmp/bd-smoke/wd` and confirm it exists but no leftover `bd-sample-*` tempdirs (those are auto-cleaned by `tempfile.TemporaryDirectory`).

Cleanup: `rm -rf /tmp/bd-smoke`

**Step 4: Confirm no missing migration**

```bash
grep -rn "estimate" src/ README.md CLAUDE.md
```

Expected: zero matches in those three locations. (Matches in `docs/plans/` and git history are expected.)

**Step 5: Done — no commit needed**

This task only verifies; no files changed.

---

## Out of scope

These are intentionally NOT in this plan:

- Tests. The project has none and the user has not asked for any.
- Refactoring `measure_compression_ratio` or `compute_slice_bytes`. They stay as-is.
- Renaming `DISC_END_MARGIN` or changing its value. Estimate's `2 * MiB` value is gone with the file; create's `1 MiB` (the existing `DISC_END_MARGIN`) is the unified value.
- Version bump. On-disc layout doesn't change.
- Adding `--skip-check` / `--no-prompt` aliases for `-y`. One flag is enough.
