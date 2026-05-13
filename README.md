# bd-archive

Archive data to Blu-ray discs with `dar` + `par2`.

Four subcommands form a build-then-burn pipeline:

- `create`   — Slice + compress source, build PAR2 recovery, assemble per-disc ISO images. Supports full archives and incrementals (via `--base`). No burning.
- `burn`     — Burn pre-built ISO images to discs (resumable).
- `verify`   — Check disc / directory / ISO integrity via PAR2. Exit code reflects state.
- `extract`  — Restore archive from discs with auto-repair via PAR2. Whole-chain mode: insert discs from any generation in any order; the tool walks the chain at the end.

Optical drives are auto-detected from `/sys/block/sr*`: a single drive is used automatically, multiple drives trigger a picker. Pass `-D /dev/srN` to override.

### Chain identity = archive name

Incremental archives form a **chain**: a Full (Gen 1), then any number of incremental generations (Gen 2, 3, …) that record only what changed since the previous gen. The archive name from `-n` is the chain's identity — **use the same `-n` for every generation of the same chain**. Renaming between generations breaks chain detection at extract time. The volume label shows generation + disc number; the human-readable name in `-n` should be picked for the long term, even if its meaning drifts (an archive named `family-2024-batch1` can grow to hold years of new family photos — its name doesn't have to stay literally accurate, but it must stay literally the same).

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

Requires Python ≥ 3.11. It's recommended to use a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate

# For production
pip install .

# For development
pip install -e '.[dev]'

bd-archive -h # show bd-archive usage
```

To re-activate the virtual environment after closing the terminal, type `source .venv/bin/activate` again.


#### Shell completion (optional)

`bd-archive` ships with [`argcomplete`](https://github.com/kislyuk/argcomplete) support for bash/zsh tab-completion of subcommands, flags, and path arguments.

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

### Docker

A prebuilt image with all runtime tools (dar, par2, mkisofs, growisofs, dvd+rw-tools, udisks2, lsof, eject) is published to GitHub Container Registry on every `v*.*.*` tag:

```bash
docker pull ghcr.io/xitee1/bd-archiver:latest
```

#### Caveats

- **Drive auto-detection is disabled in containers.** `list_drives()` scans `/sys/block/sr*`, which is not populated by `--device=…` passthrough. Always pass `-D /dev/srN` explicitly when the subcommand uses a drive.
- **`burn` needs raw SCSI access** (`growisofs` issues SG_IO ioctls). The simplest way is `--privileged`; if you want tighter scoping, `--cap-add=SYS_RAWIO` is the relevant capability.
- **`verify <iso-file>` does not work out of the box.** It uses `udisksctl` to loop-mount, which needs a running `udisksd` + dbus inside the container. Easiest workaround: verify ISOs on the host, or pre-mount on the host (`sudo mount -o loop disc.iso /mnt/iso`) and pass the mountpoint instead.

#### Examples

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

## Usage example
This is an example that demonstrates a lot of (but not all) features of this tool.

Let's say you have 199GB worth of images on an HDD that you want to archive onto 25GB BDs.
Before you start, you check that the output dir has at least 250GB (total amount (199GB) + disc size (25GB) + some buffer).

Because it's all images, you opt for no compression (images don't compress good).

### create + burn
First, create the ISOs:
`bd-archive create -s /path/to/images -o /path/to/staging-dir --name "My_image_archive" -c none`

Now the folder is scanned and an overview is provided with the amount of discs and other useful information.
You notice that it says the last disk will only be filled with 300 MB.
Images don't compress good, but you can still get a little bit out of it, so you decline and run again with the default compression:

`bd-archive create -s /path/to/images --name "My_image_archive" -c none`

Now it fits perfectly and you safe a disc. You confirm with `y`.
The ISO files are now generated. This can take a while (multiple hours depending on the storage device of the output dir).

After it's done, proceed with burning (x4 speed).
`bd-archive burn -i /path/to/staging-dir -S 4`

bd-archiver will now ask you to insert the first disc. After it's inserted, press enter to start the burn process.
After burning it will automatically verify the data integrity. You may need manual intervention and close the tray before that if it opens automatically by firmware.
After everything is verified, insert the next disc until the end.

But let's say you need to shutdown/restart your PC and have a lot of discs left. No problem, just wait for the current burn process to finish and exit using `CTRL + C`.
When you want to continue, just start with `--start x` where x is the disc number. For example you've burned 3 out of 10 discs, you type `--start 4`.

After all your discs are burned and verified, delete all the staging ISO files.

### verify
If later (e.g. after some years) you want to verify a specific disc, just insert it and execute `bd-archive verify` to check the integrity.

Exit codes: `0` OK, `1` repairable, `2` broken.


### Adding an incremental generation

Some time later you have a new batch of photos you want to add to the same archive. Rather than re-burning everything from scratch, build an **incremental** generation that contains only the delta:

```bash
bd-archive create \
    -s /path/to/images \
    -n "My_image_archive" \
    --base /path/to/staging-dir/My_image_archive-gen1-catalog.0001.dar \
    -o /path/to/gen2-staging-dir \
    -c none
```

`--base` points at the previous generation's locally-persisted catalog (written into the previous output dir alongside `images/`). The tool diffs the current source against that catalog and archives only files that are new or changed. The new gen gets its own ISOs (typically far fewer discs than the full), its own catalog, its own output dir. Burn it like any other set:

```bash
bd-archive burn -i /path/to/gen2-staging-dir
```

You can chain as many generations as you want (`--base` always points at the most recent gen's catalog). The first lookup the tool does is **the archive name** — pass the same `-n` you used for Gen 1, otherwise `--base` refuses to proceed.

#### Auto-defer with `--min-last-disc-fill`

When the last disc of an incremental would be only sparsely filled (e.g. 1 GB on a 50 GB disc), you can tell bd-archive to push the newest files to a later generation so the current set rounds down to fewer discs:
The tool will prompt you to insert disc 1 - x. `dar` supports partial restore, so you don't need all discs for a file you know is on disc 5. Just make sure you insert all the relevant discs if the file is physically splitted across multiple discs.

While extracting, it will automatically check for data integrity and fix everything it can with the help of par2.

```bash
bd-archive create -s /path/to/images -n "My_image_archive" \
    --base /path/to/gen1/My_image_archive-gen1-catalog.0001.dar \
    -o /path/to/gen2 -c none --min-last-disc-fill 50
```

`--min-last-disc-fill 50` says "the last disc must end up at least 50 % full". The tool iterates the newest-by-mtime files that are **not already in the base catalog** and defers them one by one until either the threshold is met or the candidate pool is exhausted. The deferred files stay in your source — they'll naturally appear as "new" the next time you create an incremental against this generation's catalog.

Without `--base` (i.e. on a Full archive), `--min-last-disc-fill` still works but defers files that **will not be archived until you do an incremental run later**. The tool warns loudly when you're in that mode.

### extract — whole-chain restore

```bash
bd-archive extract -o /path/to/output [options]
```

| Flag | Default | Description |
|---|---|---|
| `-o, --output`   | required                       | Where extracted files land |
| `-D, --device`   | auto-detect                    | Optical drive. Auto-picks the only drive present; prompts if multiple. |
| `-w, --workdir`  | `<output>/.bd-archive-work/`   | Staging dir for slices. Override to put scratch on tmpfs/RAM. Auto-removed on success when default. |

The chain name is auto-detected from the first disc's filenames — there is no `-n` flag. Discs from multiple generations of the same chain may be inserted in any order; the tool detects each disc's generation from its filenames (`<name>-gen<N>.NNNN.dar`).

Per-disc flow: copy slice + sha512 sidecar (and that generation's catalog, on its first intact arrival) to staging in a single read pass, eject, verify the staged slice via SHA-512. PAR2 files are **not** copied unless a slice fails verification — at which point the disc is re-mounted, just the par2 for the affected slice is fetched, and `par2 repair` runs in staging. If the catalog itself fails on a disc, the bad slice is dropped and re-fetched from the next disc that carries it.

After each disc, the tool asks whether to continue. Once you stop, it runs `dar -x` for each generation found in staging, in order: Gen 1 extracts into the (empty) output dir, then Gen 2 extracts on top with overwrite, and so on. Files modified in later generations replace the older versions; deletions recorded in later catalogs are honoured. Partial restores work too — losing all discs of one generation leaves a hole in the chain, but earlier and later gens still restore what they hold.

Per-file `Bad CRC` lines from dar plus any slices that failed sha512 *and* par2 are recorded in `<output>/corrupted-files.txt`, and `extract` exits with code `1` so scripts can detect a non-clean restore. The output dir still contains whatever dar managed to extract — best-effort, never silently corrupt.

For maximum throughput on SSD-hosted archives, point `-w` at a tmpfs path (`/dev/shm/bd-extract`) — a 25 GB slice fits in RAM and never hits disk during staging.

#### Legacy (pre-incremental) archives

Archive sets burned before this version's naming convention have slices named `<name>.NNNN.dar` (no `-gen<N>` segment). Extract handles them transparently as Gen 1. To extend an old set with an incremental: copy the isolated catalog off any of its discs (`<name>-catalog.NNNN.dar`) and pass that file to `--base` on a new `create` run — the new generation will be Gen 2 of the chain, with `<name>-gen2.NNNN.dar` filenames.

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
```
