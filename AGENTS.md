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

External binaries required at runtime: `dar`, `par2`, `growisofs`, `dvd+rw-mediainfo`, plus `mount`/`umount`/`eject` for disc handling. `check_deps()` enforces this per-subcommand (both `create` and `burn` require `dvd+rw-mediainfo`).

`verify` exits with `VerifyResult.value` (0=OK, 1=REPAIRABLE, 2=BROKEN) — useful for scripting.

## Architecture

Four subcommands form a pipeline, glued together by a workdir on disk; `estimate` is a side-tool that previews the same math without running dar/par2.

1. **`create`** reads disc capacity via `detect_disc_capacity(args.device)` (or `args.bytes` as a manual override), then runs `dar --hash sha512 --min-digits 4` to slice the source into per-disc-sized `.dar` files (dar emits a sibling `<slice>.sha512` per slice, sha512sum-compatible; `--min-digits 4` zero-pads slice numbers so lexical sort matches numerical order past 9 slices). For each slice it builds a staging directory `<workdir>/staging/disc_NNNN/` containing: the slice + its `.sha512`, the isolated catalog slices + their `.sha512` files, PAR2 recovery files (self-verifying), and a `README.txt`. PAR2 and README intentionally have no hash — PAR2 verifies itself, README is non-load-bearing.
2. **`burn`** burns each `staging/disc_NNNN/` with `growisofs`, deriving disc count from the sorted `disc_*/` directories and archive name from the first `*.dar` filename in `disc_0001`. Volume label is `<archive_name>_<NNNN>` (also zero-padded for stable physical disc ordering). Performs a pre-burn fit check (rejects discs whose capacity is too small or more than 5% larger than the staging size; bypass with `--skip-fit-check`). Loop is resumable via `--start N`; the script prints the exact resume command after each disc and on post-burn-verify failure.
3. **`verify`** dispatches on the target type (block device → mount; directory → check directly).
4. **`extract`** prompts for discs in any order, copies slices into a staging dir, auto-repairs damaged slices via PAR2 when verify reports `REPAIRABLE`, then runs `dar -x` on the collected slices.
5. **`estimate`** walks the source and applies the same `compute_slice_bytes` math as `create` to predict disc count and last-disc fill, without burning anything. Compression ratio comes from one of three sources, in order of accuracy: `--sample <path>` runs dar with `-c`/`-l` on a representative subset and measures the actual output/input ratio (via `measure_compression_ratio`); `--ratio <float>` accepts a manual override; otherwise 1.0 (worst case). Useful for adjusting source contents before committing to a real `create`.

`verify_disc()` is shared between all three verify paths (standalone `verify`, post-burn check inside `burn`, and per-disc check inside `extract`).

### Slice sizing (`cmd_create`, `cmd_estimate`)

Raw capacity comes from `detect_disc_capacity(args.device)` (via `dvd+rw-mediainfo`) or `args.bytes`. After subtracting 2 MiB for ISO/UDF filesystem overhead, `compute_slice_bytes` reserves `catalog_est + PAR2_AND_MISC_OVERHEAD` per disc, divides the remainder by `(100 + redundancy)/100` to leave room for PAR2, and floors to a MiB boundary. `catalog_est` comes from `scan_source` (single filesystem walk: per entry ~256 B + path length), so the reservation scales with file count rather than relying on a fixed margin. `PAR2_AND_MISC_OVERHEAD` (4 MiB) covers the par2 index file, packet/block-rounding overhead, sha512 hash files, and README. The staging size check at the end of `cmd_create` is the final safety net.

### Staging contract

No metadata file connects `create` to `burn`. `cmd_burn` derives `disc_count` from the sorted `staging/disc_*/` directories (zero-padded to 4 digits, so lexical sort = numerical sort) and `archive_name` from the first non-catalog `*.dar` filename in `disc_0001`. Renaming staging directories or `.dar` files breaks `cmd_burn`.

### Subprocess wrapper

`run()` streams subprocess output line-by-line with a `[label]` prefix. Use `capture=True` only when you need to parse stdout (e.g. `Par2.verify` matching on "All files are correct" / "Repair is required"). `check=True` raises `CalledProcessError`; pass `check=False` when a non-zero exit is informational rather than fatal.

## Conventions

- All user-facing output goes through the `Logger` class (`log.info/ok/warn/error/step/banner`). Don't `print()` directly for status messages — colors auto-disable on non-TTY via `_c()`.
- Interactive prompts use `prompt_disc()` (insert-disc gate, supports `q` to cancel) and `prompt_yn()`. Keep them — the tool is interactive by design.
- `VERSION` is the single source of truth, embedded in the burned ISO's publisher field. Bump when changing on-disc layout.
