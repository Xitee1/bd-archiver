# bd-archive

Archive data to Blu-ray discs with `dar` + `par2`.

Four subcommands form a build-then-burn pipeline:

- `create`   — Slice + compress source, build PAR2 recovery, assemble per-disc ISO images. No burning.
- `burn`     — Burn pre-built ISO images to discs (resumable).
- `verify`   — Check disc / directory / ISO integrity via PAR2. Exit code reflects state.
- `extract`  — Restore archive from discs with auto-repair via PAR2.

Optical drives are auto-detected from `/sys/block/sr*`: a single drive is used automatically, multiple drives trigger a picker. Pass `-D /dev/srN` to override.

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
pip install -e '.[dev]'   # editable + dev tools (ruff, pre-commit)
# or, runtime only:
pip install .
```

`.venv/` is gitignored. With the venv activated, the `bd-archive` command is on `PATH`. Re-activate later with `source .venv/bin/activate`.

### Shell completion (optional)

`bd-archive` ships with [`argcomplete`](https://github.com/kislyuk/argcomplete) support for bash/zsh tab-completion of subcommands, flags, and path arguments. Pick one:

**Per-user (recommended):** add to `~/.bashrc` (or `~/.zshrc`):

```bash
eval "$(register-python-argcomplete bd-archive)"
```

Reload the shell (`exec bash`) and `bd-archive <TAB>` works.

**System-wide:** if you use several argcomplete-based tools, activate the global hook once instead:

```bash
sudo activate-global-python-argcomplete
```

This enables completion for **every** argcomplete-enabled Python CLI on the system, not just `bd-archive`. No per-user setup needed afterwards.

## Docker

A prebuilt image with all runtime tools (dar, par2, mkisofs, growisofs, dvd+rw-tools, udisks2, lsof, eject) is published to GitHub Container Registry on every `v*.*.*` tag:

```bash
docker pull ghcr.io/xitee1/bd-archiver:latest
```

Available tags: `latest`, `<major>` (e.g. `5`), `<major>.<minor>` (e.g. `5.0`), `<full-version>` (e.g. `5.0.0`). Images are built for `linux/amd64` and `linux/arm64`.

### Caveats

- **Drive auto-detection is disabled in containers.** `list_drives()` scans `/sys/block/sr*`, which is not populated by `--device=…` passthrough. Always pass `-D /dev/srN` explicitly when the subcommand uses a drive.
- **`burn` needs raw SCSI access** (`growisofs` issues SG_IO ioctls). The simplest way is `--privileged`; if you want tighter scoping, `--cap-add=SYS_RAWIO` is the relevant capability.
- **`verify <iso-file>` does not work out of the box.** It uses `udisksctl` to loop-mount, which needs a running `udisksd` + dbus inside the container. Easiest workaround: verify ISOs on the host, or pre-mount on the host (`sudo mount -o loop disc.iso /mnt/iso`) and pass the mountpoint instead.

### Examples

Host paths used below: source data at `/data/src`, output at `/data/out`. Adjust to taste.

**create** (build per-disc ISOs from a source tree):

```bash
docker run --rm -it \
  --device=/dev/sr0 \
  -v /data:/data \
  ghcr.io/xitee1/bd-archiver:latest \
  create -s /data/src -n my-archive -o /data/out -D /dev/sr0
```

If you don't want to mount the drive at all (e.g. driveless build with a fixed capacity), drop `--device` and pass `-b <bytes>` instead of `-D`:

```bash
docker run --rm -it \
  -v /data:/data \
  ghcr.io/xitee1/bd-archiver:latest \
  create -s /data/src -n my-archive -o /data/out -b 25025314816
```

**burn** (write ISOs to disc):

```bash
docker run --rm -it \
  --privileged --device=/dev/sr0 \
  -v /data:/data \
  ghcr.io/xitee1/bd-archiver:latest \
  burn -i /data/out -D /dev/sr0
```

**verify** a block device:

```bash
docker run --rm -it \
  --device=/dev/sr0 \
  -v /data:/data \
  ghcr.io/xitee1/bd-archiver:latest \
  verify /dev/sr0
```

**extract** (restore from disc, RAM staging via tmpfs):

```bash
docker run --rm -it \
  --device=/dev/sr0 \
  -v /data:/data \
  --tmpfs /scratch:size=32g \
  ghcr.io/xitee1/bd-archiver:latest \
  extract -o /data/restore -w /scratch -D /dev/sr0
```

`--tmpfs` here serves the same purpose as `-w /dev/shm/...` for a local install: keeps slice staging in RAM during extract so the SSD takes zero writes.

## Usage

### create

```bash
bd-archive create -s /path/to/source -n my-archive -o /path/to/output [options]
```

| Flag | Default | Description |
|---|---|---|
| `-s, --source`     | required          | Source directory |
| `-n, --name`       | required          | Archive name (≤ 27 chars; ISO9660 volume label limit minus 5-char disc suffix) |
| `-o, --output`     | required          | Output directory for ISO images |
| `-w, --workdir`    | `<output>/.bd-archive-work/` | Scratch dir for transient build files (dar slices, par2). Override to put scratch on tmpfs/RAM. Auto-removed on success when default. |
| `-r, --redundancy` | `5`               | PAR2 redundancy in % |
| `-D, --device`     | auto-detect       | Optical drive used for capacity detection. Auto-picks the only drive present; prompts if multiple. |
| `-b, --bytes`      | auto-detected     | Manual disc capacity in raw bytes |
| `-c, --compression`| `zstd`            | `zstd`, `lzma`, `lz4`, `gzip`, `bzip2`, `none` |
| `-l, --level`      | —                 | Compression level |
| `--ratio`          | —                 | Manual compression ratio for the disc-count preview (1.0 = none, 0.5 = 50% reduction). Mutually exclusive with `--sample`. |
| `--sample <path>`  | —                 | Run dar on this subset with `-c/-l` and use the measured ratio for the preview. Mutually exclusive with `--ratio`. |
| `-y, --yes`        | off               | Skip the pre-archive confirmation prompt. |

`create` prints a disc-count + last-disc-fill preview and asks for confirmation before running. Pass `-y` to skip the prompt for scripts.

After completion, ISOs sit in `<output>/images/disc_NNNN.iso`. Verify them before burning:

```bash
bd-archive verify <output>/images/disc_0001.iso
```

### burn

```bash
bd-archive burn -i /path/to/input [options]
```

| Flag | Default | Description |
|---|---|---|
| `-i, --input`        | required        | Directory `create` wrote to (contains `images/disc_*.iso`) |
| `-D, --device`       | auto-detect     | Optical drive. Auto-picks the only drive present; prompts if multiple. |
| `-S, --speed`        | drive max       | BD speed multiplier (e.g. `2`, `4`, `6`; 1× ≈ 4.5 MB/s) |
| `--start N`          | `1`             | Resume from disc N |
| `--no-verify`        | off             | Skip post-burn verification |
| `--skip-fit-check`   | off             | Skip the pre-burn capacity check (covers both *too small* and *too large by >5%*, the latter guards against wasting a 50 GB BD-DL on a 25 GB-sized archive) |

If burning fails on disc N, resume with `--start N` after fixing the issue.

### verify

```bash
bd-archive verify [target]
```

`[target]` is optional and may be:
- Omitted — auto-detect an optical drive (prompts if multiple)
- A mountpoint directory (already-mounted disc or extracted slices)
- A block device (e.g. `/dev/srN`) — mounted automatically
- An `.iso` file — loop-mounted via `udisksctl`

Exit codes: `0` OK, `1` repairable, `2` broken.

### extract

```bash
bd-archive extract -o /path/to/output [options]
```

| Flag | Default | Description |
|---|---|---|
| `-o, --output`   | required                       | Where extracted files land |
| `-D, --device`   | auto-detect                    | Optical drive. Auto-picks the only drive present; prompts if multiple. |
| `-w, --workdir`  | `<output>/.bd-archive-work/`   | Staging dir for slices. Override to put scratch on tmpfs/RAM. Auto-removed on success when default. |

The archive name is auto-detected from the first disc's filenames — there is no `-n` flag.

Per-disc flow: copy slice + sha512 sidecar (and the catalog, on its first intact arrival) to staging in a single read pass, eject, then verify the staged slice via SHA-512. PAR2 files are **not** copied unless a slice fails verification — at which point the disc is re-mounted, just the par2 for the affected slice is fetched, and `par2 repair` runs in staging. If the catalog itself fails on this disc, the bad slice is dropped and re-fetched from the next disc that carries it.

After each disc, you are asked whether to continue — answer `n` for a partial restore (e.g. one disc lost). Once you stop, `dar --sequential-read` does the final extraction; dar's "missing slice" prompts are auto-skipped so a partial set still yields ~95% of files. Per-file `Bad CRC` lines from dar plus any slices that failed sha512 *and* par2 are recorded in `<output>/corrupted-files.txt`, and `extract` exits with code `1` so scripts can detect a non-clean restore. The output dir still contains whatever dar managed to extract — best-effort, never silently corrupt.

For maximum throughput on SSD-hosted archives, point `-w` at a tmpfs path (`/dev/shm/bd-extract`) — a 25 GB slice fits in RAM and never hits disk during staging.

## Development

### Project structure

```
src/bd_archive/
├── __init__.py         # __version__ (loaded from _version.py via hatch-vcs)
├── __main__.py         # entry point for `python -m bd_archive`
├── _par2_helper.py     # dar -E hook: runs par2 on each freshly written slice
├── cli.py              # argparse + dispatch + top-level exception handling
├── constants.py        # disc capacities, ISO9660 limits, regex
├── ui/                 # logger, prompts (interactive), progress reporter
├── shell/              # runner (run()), deps (check_deps()), format (human_bytes())
├── tools/              # one thin wrapper per external CLI
│   ├── dar.py          # dar create/extract/isolate/sample-compress
│   ├── par2.py         # par2 + VerifyResult + is_par2_index
│   ├── mkisofs.py      # ISO9660+UDF builder
│   ├── growisofs.py    # burn (+ DeviceBusyError, SIGINT double-press abort)
│   ├── mount.py        # plain mount/umount
│   ├── udisks.py       # udisksctl mount/unmount/loop-setup/loop-delete
│   ├── eject.py        # eject + close_tray + drive_status (CDROM ioctl)
│   ├── mediainfo.py    # dvd+rw-mediainfo capacity detection (all format types)
│   ├── optical.py      # list_drives + resolve_device (auto-detect / prompt)
│   └── lsof.py         # find_device_holders (optional)
├── archive/            # domain logic over tools/
│   ├── checksums.py    # SHA-512 verification
│   ├── config.py       # ArchiveConfig + write_readme
│   ├── dar_archive.py  # DarArchive class
│   ├── disc.py         # DiscIO (mount/with-retry/umount/eject/close-tray/burn) + find_sg_device
│   ├── sizing.py       # compute_slice_bytes + measure_compression_ratio
│   ├── source_scan.py  # SourceScan + scan_source
│   └── verify.py       # verify_disc()
└── commands/           # one file per subcommand
    ├── create.py
    ├── burn.py
    ├── verify.py
    └── extract.py
```

Layering: `commands/` → `archive/` → `tools/` → `shell/`. Lower layers never import from higher ones. `ui/` is shared and may be used at any layer.

### Build

Build backend: `hatchling` + `hatch-vcs`. The version is derived from the latest git tag (`v*` prefix) and written into `src/bd_archive/_version.py` at build time. `bd_archive/__init__.py` imports from there, with an `importlib.metadata` fallback for editable installs. To release, tag (`v5.0.0`) and push — the `Docker Publish` workflow takes care of the rest.

```bash
source .venv/bin/activate    # if not already active
pip install build
python -m build              # produces sdist + wheel in dist/
```

`_version.py` is generated and gitignored — don't commit it. After moving to a new tag, re-run `pip install -e .` to regenerate it for editable installs.

### Lint

The dev install (`pip install -e '.[dev]'`) puts `ruff` and `pre-commit` in the venv:

```bash
source .venv/bin/activate    # if not already active
ruff check src/
ruff format src/
pre-commit install           # one-time: enables ruff-format on each commit
```

Config in `pyproject.toml`: line-length 100, target Python 3.11, rule selection `E,W,F,I,B,UP,C4,SIM`.

### Run without install

For a quick `--help` peek without setting up the venv (subcommands still need the system deps listed above):

```bash
PYTHONPATH=src python3 -m bd_archive --help
```
