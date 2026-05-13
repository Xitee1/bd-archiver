# AGENTS

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Python package (`bd_archive`, Python 3.11+ — uses `match`, `int | None`, etc.) that archives a directory tree onto one or more Blu-ray discs using `dar` (slicing/compression) and `par2` (forward error correction). Built with `hatchling`; installed via `pip install .`, exposes the `bd-archive` console script. No tests.

## Running

```bash
# editable install in a project-local venv (.venv/ is gitignored)
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# or invoke as a module without install (no venv needed for --help)
PYTHONPATH=src python3 -m bd_archive ...
```

```bash
bd-archive create   -s <source> -n <name> -o <output> [-w <workdir>] [-D /dev/srN] [-b BYTES] [-r %] [-c zstd|lzma|...] [-l <level>] [--ratio <float> | --sample <path>] [-y]
bd-archive burn     -i <input> [-D /dev/srN] [--start N] [--no-verify] [--skip-fit-check] [-S <speed>]
bd-archive verify   [<mountpoint|dir|/dev/srN|*.iso>]
bd-archive extract  -o <output> [-D /dev/srN] [-w <workdir>]
```

`-D/--device` is optional everywhere. Omitting it triggers auto-detection via `tools.optical.resolve_device`: scans `/sys/block/sr*`, uses the only drive if there's one, prompts the user if there are multiple, errors out if none. Same for the positional `target` of `verify` — leave it off and the disc in the auto-detected drive is verified.

`-w/--workdir` is optional for `create` and `extract` — it defaults to `<output>/.bd-archive-work/` (hidden, auto-removed on success). Override only when you want scratch on tmpfs/RAM. `burn` reads ISOs from `-i <input>` (the directory `create` wrote to) and has no separate workdir.

External binaries required at runtime, enforced per-subcommand via `check_deps()`:
- `create`: `dar`, `par2`, `mkisofs`, `dvd+rw-mediainfo`
- `burn`: `growisofs`, `dvd+rw-mediainfo`
- `verify`: `par2`, plus `udisksctl` when the target is an `.iso` file (loop-mount)
- `extract`: `dar`, `par2`

`udisksctl` is also used as a Polkit-based mount fallback in `DiscIO.mount` when plain `mount` fails (no permission). `mount`, `umount`, `eject` are NOT enforced — assumed to be present as part of util-linux. `lsof` is optional, used by `tools.lsof.find_device_holders` for diagnostics when the burn device is busy; gracefully no-ops if missing. Python dep: `argcomplete>=3.0` (pulled via `pyproject.toml`, used for shell tab-completion).

`verify` exits with `VerifyResult.value` (0=OK, 1=REPAIRABLE, 2=BROKEN) — useful for scripting. `extract` exits with `1` whenever it wrote a `corrupted-files.txt` (per-file Bad CRC from dar OR slices that failed sha512+par2), `0` on a fully clean restore.

## Package layout

```
src/bd_archive/
├── __init__.py         # __version__ (single source of truth, embedded in burned ISO publisher)
├── __main__.py         # entry point for `python -m bd_archive`
├── _par2_helper.py     # dar -E hook: invoked as `python -m bd_archive._par2_helper ...`
├── cli.py              # argparse + dispatch + top-level exception handling (uniform cancel/error output)
├── constants.py        # MiB, DISC_OVERSIZE_TOLERANCE, PAR2_AND_MISC_OVERHEAD, DISC_END_MARGIN, POST_BURN_MOUNT_TIMEOUT, ISO9660_VOLUME_LABEL_MAX, PAR2_RECOVERY_RE
├── ui/                 # logger, prompts (interactive), progress (byte-counted, TTY-aware)
├── shell/              # runner.py: run() (+ SIGINT handling); deps.py: check_deps(); format.py: human_bytes()
├── tools/              # one thin wrapper per external CLI
│   ├── dar.py          # dar create_sliced/isolate_catalog/compress/extract_sequential (Bad-CRC parser)
│   ├── par2.py         # par2 create/verify/repair (+ VerifyResult, is_par2_index)
│   ├── mkisofs.py      # ISO9660+UDF image build (`-iso-level 3 -udf -V -publisher -input-charset utf-8 -graft-points`)
│   ├── growisofs.py    # burn (+ DeviceBusyError on sg lock, SIGINT double-press abort with BURN_ABORT_GRACE_S=5s)
│   ├── mount.py        # plain mount/umount (no sudo)
│   ├── udisks.py       # is_available, mount/unmount, loop_setup/loop_delete
│   ├── eject.py        # eject + close_tray + drive_status (CDROM ioctl, CDS_* constants)
│   ├── mediainfo.py    # detect_disc_capacity (Free Blocks for write-once, Track Size for rewritables; assumes growisofs spare=none — no format → no OSA)
│   ├── optical.py      # list_drives + resolve_device (sysfs sr* enum, interactive picker)
│   └── lsof.py         # find_device_holders (optional — no-op if lsof absent)
├── archive/            # domain logic over tools/
│   ├── checksums.py    # sha512 verify (verify_slice per-file, used by extract on staging)
│   ├── config.py       # ArchiveConfig, write_readme
│   ├── dar_archive.py  # DarArchive (slices, catalog, work-dir layout)
│   ├── disc.py         # DiscIO (mount/mount_with_retry/umount/eject/close_tray_if_open/burn) + find_sg_device
│   ├── sizing.py       # compute_slice_bytes, measure_compression_ratio
│   ├── source_scan.py  # SourceScan + scan_source
│   └── verify.py       # verify_disc()
└── commands/           # one file per subcommand
    ├── create.py
    ├── burn.py
    ├── verify.py
    └── extract.py
```

Layering: `commands/` → `archive/` → `tools/` → `shell/`. Lower layers never import from higher ones. `ui/` is a leaf shared by all layers.

## Architecture (v5: build-then-burn separation)

Four subcommands form a pipeline. `create` previews disc count + last-disc fill before prompting for confirmation, so users can dry-run sizing without committing.

1. **`create`** (`commands/create.py`) reads disc capacity via `tools.mediainfo.detect_disc_capacity` (or `args.bytes`), scans the source, and computes slice sizing plus a disc-count estimate (optionally measuring the compression ratio via `--sample`). The user confirms via `prompt_yn` before any heavy work begins (skip with `-y`). Then runs `tools.dar.create_sliced` with `--hash sha512 --min-digits 4 -Q` (plus `-z<algo>[:level] -am` when compression is enabled) to slice the source into per-disc-sized `.dar` files in `<workdir>/tmp/`. par2 is generated **inline** via dar's `-E` hook (`bd_archive._par2_helper`) — the hook fires after each slice is fully written, so par2 reads the slice while it is still hot in the OS page cache, eliminating most SSD read traffic of the create phase. After dar completes, the catalog is isolated. For each slice in order: regenerate `README.txt` with the right disc number and call `tools.mkisofs.build` (mkisofs `-iso-level 3 -udf -V <label> -publisher "bd-archive v<ver>" -input-charset utf-8 -graft-points`) to assemble `<output>/images/disc_NNNN.iso` directly from in-place files (no staging copies). The ISO file size is checked against the format-aware writable capacity as a hard limit. Phase 3 also asserts par2 files are present on disk — a missing file means the `-E` helper silently failed during dar create. After each ISO is built, the slice + par2 are deleted from `tmp/`; once all slices are processed, `tmp/` is wiped entirely. If `-w` was not supplied, the default `<output>/.bd-archive-work/` is also removed, so `<output>` ends up containing only `images/disc_*.iso`.
2. **`burn`** (`commands/burn.py`) iterates `<input>/images/disc_*.iso` lexically and burns each via `growisofs -use-the-force-luke=notray -use-the-force-luke=spare=none -dvd-compat -Z dev=image.iso` — a byte-for-byte ISO write. `spare=none` skips the implicit BD-R format and disables drive-firmware defect management (read-after-write + Outer Spare Area reservation), which roughly doubles write speed on BD-R; par2 + sha512 + the post-burn verify pass cover the integrity story at the application layer, no on-the-fly mkisofs, so what's in the ISO file is exactly what ends up on disc. Volume label, publisher, file layout are all already in the file. Pre-burn fit check is **two-sided**: rejects too-small discs AND discs more than `DISC_OVERSIZE_TOLERANCE` (= 5%) larger than the ISO, guarding against wasting a 50 GB BD-DL on a 25 GB-sized archive. `--skip-fit-check` disables both directions. SIGINT is trapped during the burn itself: a first `Ctrl+C` warns and is ignored (cancelling mid-burn coasters the disc), a second within `BURN_ABORT_GRACE_S` (= 5 s) terminates growisofs and bubbles up as `KeyboardInterrupt`. growisofs runs in its own session (`start_new_session=True`) so the tty's SIGINT does not reach it directly. After burn, `DiscIO.close_tray_if_open` pulls the tray back in on auto-ejecting drives so the post-burn verify can mount. The post-burn verify runs `verify_disc` and loops on any mount/verify failure with a `Re-insert the disc … press Enter to retry` prompt, since some drives need a manual re-insert. Resumable via `--start N`; per-disc resume hints are logged on every cancel/error path. Catches `DeviceBusyError` from `tools/growisofs.py` (sg device locked) and offers an interactive retry — `tools.lsof.find_device_holders` is consulted to name the holding processes when available.
3. **`verify`** (`commands/verify.py`) dispatches on target type: block device → mount; directory → check directly; **`.iso` file → loop-mount via `tools.udisks.loop_setup` + check + tear down**. The ISO branch makes pre-burn dry-run trivial: run `create`, then `verify images/disc_0001.iso` to confirm the image is internally consistent before touching media.
4. **`extract`** (`commands/extract.py`) auto-detects the archive name from the first disc's `*.dar` filenames (no `-n` flag), then iterates discs interactively. For each disc: copy slice + sha512 sidecar (and the catalog on its first arrival) to staging in a single disc-read pass — par2 is **not** copied — then eject. The catalog is verified separately via `_verify_catalog_on_staging`: any failing catalog slice is deleted from staging so the **next** disc that carries it can re-fetch it (multi-disc catalogs converge in fewer disc-iterations than a stop-at-first-failure pass). Each slice is then verified via SHA-512 on the local copy. On corruption, the disc is re-mounted, just the par2 files for the affected slice are fetched, `par2 repair` runs in staging, and the slice is re-verified. If par2 cannot recover, the slice is kept as-is and recorded in `unrepairable_slices` — no prompt, no abort. After each disc the user is asked "Insert another disc?", allowing an early stop for partial restores (e.g. one disc lost). Once the user is done, `tools.dar.extract_sequential` does the final pass with a background thread feeding ESC bytes on stdin so dar's "missing slice" prompts auto-skip — a partial slice set still restores ~95% of files. dar 2.7 exits 0 even when per-file CRC errors occur, so the wrapper parses `Error while restoring <path> : Bad CRC` lines into a list. If `corrupted_files` OR `unrepairable_slices` is non-empty, `<output>/corrupted-files.txt` is written (listing both groups, with unrepairable slices flagged as "files originating from these may be corrupt even if dar didn't report them"), and `cmd_extract` exits with code 1 so scripts can detect a non-clean restore. The output dir still contains whatever dar managed to extract — best-effort, never silently corrupt.

**SSD-friendly tip:** pass `-w /dev/shm/bd-extract` (or any tmpfs path) to keep the staging copy in RAM. On a 25 GB-slice + 32 GB-RAM box this means **zero SSD writes for slice payload** during extract. Falls back to SSD staging automatically if `-w` is not given.

The build-then-burn separation makes mid-burn sizing failures **constructively impossible**: the ISO exists and is size-checked before any drive is touched. `burn` is a pure file-to-device copy.

`archive/verify.py:verify_disc` is shared between standalone `verify` (block device / dir / ISO file) and the post-burn check inside `burn`. It is **par2-only**: par2 is self-verifying (each packet carries an MD5), so a single par2 pass catches both slice corruption and damage to the par2 files themselves — running sha512 alongside it would just double the disc-read time without expanding coverage. The `.sha512` sidecars dar emits stay on disc; they're used by `extract`, which goes per-slice via `archive/checksums.py:verify_slice` (sha512 on local staging) and reaches for `tools.par2` only when a slice fails — this avoids reading the disc multiple times and surfaces *which* slice is damaged, not just whether the disc as a whole is repairable.

### Slice sizing (`archive/sizing.py`)

Raw capacity from `tools.mediainfo.detect_disc_capacity` is `Free Blocks` for write-once media (BD-R, DVD-R — the per-burn writable remainder) and falls back to `Track Size` when `Free Blocks` is 0 (rewritable formatted media — DVD+RW, BD-RE, where `Free Blocks` doesn't apply). This assumes the burn step keeps BD-R unformatted via growisofs's `-use-the-force-luke=spare=none` (see `tools/growisofs.py`): without the implicit format step, no Outer Spare Area is reserved, so the nominal `Free Blocks` figure (~25.03 GB on a 25 GB SL BD-R, 12219392 blocks × 2048) is fully writable. If `spare=none` is ever removed, the drive will format with a default OSA (~256 MiB on SL BD-R) and `Free Blocks` will over-report; in that case capacity must instead come from the MMC-6 32h format-type descriptor in `READ FORMAT CAPACITIES`. The two changes are coupled — see commit 43fce62 for the descriptor-walk variant.

`cmd_create` then subtracts `DISC_END_MARGIN = 1 MiB` from this raw capacity to derive `sizing_target` (absorbs ISO9660+UDF metadata growth that exceeds the slice estimate). `compute_slice_bytes(sizing_target, catalog_est, redundancy)` further subtracts `catalog_est + PAR2_AND_MISC_OVERHEAD` per disc (catalog scales with file count via `archive.source_scan.scan_source`; `PAR2_AND_MISC_OVERHEAD = 4 MiB` covers par2 index/packet/block-rounding overhead), divides by `(100 + redundancy)/100`, and floors to a MiB boundary. The post-build ISO size check against the full `raw_capacity` is the hard final gate.

### Output / workdir layout

- `<output>/images/disc_NNNN.iso` — persisted ISO9660+UDF images written by `cmd_create`, the canonical "what gets burned" artifact. `cmd_burn` reads from here as `-i <input>` (i.e. point burn at the same dir create wrote to).
- `<workdir>/tmp/` — ephemeral working dir for dar slices, par2 files, README during `cmd_create`. Wiped before `cmd_create` returns. `<workdir>` defaults to `<output>/.bd-archive-work/` and is also removed in that case; if the user supplied `-w` (e.g. a tmpfs path) it's left intact apart from the `tmp/` cleanup.
- For `cmd_extract`: `<workdir>/slices/` holds copied slices + sha512 sidecars (and lazy-fetched par2 only during a repair pass). Same default-vs-custom rule: default `<output>/.bd-archive-work/` is auto-removed on success, custom `-w` is left alone.

No metadata file connects `create` to `burn`. `cmd_burn` derives disc count from sorted `images/disc_*.iso` (zero-padded so lexical = numerical sort). Renaming ISOs breaks `cmd_burn`.

### Subprocess wrapper

`shell/runner.py:run()` has three output modes. **Default** streams subprocess output line-by-line with a `[label]` prefix — fine for tools that emit `\n`-terminated lines. **`capture=True`** buffers stdout/stderr into the returned `CompletedProcess` so callers can parse it. **`passthrough=True`** lets the child inherit our stdout/stderr directly — needed for tools that paint progress via `\r` (par2 verify/repair: "Scanning: X%" updates rewrite a single line, which the default streamer would buffer until the next `\n`). par2's exit codes are precise (0=OK, 1=REPAIRABLE, 2=BROKEN) so `tools.par2.verify` reads the result from the return code and doesn't need to capture stdout. `check=True` raises `CalledProcessError`; pass `check=False` when a non-zero exit is informational rather than fatal.

SIGINT handling: children share the parent's process group, so a tty Ctrl+C reaches them too. The wrapper waits up to 5 s for the child to exit, then escalates `terminate()` → wait 5 s → `kill()`. `_check_sigint` converts `returncode == -SIGINT` into `KeyboardInterrupt` on the way out, so the top-level handler in `cli.py` emits a single uniform cancel banner (exit 130) instead of a noisy `CalledProcessError`. `tools/growisofs.py` and `tools/dar.py` opt out of this default in different ways: growisofs runs in `start_new_session=True` and installs its own two-press SIGINT handler so a single accidental Ctrl+C does not coaster a BD-R; dar shares our group so it gets the user's SIGINT directly and we just join + escalate.

### CLI-level error handling

`cli.py:main()` catches `KeyboardInterrupt` (exit 130 with a newline-prefixed "Cancelled by user" banner — the newline matters because progress lines use bare `\r`), `EOFError` (treated same as Ctrl+C, e.g. Ctrl+D at a prompt), `subprocess.CalledProcessError` (prints `tool failed (exit N)` instead of a traceback, since the child's output has already streamed via `shell.runner`), `FileNotFoundError`, and `PermissionError`. Subcommand code can `sys.exit(N)` or raise — both paths produce a coherent terminal experience.

## Conventions

- All user-facing output goes through `ui.logger.Logger` (`log.info/ok/warn/error/step/banner`). Don't `print()` directly for status messages — colors auto-disable on non-TTY via `_c()`.
- Interactive prompts use `ui.prompts.prompt_disc()` (insert-disc gate, supports `q` to cancel; sleeps 3 s after Enter so the drive has time to spin up) and `prompt_yn()`. Keep them — the tool is interactive by design.
- Long-running file ops (slice copy, sha512) wrap themselves in `ui.progress.Progress` (≥ 50 MiB threshold; smaller files copy silently to avoid spam). TTY mode rewrites a single line via `\r`; non-TTY falls back to periodic full lines.
- `bd_archive.__version__` is the single source of truth, embedded in the burned ISO's publisher field and read dynamically by hatch from `src/bd_archive/__init__.py`. Bump when changing on-disc layout.
- Tool wrappers in `tools/` should be thin: build the argv, call `run()`, parse minimally. Domain decisions belong in `archive/` or `commands/`.
- `cli.py` registers `argcomplete.autocomplete(parser)`; the `# PYTHON_ARGCOMPLETE_OK` marker on line 1 is required for the global-completion hook. README documents the user-side setup.

## Working on this project

Always use the project-local venv at `.venv/` for any Python invocation — `python`, `pip`, `ruff`, `bd-archive`, etc. Activate it (`source .venv/bin/activate`) or call the binary directly (`.venv/bin/python`, `.venv/bin/ruff`, `.venv/bin/bd-archive`). Never run `pip install` or `python3` against the system interpreter. If `.venv/` does not exist, create it per the "Running" section above before doing anything else.

Implementation plans under `docs/plans/` are local scratch — gitignored and never committed.
