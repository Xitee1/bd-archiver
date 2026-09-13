"""Collect concise provenance for the programs used to create an archive."""

import platform
import re
import subprocess
from importlib.metadata import PackageNotFoundError, version

from bd_archive import __version__

# Tools without a version-only invocation are listed without a version.
_PROGRAMS = {
    "dar": ("-V", r"dar version ([^,\s]+)", "https://dar.sourceforge.io/"),
    "mkisofs": (
        "--version",
        r"(?:mkisofs|genisoimage) ([^\s]+)",
        "https://cdrtools.sourceforge.net/",
    ),
    "par2": (
        "-V",
        r"par2cmdline(?:-turbo)? version ([^\s]+)",
        "https://github.com/Parchive/par2cmdline",
    ),
    "dvd+rw-mediainfo": (None, "", "https://fy.chalmers.se/~appro/linux/DVD+RW/"),
    "udisksctl": (
        None,
        "",
        "https://www.freedesktop.org/wiki/Software/udisks/",
    ),
}


def software_info(commands: list[str]) -> str:
    """Query selected tools without a shell, device access or interactive input."""
    entries = [
        ("bd-archive", __version__, "https://github.com/Xitee1/bd-archiver"),
        ("Python", platform.python_version(), "https://www.python.org/"),
    ]
    try:
        argcomplete_version = version("argcomplete")
    except PackageNotFoundError as exc:
        raise ValueError("Cannot determine argcomplete version") from exc
    entries.append(("argcomplete", argcomplete_version, "https://github.com/kislyuk/argcomplete"))
    for command in dict.fromkeys(commands):
        flag, pattern, url = _PROGRAMS[command]
        name = command
        if flag is None:
            tool_version = ""
        else:
            try:
                result = subprocess.run(
                    [command, flag],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    timeout=5,
                    check=False,
                )
                output = result.stdout
                match = re.search(pattern, output, re.IGNORECASE)
                if result.returncode != 0:
                    raise ValueError(
                        f"Version query for {command} failed (exit {result.returncode})"
                    )
                if not match:
                    raise ValueError(f"Cannot parse version reported by {command}")
                tool_version = match[1]
                if command == "dar":
                    libdar = re.search(r"Using libdar ([^\s]+)", output)
                    if libdar:
                        entries.append(("libdar", libdar[1], "https://dar.sourceforge.io/"))
                if command == "mkisofs" and "genisoimage" in output.lower():
                    name = "genisoimage (mkisofs)"
                    url = "https://salsa.debian.org/debian/cdrkit"
                if command == "par2" and "par2cmdline-turbo" in output.lower():
                    name = "par2cmdline-turbo"
                    url = "https://github.com/animetosho/par2cmdline-turbo"
                elif command == "par2":
                    name = "par2cmdline"
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError(f"Version query for {command} failed: {exc}") from exc
        entries.append((name, tool_version, url))
    return "SOFTWARE:\n" + "\n".join(
        f"  {name}{' ' + tool_version if tool_version else ''}\n  {url}\n"
        for name, tool_version, url in entries
    )
