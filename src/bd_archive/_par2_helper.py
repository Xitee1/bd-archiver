"""dar -E hook for create: runs par2 on the slice dar just completed.

Invoked as: python3 -m bd_archive._par2_helper <num> <redundancy>

dar fires -E "once the slice has been completed" during create
(verified against dar 2.7.17 man page and a 6-slice empirical run).
%N is substituted by dar into <num> (zero-padded slice number, e.g.
"0001"). The slice directory and archive basename arrive via the
environment — set by cmd_create and inherited through dar's /bin/sh —
because dar substitutes %p/%b literally into the shell command, where
no quoting scheme survives paths containing quotes, '$' or backticks:

  BD_ARCHIVE_SLICE_DIR       directory containing slices
  BD_ARCHIVE_SLICE_BASENAME  archive base name (e.g. "myarchive-gen1")

Running par2 here (rather than in a separate phase-3 loop in
cmd_create) leverages the OS page cache: dar's just-written slice
is still mostly in RAM, so par2's read pass costs near-zero SSD
reads. Total writes are unchanged.

A non-zero exit from this helper is reported back to dar, which
will abort the backup (and surface a non-zero status from
dar.create_sliced). cmd_create additionally checks for the
presence of .par2 files before building each ISO in phase 3.
"""

import os
import sys
from pathlib import Path

from bd_archive.tools import par2


def main():
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <num> <redundancy>", file=sys.stderr)
        sys.exit(2)
    num = sys.argv[1]  # zero-padded, e.g. "0001"
    redundancy = int(sys.argv[2])

    slice_dir = os.environ.get("BD_ARCHIVE_SLICE_DIR")
    basename = os.environ.get("BD_ARCHIVE_SLICE_BASENAME")
    if not slice_dir or not basename:
        print(
            "_par2_helper: BD_ARCHIVE_SLICE_DIR / BD_ARCHIVE_SLICE_BASENAME "
            "not set (must be invoked via dar's -E hook from cmd_create)",
            file=sys.stderr,
        )
        sys.exit(2)

    slice_path = Path(slice_dir) / f"{basename}.{num}.dar"
    if not slice_path.exists():
        print(f"_par2_helper: slice not found: {slice_path}", file=sys.stderr)
        sys.exit(1)
    par2.create(slice_path, redundancy)


if __name__ == "__main__":
    main()
