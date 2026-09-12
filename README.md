# bd-archiver

Archive folders to Blu-ray data discs with integrity checks and optional PAR2
recovery. Build ISO images first, then burn and verify each disc.

| Mode | Use it for | Restore with |
| --- | --- | --- |
| **Raw** (default) | Original files on one directly readable disc | Your file manager |
| **DAR** (`-m dar`) | Compressed archives across multiple discs, including incrementals | `bd-archive extract` |

**[Documentation and guides](https://github.com/Xitee1/bd-archiver/wiki)**

## Install

Choose a native installation or the prebuilt Docker image.

### Native installation

Requires **Linux and Python 3.11+**, plus external tools for the commands you use.
For the full workflow:

```bash
# Debian / Ubuntu
sudo apt install dar par2 growisofs dvd+rw-tools genisoimage udisks2

# Arch Linux
sudo pacman -Syu dar par2cmdline dvd+rw-tools cdrtools udisks2
```

With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed,
run from this checkout:

```bash
uv tool install --editable .
bd-archive --help
```

See [Installation](https://github.com/Xitee1/bd-archiver/wiki/Installation)
for minimal dependencies, updates and shell completion.

### Docker

The prebuilt image includes Python and the runtime tools (AMD64 and ARM64):

```bash
docker pull ghcr.io/xitee1/bd-archiver:latest
docker run --rm ghcr.io/xitee1/bd-archiver:latest --help
```

See [Docker](https://github.com/Xitee1/bd-archiver/wiki/Docker) for volume mounts,
commands and the additional setup needed for drives and ISO mounting.

## Create your first disc

Insert a blank disc so its capacity can be detected. Replace the paths below;
keep the source and output separate.

```bash
# Build one disc containing directly readable files.
bd-archive create -s /data/photos -n Photos -o /data/photos-disc

# Optionally check the ISO before burning (requires working udisks2).
bd-archive verify /data/photos-disc/images/disc_0001.iso

# Burn it; verification runs automatically after burning.
bd-archive burn -i /data/photos-disc
```

`create` shows a preview and asks before building. It does not burn anything.
A single drive is detected automatically; use `-D /dev/sr0` to select one.
Use `create -b BYTES` to supply capacity without a drive.

For a folder that needs multiple discs:

```bash
bd-archive create -m dar -s /data/photos -n Photos -o /data/photos-gen1
bd-archive burn -i /data/photos-gen1
bd-archive extract -o /data/restored-photos -i /data/photos-gen1
```

Raw mode uses remaining space for PAR2; DAR defaults to zstd compression and
5% recovery. `-r 0` disables recovery but retains SHA-512 checksums. Keep DAR
catalogs and earlier generations when creating incremental archives.

## Documentation

- [Raw data discs](https://github.com/Xitee1/bd-archiver/wiki/Raw-data-discs) and [DAR archives](https://github.com/Xitee1/bd-archiver/wiki/DAR-archives)
- [Incrementals and disc packing](https://github.com/Xitee1/bd-archiver/wiki/Incrementals-and-packing)
- [Burning](https://github.com/Xitee1/bd-archiver/wiki/Burning), [verification](https://github.com/Xitee1/bd-archiver/wiki/Verification) and [restoring](https://github.com/Xitee1/bd-archiver/wiki/Restoring)
- [Storage planning](https://github.com/Xitee1/bd-archiver/wiki/Storage-and-layout), [CLI reference](https://github.com/Xitee1/bd-archiver/wiki/CLI-reference) and [troubleshooting](https://github.com/Xitee1/bd-archiver/wiki/Troubleshooting)
- [Development](https://github.com/Xitee1/bd-archiver/wiki/Development)
