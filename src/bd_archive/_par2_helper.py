"""dar -E hook for create: runs par2 on the slice dar just completed.

Invoked as: python3 -m bd_archive._par2_helper <path> <basename> <num> <redundancy>

dar fires -E "once the slice has been completed" during create
(verified against dar 2.7.17 man page and a 6-slice empirical run).
Substitutions:
  %p -> <path>      directory containing slices
  %b -> <basename>  archive base name (e.g. "myarchive")
  %N -> <num>       zero-padded slice number (e.g. "0001")

Running par2 here (rather than in a separate phase-3 loop in
cmd_create) leverages the OS page cache: dar's just-written slice
is still mostly in RAM, so par2's read pass costs near-zero SSD
reads. Total writes are unchanged.

A non-zero exit from this helper is reported back to dar, which
will abort the backup (and surface a non-zero status from
dar.create_sliced). cmd_create additionally checks for the
presence of .par2 files before building each ISO in phase 3.
"""
import sys
from pathlib import Path

from bd_archive.tools import par2


def main():
    if len(sys.argv) != 5:
        print(f"usage: {sys.argv[0]} <path> <basename> <num> <redundancy>",
              file=sys.stderr)
        sys.exit(2)
    path = Path(sys.argv[1])
    basename = sys.argv[2]
    num = sys.argv[3]  # zero-padded, e.g. "0001"
    redundancy = int(sys.argv[4])

    slice_path = path / f"{basename}.{num}.dar"
    if not slice_path.exists():
        print(f"_par2_helper: slice not found: {slice_path}",
              file=sys.stderr)
        sys.exit(1)
    par2.create(slice_path, redundancy)


if __name__ == "__main__":
    main()
