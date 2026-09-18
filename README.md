# bd-archiver

bd-archiver helps you archive folders to Blu-ray data discs, from a collection
of photos or videos to a larger backup spanning multiple discs. It combines
integrity checks and optional recovery data with a workflow that separates
archive preparation from burning.

For files you want to open directly, the default
[raw mode](https://github.com/Xitee1/bd-archiver/wiki/Raw-data-discs) keeps the
original folder and its contents on a single data disc. Use
[`prepare`](https://github.com/Xitee1/bd-archiver/wiki/Preparing-raw-discs) to
divide a larger incoming collection into disc-sized source folders first. It
previews chronological packing alternatives, moves selected files after a
yes/no confirmation, and can leave the newest material for a future session.
Then create and burn each source folder normally. For larger archives,
[DAR mode](https://github.com/Xitee1/bd-archiver/wiki/DAR-archives) supports
compression and splits data across multiple discs, with a dedicated restore
command to put the files back together.

Archives are prepared as self-contained disc folders by default; add `--iso`
to create ISO images instead. Exact filesystem sizing checks capacity without
writing an ISO. You can verify the folders and try a restore before burning.
[Burning](https://github.com/Xitee1/bd-archiver/wiki/Burning) includes automatic
post-burn verification and can be resumed from a chosen disc in the set.
Drive write commands have a minimum timeout of 180 seconds by default, adjustable
with `burn --write-timeout SECONDS`.

SHA-512 checksums help detect damaged data, while optional PAR2 recovery can
reconstruct missing or corrupted bytes within its recovery limits. The
[verification](https://github.com/Xitee1/bd-archiver/wiki/Verification) and
[restoration](https://github.com/Xitee1/bd-archiver/wiki/Restoring) guides explain
what is checked and how recovery works with discs, prepared folders or saved ISO images.

DAR archives can grow through
[incremental generations](https://github.com/Xitee1/bd-archiver/wiki/Incrementals-and-packing),
saving new and changed files while retaining earlier generations for restoration.
The same guide covers deferring files to a later archive and combining an
unburned, partly filled disc with the next archive to make better use of disc
capacity. [Storage planning](https://github.com/Xitee1/bd-archiver/wiki/Storage-and-layout)
explains the disk space needed for creation and restoration.

Multisession is not supported and is not planned for the foreseeable future.
Files cannot be added to an already burned disc; collect them before burning or
use a new disc for later additions. See the
[multisession research](https://github.com/Xitee1/bd-archiver/wiki/Multisession)
for the reasons and alternatives considered.

## Getting started

bd-archiver runs on Linux. You can
[install it natively](https://github.com/Xitee1/bd-archiver/wiki/Installation)
with Python 3.11+, the required system tools and a C compiler for installation
from source, or use the
[prebuilt Docker image](https://github.com/Xitee1/bd-archiver/wiki/Docker) for
AMD64 or ARM64. The [quick start](https://github.com/Xitee1/bd-archiver/wiki/Quick-start)
walks through creating, checking and burning your first archive.

The [wiki](https://github.com/Xitee1/bd-archiver/wiki) contains the full documentation,
including the [CLI reference](https://github.com/Xitee1/bd-archiver/wiki/CLI-reference),
[troubleshooting](https://github.com/Xitee1/bd-archiver/wiki/Troubleshooting) and
[development guide](https://github.com/Xitee1/bd-archiver/wiki/Development).
