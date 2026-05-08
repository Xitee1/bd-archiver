# AGENTS

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Single-file Python tool (`bd-archive.py`, Python 3.10+ — uses `match` and `int | float` syntax) that archives a directory tree onto one or more Blu-ray discs using `dar` (slicing/compression) and `par2` (forward error correction). No build system, no tests, no dependencies file — the script is run directly.

## Running

```bash
./bd-archive.py create  -s <source> -n <name> -w <workdir> [-d 25|50|100] [-r %] [-c zstd|lzma|...] [-l <level>]
./bd-archive.py burn    -w <workdir> [-D /dev/sr0] [--start N] [--no-verify] [-S <speed>]
./bd-archive.py verify  <mountpoint|dir|/dev/sr0>
./bd-archive.py extract -o <output> [-D /dev/sr0] [-w <staging>]
```

External binaries required at runtime: `dar`, `par2`, `growisofs`, plus `mount`/`umount`/`eject` for disc handling. `check_deps()` enforces this per-subcommand.

`verify` exits with `VerifyResult.value` (0=OK, 1=REPAIRABLE, 2=BROKEN) — useful for scripting.

## Architecture

Four subcommands form a pipeline, glued together by a workdir on disk:

1. **`create`** runs `dar` to slice the source into per-disc-sized `.dar` files, then for each slice builds a staging directory `<workdir>/staging/disc_N/` containing: the slice, the isolated catalog, PAR2 recovery files, a `README.txt`, and `CHECKSUMS.sha256` (generated **last** so it covers everything else). Writes `bd-archive.json` metadata into the workdir.
2. **`burn`** reads `bd-archive.json` and burns each `staging/disc_N/` with `growisofs`. Loop is resumable via `--start N`; the script prints the exact resume command after each disc and on post-burn-verify failure.
3. **`verify`** dispatches on the target type (block device → mount; directory → check directly).
4. **`extract`** prompts for discs in any order, copies slices into a staging dir, auto-repairs damaged slices via PAR2 when verify reports `REPAIRABLE`, then runs `dar -x` on the collected slices.

`verify_disc()` is shared between all three verify paths (standalone `verify`, post-burn check inside `burn`, and per-disc check inside `extract`).

### Slice sizing (`cmd_create`)

Disc capacities in `DISC_CAPACITY` already subtract 2 MiB for ISO/UDF filesystem overhead. `cmd_create` then subtracts a further `1 MiB + 256 KiB` overhead, divides the remainder by `(100 + redundancy)/100` to leave room for PAR2, and floors to a MiB boundary. Changing any of these constants risks discs that overflow at burn time — the staging size check at the end of `cmd_create` is the safety net.

### Metadata contract

`bd-archive.json` (constant `METADATA_FILE`) is the only thing connecting `create` to `burn`. Adding fields is safe; renaming/removing existing keys (`disc_count`, `disc_size`, `archive_name`) breaks `cmd_burn`.

### Subprocess wrapper

`run()` streams subprocess output line-by-line with a `[label]` prefix. Use `capture=True` only when you need to parse stdout (e.g. `Par2.verify` matching on "All files are correct" / "Repair is required"). `check=True` raises `CalledProcessError`; pass `check=False` when a non-zero exit is informational rather than fatal.

## Conventions

- All user-facing output goes through the `Logger` class (`log.info/ok/warn/error/step/banner`). Don't `print()` directly for status messages — colors auto-disable on non-TTY via `_c()`.
- Interactive prompts use `prompt_disc()` (insert-disc gate, supports `q` to cancel) and `prompt_yn()`. Keep them — the tool is interactive by design.
- `VERSION` is the single source of truth, embedded in the burned ISO's publisher field. Bump when changing on-disc layout.
