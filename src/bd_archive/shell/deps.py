import shutil
import sys

from bd_archive.ui.logger import log


def check_deps(*commands: str):
    missing = [c for c in commands if shutil.which(c) is None]
    if missing:
        log.error(f"Missing dependencies: {', '.join(missing)}")
        print("  Arch:   pacman -Syu dar par2cmdline dvd+rw-tools cdrtools")
        print("  Debian: apt install dar par2 growisofs genisoimage")
        if "exiftool" in missing:
            print("  ExifTool (prepare): Arch: perl-image-exiftool; Debian: libimage-exiftool-perl")
        sys.exit(1)
