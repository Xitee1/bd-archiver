# AGENTS

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Python package (`bd_archive`, Python 3.11+ — uses `match`, `int | None`, etc.) that archives a directory tree onto one or more Blu-ray discs using `dar` (slicing/compression) and `par2` (forward error correction). Built with `hatchling`; installed via `pip install .`, exposes the `bd-archive` console script. No tests.

## Running

```bash
# editable install for development
pip install -e '.[dev]'

# or invoke as a module without install
PYTHONPATH=src python -m bd_archive ...
```

```bash
bd-archive create   -s <source> -n <name> -w <workdir> [-D /dev/sr0] [-b BYTES] [-r %] [-c zstd|lzma|...] [-l <level>]
bd-archive estimate -s <source> [-D /dev/sr0] [-b BYTES] [-r %] [--ratio <float> | --sample <path> [-c <algo>] [-l <level>]]
bd-archive burn     -w <workdir> [-D /dev/sr0] [--start N] [--no-verify] [--skip-fit-check] [-S <speed>]
bd-archive verify   <mountpoint|dir|/dev/sr0|*.iso>
bd-archive extract  -o <output> [-D /dev/sr0] [-w <staging>]
```

External binaries required at runtime: `dar`, `par2`, `mkisofs`, `growisofs`, `dvd+rw-mediainfo`, `udisksctl` (for ISO loop-mount in `verify` and Polkit-based mount fallback), plus `mount`/`umount`/`eject` for disc handling. `check_deps()` enforces these per-subcommand.

`verify` exits with `VerifyResult.value` (0=OK, 1=REPAIRABLE, 2=BROKEN) — useful for scripting.

## Package layout

```
src/bd_archive/
├── cli.py              # argparse + dispatch
├── constants.py        # MiB, DISC_*, ISO9660_VOLUME_LABEL_MAX, PAR2_RECOVERY_RE, ...
├── ui/                 # logger, prompts (interactive)
├── shell/              # run(), check_deps(), human_bytes()
├── tools/              # one thin wrapper per external CLI
│   ├── dar.py          # dar create_sliced/isolate_catalog/compress/extract_sequential
│   ├── par2.py         # par2 create/verify/repair (+ VerifyResult, is_par2_index)
│   ├── mkisofs.py      # ISO9660+UDF image build
│   ├── growisofs.py    # burn (+ DeviceBusyError on sg lock)
│   ├── mount.py        # plain mount/umount (no sudo)
│   ├── udisks.py       # udisksctl mount/unmount/loop-setup/loop-delete
│   ├── eject.py        # eject
│   ├── mediainfo.py    # detect_disc_capacity (dvd+rw-mediainfo)
│   └── lsof.py         # find_device_holders
├── archive/            # domain logic over tools/
│   ├── checksums.py    # sha512 verify (verify_dar_hashes)
│   ├── config.py       # ArchiveConfig, write_readme
│   ├── dar_archive.py  # DarArchive (slices, catalog, work-dir layout)
│   ├── disc.py         # DiscIO (mount/burn/eject/with-retry), find_sg_device
│   ├── sizing.py       # compute_slice_bytes, measure_compression_ratio
│   ├── source_scan.py  # SourceScan + scan_source
│   └── verify.py       # verify_disc()
└── commands/           # one file per subcommand
    ├── create.py
    ├── estimate.py
    ├── burn.py
    ├── verify.py
    └── extract.py
```

Layering: `commands/` → `archive/` → `tools/` → `shell/`. Lower layers never import from higher ones. `ui/` is a leaf shared by all layers.

## Architecture (v5: build-then-burn separation)

Four subcommands form a pipeline; `estimate` is a side-tool that previews the same math without running anything.

1. **`create`** (`commands/create.py`) reads disc capacity via `tools.mediainfo.detect_disc_capacity` (or `args.bytes`), runs `tools.dar.create_sliced` with `--hash sha512 --min-digits 4` to slice the source into per-disc-sized `.dar` files in `<workdir>/tmp/`, then isolates the catalog. For each slice in order: generates PAR2 recovery (alongside the slice in `tmp/`), regenerates `README.txt` with the right disc number, and calls `tools.mkisofs.build` (mkisofs `-iso-level 3 -udf -graft-points`) to assemble `<workdir>/images/disc_NNNN.iso` directly from in-place files (no staging copies). The ISO file size is checked against the format-aware writable capacity as a hard limit. After each ISO is built, the slice + par2 are deleted from `tmp/`; once all slices are processed, `tmp/` is wiped entirely. Final workdir contains only `images/disc_*.iso`.
2. **`burn`** (`commands/burn.py`) iterates `<workdir>/images/disc_*.iso` lexically and burns each via `growisofs -dvd-compat -Z dev=image.iso` — a byte-for-byte ISO write, no on-the-fly mkisofs, so what's in the ISO file is exactly what ends up on disc. Volume label, publisher, file layout are all already in the file. Pre-burn fit check compares `iso_size` to `detect_disc_capacity` of the inserted blank. Resumable via `--start N`. Catches `DeviceBusyError` from `tools/growisofs.py` to retry when the sg device is locked by another process.
3. **`verify`** (`commands/verify.py`) dispatches on target type: block device → mount; directory → check directly; **`.iso` file → loop-mount via `tools.udisks.loop_setup` + check + tear down**. The ISO branch makes pre-burn dry-run trivial: run `create`, then `verify images/disc_0001.iso` to confirm the image is internally consistent before touching media.
4. **`extract`** (`commands/extract.py`) prompts for discs in any order, copies slices into a staging dir, auto-repairs damaged slices via PAR2 when verify reports `REPAIRABLE`, then runs `tools.dar.extract_sequential` on the collected slices.
5. **`estimate`** (`commands/estimate.py`) walks the source and applies the same `archive.sizing.compute_slice_bytes` math as `create` to predict disc count and last-disc fill, without invoking dar/par2/mkisofs. Compression ratio comes from one of three sources: `--sample <path>` runs dar on a representative subset and measures the actual ratio; `--ratio <float>` is a manual override; otherwise 1.0 (worst case).

The build-then-burn separation makes mid-burn sizing failures **constructively impossible**: the ISO exists and is size-checked before any drive is touched. `burn` is a pure file-to-device copy.

`archive/verify.py:verify_disc` is shared between all four verify paths (standalone `verify` on block device / dir / ISO file, post-burn check inside `burn`, and per-disc check inside `extract`).

### Slice sizing (`archive/sizing.py`)

Raw capacity from `tools.mediainfo.detect_disc_capacity` is the format-aware writable extent: it parses MMC-6 format-type 32h descriptors from `dvd+rw-mediainfo` and returns the largest 32h capacity ≤ Free Blocks (= what the drive will actually accept after its default Outer Spare Area reservation, ~256 MiB on a 25 GB BD-R). `compute_slice_bytes` then subtracts `catalog_est + PAR2_AND_MISC_OVERHEAD` per disc (catalog scales with file count via `archive.source_scan.scan_source`; `PAR2_AND_MISC_OVERHEAD = 4 MiB` covers par2 index/packet/block-rounding overhead) plus `DISC_END_MARGIN = 1 MiB` for ISO9660+UDF metadata growth that exceeds the slice estimate, then divides by `(100 + redundancy)/100` and floors to a MiB boundary. The post-build ISO size check against `raw_capacity` is the hard final gate.

### Workdir layout

- `<workdir>/tmp/` — ephemeral working dir for dar slices, par2 files, README. Wiped before `cmd_create` returns.
- `<workdir>/images/disc_NNNN.iso` — persisted ISO9660+UDF images, the canonical "what gets burned" artifact. `cmd_burn` reads from here.

No metadata file connects `create` to `burn`. `cmd_burn` derives disc count from sorted `images/disc_*.iso` (zero-padded so lexical = numerical sort). Renaming ISOs breaks `cmd_burn`.

### Subprocess wrapper

`shell/runner.py:run()` streams subprocess output line-by-line with a `[label]` prefix. Use `capture=True` only when you need to parse stdout (e.g. `tools.par2.verify` matching on "All files are correct" / "Repair is required"). `check=True` raises `CalledProcessError`; pass `check=False` when a non-zero exit is informational rather than fatal.

## Conventions

- All user-facing output goes through `ui.logger.Logger` (`log.info/ok/warn/error/step/banner`). Don't `print()` directly for status messages — colors auto-disable on non-TTY via `_c()`.
- Interactive prompts use `ui.prompts.prompt_disc()` (insert-disc gate, supports `q` to cancel) and `prompt_yn()`. Keep them — the tool is interactive by design.
- `bd_archive.__version__` is the single source of truth, embedded in the burned ISO's publisher field and read dynamically by hatch from `src/bd_archive/__init__.py`. Bump when changing on-disc layout.
- Tool wrappers in `tools/` should be thin: build the argv, call `run()`, parse minimally. Domain decisions belong in `archive/` or `commands/`.
