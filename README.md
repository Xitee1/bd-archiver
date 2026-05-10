# bd-archive

Archive data to Blu-ray discs with `dar` + `par2`.

Five subcommands form a build-then-burn pipeline:

- `create`   — Slice + compress source, build PAR2 recovery, assemble per-disc ISO images. No burning.
- `estimate` — Preview disc count and per-disc fill without running dar/par2.
- `burn`     — Burn pre-built ISO images to discs (resumable).
- `verify`   — Check disc / directory / ISO integrity (SHA-512 + PAR2). Exit code reflects state.
- `extract`  — Restore archive from discs with auto-repair via PAR2.

## Installation

### System dependencies

```bash
# Arch
sudo pacman -Syu dar par2cmdline dvd+rw-tools cdrtools udisks2

# Debian / Ubuntu
sudo apt install dar par2 growisofs genisoimage udisks2
```

Optional: `lsof` (better diagnostics when the optical device is locked by another process).

### Python package

Requires Python ≥ 3.11. Install into a project-local virtualenv (modern distros block bare `pip install` via PEP 668):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'   # editable + dev tools (ruff)
# or, runtime only:
pip install .
```

`.venv/` is gitignored. With the venv activated, the `bd-archive` command is on `PATH`. Re-activate later with `source .venv/bin/activate`.

## Usage

### create

```bash
bd-archive create -s /path/to/source -n my-archive -w /path/to/workdir [options]
```

| Flag | Default | Description |
|---|---|---|
| `-s, --source`     | required          | Source directory |
| `-n, --name`       | required          | Archive name (≤ 27 chars; ISO9660 volume label limit minus 5-char disc suffix) |
| `-w, --workdir`    | required          | Working directory for archive + staged ISOs |
| `-r, --redundancy` | `5`               | PAR2 redundancy in % |
| `-D, --device`     | `/dev/sr0`        | Optical drive used for capacity detection |
| `-b, --bytes`      | auto-detected     | Manual disc capacity in raw bytes |
| `-c, --compression`| `zstd`            | `zstd`, `lzma`, `lz4`, `gzip`, `bzip2`, `none` |
| `-l, --level`      | —                 | Compression level |

After completion, ISOs sit in `<workdir>/images/disc_NNNN.iso`. Verify them before burning:

```bash
bd-archive verify <workdir>/images/disc_0001.iso
```

### estimate

```bash
bd-archive estimate -s /path/to/source [-D /dev/sr0 | -b BYTES] [options]
```

Compression ratio source (mutually exclusive):
- `--sample <path>`  — Run dar on a subset, measure actual ratio (most accurate).
- `--ratio <float>`  — Manually supplied ratio (`1.0` = no compression, `0.5` = 50% reduction).
- *neither*          — Defaults to `1.0` (worst case).

### burn

```bash
bd-archive burn -w /path/to/workdir [options]
```

| Flag | Default | Description |
|---|---|---|
| `-w, --workdir`      | required        | Working directory from `create` |
| `-D, --device`       | `/dev/sr0`      | Optical drive |
| `-S, --speed`        | drive max       | BD speed multiplier (e.g. `2`, `4`, `6`; 1× ≈ 4.5 MB/s) |
| `--start N`          | `1`             | Resume from disc N |
| `--no-verify`        | off             | Skip post-burn verification |
| `--skip-fit-check`   | off             | Skip pre-burn capacity check |

If burning fails on disc N, resume with `--start N` after fixing the issue.

### verify

```bash
bd-archive verify <target>
```

`<target>` is one of:
- A mountpoint directory (already-mounted disc or extracted slices)
- A block device (e.g. `/dev/sr0`) — mounted automatically
- An `.iso` file — loop-mounted via `udisksctl`

Exit codes: `0` OK, `1` repairable, `2` broken.

### extract

```bash
bd-archive extract -o /path/to/output [-D /dev/sr0] [-w /path/to/workdir]
```

Prompts for each disc; auto-repairs damaged slices via PAR2 when possible. Uses `dar --sequential-read` so missing slices auto-skip rather than aborting the whole restore.

## Development

### Project structure

```
src/bd_archive/
├── cli.py              # argparse + dispatch
├── constants.py        # disc capacities, ISO9660 limits, regex
├── ui/                 # logger, prompts (interactive)
├── shell/              # run(), check_deps(), human_bytes()
├── tools/              # one thin wrapper per external CLI
│   ├── dar.py          # dar create/extract/isolate/sample-compress
│   ├── par2.py         # par2 + VerifyResult + is_par2_index
│   ├── mkisofs.py      # ISO9660+UDF builder
│   ├── growisofs.py    # burn (+ DeviceBusyError)
│   ├── mount.py        # plain mount/umount
│   ├── udisks.py       # udisksctl mount/loop-setup
│   ├── eject.py
│   ├── mediainfo.py    # dvd+rw-mediainfo capacity detection
│   └── lsof.py         # find_device_holders
├── archive/            # domain logic over tools/
│   ├── checksums.py    # SHA-512 verification
│   ├── config.py       # ArchiveConfig + write_readme
│   ├── dar_archive.py  # DarArchive class
│   ├── disc.py         # DiscIO class + find_sg_device
│   ├── sizing.py       # compute_slice_bytes + measure_compression_ratio
│   ├── source_scan.py  # SourceScan + scan_source
│   └── verify.py       # verify_disc()
└── commands/           # one file per subcommand
    ├── create.py
    ├── estimate.py
    ├── burn.py
    ├── verify.py
    └── extract.py
```

Layering: `commands/` → `archive/` → `tools/` → `shell/`. Lower layers never import from higher ones. `ui/` is shared and may be used at any layer.

### Build

Build backend: `hatchling`. Version is read dynamically from `src/bd_archive/__init__.py` (`__version__`).

```bash
source .venv/bin/activate    # if not already active
pip install build
python -m build              # produces sdist + wheel in dist/
```

### Lint

The dev install (`pip install -e '.[dev]'`) puts `ruff` in the venv:

```bash
source .venv/bin/activate    # if not already active
ruff check src/
ruff format src/
```

Config in `pyproject.toml`: line-length 100, target Python 3.11, rule selection `E,W,F,I,B,UP,C4,SIM`.

### Run without install

For a quick `--help` peek without setting up the venv (subcommands still need the system deps listed above):

```bash
PYTHONPATH=src python3 -m bd_archive --help
```
