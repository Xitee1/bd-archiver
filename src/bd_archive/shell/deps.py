import shutil
import sys

from bd_archive.ui.logger import log


def check_deps(*commands: str):
    missing = [c for c in commands if shutil.which(c) is None]
    if missing:
        log.error(f"Missing dependencies: {', '.join(missing)}")
        print("  Arch:   pacman -Syu dar par2cmdline libisoburn dvd+rw-tools cdrtools")
        print("  Debian: apt install dar par2 xorriso dvd+rw-tools genisoimage")
        sys.exit(1)
