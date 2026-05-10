# AGENTS

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Single-file Python tool (`bd-archive.py`, Python 3.10+ — uses `match` and `int | float` syntax) that archives a directory tree onto one or more Blu-ray discs using `dar` (slicing/compression) and `par2` (forward error correction). No build system, no tests, no dependencies file — the script is run directly.

## Running

```bash
python3 bd-archive.py create   -s <source> -n <name> -w <workdir> [-D /dev/sr0] [-b BYTES] [-r %] [-c zstd|lzma|...] [-l <level>]
python3 bd-archive.py estimate -s <source> [-D /dev/sr0] [-b BYTES] [-r %] [--ratio <float> | --sample <path> [-c <algo>] [-l <level>]]
python3 bd-archive.py burn     -w <workdir> [-D /dev/sr0] [--start N] [--no-verify] [--skip-fit-check] [-S <speed>]
python3 bd-archive.py verify   <mountpoint|dir|/dev/sr0>
python3 bd-archive.py extract  -o <output> [-D /dev/sr0] [-w <staging>]
```

External binaries required at runtime: `dar`, `par2`, `mkisofs`, `growisofs`, `dvd+rw-mediainfo`, `udisksctl` (for ISO loop-mount in `verify`), plus `mount`/`umount`/`eject` for disc handling. `check_deps()` enforces these per-subcommand.

`verify` exits with `VerifyResult.value` (0=OK, 1=REPAIRABLE, 2=BROKEN) — useful for scripting.

## Architecture (v5: build-then-burn separation)

Four subcommands form a pipeline; `estimate` is a side-tool that previews the same math without running anything.

1. **`create`** reads disc capacity via `detect_disc_capacity(args.device)` (or `args.bytes`), runs `dar --hash sha512 --min-digits 4` to slice the source into per-disc-sized `.dar` files in `<workdir>/tmp/`, then isolates the catalog. For each slice in order: generates PAR2 recovery (alongside the slice in `tmp/`), regenerates `README.txt` with the right disc number, and calls `Iso.build` (mkisofs `-iso-level 3 -udf -graft-points`) to assemble `<workdir>/images/disc_NNNN.iso` directly from in-place files (no staging copies). The ISO file size is checked against the format-aware writable capacity as a hard limit. After each ISO is built, the slice + par2 are deleted from `tmp/`; once all slices are processed, `tmp/` is wiped entirely. Final workdir contains only `images/disc_*.iso`.
2. **`burn`** iterates `<workdir>/images/disc_*.iso` lexically and burns each via `growisofs -dvd-compat -Z dev=image.iso` — a byte-for-byte ISO write, no on-the-fly mkisofs, so what's in the ISO file is exactly what ends up on disc. Volume label, publisher, file layout are all already in the file. Pre-burn fit check compares `iso_size` to `detect_disc_capacity` of the inserted blank. Resumable via `--start N`.
3. **`verify`** dispatches on target type: block device → mount; directory → check directly; **`.iso` file → loop-mount via `udisksctl loop-setup` + check + tear down**. The ISO branch makes pre-burn dry-run trivial: run `create`, then `verify images/disc_0001.iso` to confirm the image is internally consistent before touching media.
4. **`extract`** prompts for discs in any order, copies slices into a staging dir, auto-repairs damaged slices via PAR2 when verify reports `REPAIRABLE`, then runs `dar -x` on the collected slices.
5. **`estimate`** walks the source and applies the same `compute_slice_bytes` math as `create` to predict disc count and last-disc fill, without invoking dar/par2/mkisofs. Compression ratio comes from one of three sources: `--sample <path>` runs dar on a representative subset and measures the actual ratio; `--ratio <float>` is a manual override; otherwise 1.0 (worst case).

The build-then-burn separation makes mid-burn sizing failures **constructively impossible**: the ISO exists and is size-checked before any drive is touched. `burn` is a pure file-to-device copy.

`verify_disc()` is shared between all four verify paths (standalone `verify` on block device / dir / ISO file, post-burn check inside `burn`, and per-disc check inside `extract`).

### Slice sizing (`cmd_create`, `cmd_estimate`)

Raw capacity from `detect_disc_capacity` is the format-aware writable extent: it parses MMC-6 format-type 32h descriptors from `dvd+rw-mediainfo` and returns the largest 32h capacity ≤ Free Blocks (= what the drive will actually accept after its default Outer Spare Area reservation, ~256 MiB on a 25 GB BD-R). `compute_slice_bytes` then subtracts `catalog_est + PAR2_AND_MISC_OVERHEAD` per disc (catalog scales with file count via `scan_source`; `PAR2_AND_MISC_OVERHEAD = 4 MiB` covers par2 index/packet/block-rounding overhead) plus `DISC_END_MARGIN = 1 MiB` for ISO9660+UDF metadata growth that exceeds the slice estimate, then divides by `(100 + redundancy)/100` and floors to a MiB boundary. The post-build ISO size check against `raw_capacity` is the hard final gate.

### Workdir layout

- `<workdir>/tmp/` — ephemeral working dir for dar slices, par2 files, README. Wiped before `cmd_create` returns.
- `<workdir>/images/disc_NNNN.iso` — persisted ISO9660+UDF images, the canonical "what gets burned" artifact. `cmd_burn` reads from here.

No metadata file connects `create` to `burn`. `cmd_burn` derives disc count from sorted `images/disc_*.iso` (zero-padded so lexical = numerical sort). Renaming ISOs breaks `cmd_burn`.

### Subprocess wrapper

`run()` streams subprocess output line-by-line with a `[label]` prefix. Use `capture=True` only when you need to parse stdout (e.g. `Par2.verify` matching on "All files are correct" / "Repair is required"). `check=True` raises `CalledProcessError`; pass `check=False` when a non-zero exit is informational rather than fatal.

## Conventions

- All user-facing output goes through the `Logger` class (`log.info/ok/warn/error/step/banner`). Don't `print()` directly for status messages — colors auto-disable on non-TTY via `_c()`.
- Interactive prompts use `prompt_disc()` (insert-disc gate, supports `q` to cancel) and `prompt_yn()`. Keep them — the tool is interactive by design.
- `VERSION` is the single source of truth, embedded in the burned ISO's publisher field. Bump when changing on-disc layout.
