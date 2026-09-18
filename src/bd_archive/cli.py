# PYTHON_ARGCOMPLETE_OK
import argparse
import subprocess
import sys

import argcomplete

from bd_archive import __version__
from bd_archive.archive.prepare import free_limit, grouping
from bd_archive.commands.burn import cmd_burn
from bd_archive.commands.create import cmd_create
from bd_archive.commands.extract import cmd_extract
from bd_archive.commands.prepare import cmd_prepare
from bd_archive.commands.verify import cmd_verify
from bd_archive.tools.burn_timeout import DEFAULT_WRITE_TIMEOUT, MAX_WRITE_TIMEOUT, write_timeout
from bd_archive.ui.logger import Logger, log


def _redundancy(value: str) -> int:
    """Normalize the explicit disable option without changing mode defaults."""
    if value.lower() == "none":
        return 0
    return int(value)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bd-archive",
        description="Build, burn and verify Blu-ray archives with PAR2 recovery. "
        "Use raw for directly readable files or dar for multi-disc archives.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True, help="Available commands")

    pr = sub.add_parser(
        "prepare",
        help="Plan and move files into disc-sized raw source folders",
        description="Group a source across raw data discs, preview alternatives, then move "
        "selected files after y/N confirmation. Dates use content metadata via ExifTool, "
        "then file modification time; folders use size-weighted date medians. "
        "No saved plan or processing history.",
    )
    pr.add_argument("-s", "--source", required=True, help="Prepared incoming files/directories")
    pr.add_argument("-o", "--output", required=True, help="New or empty destination directory")
    pr.add_argument("-n", "--name", default="Prepared", help="Name for suggested create commands")
    pr.add_argument("-b", "--bytes", type=int, help="Disc capacity in bytes; no drive required")
    pr.add_argument("-D", "--device", help="Drive for capacity detection (default: auto-detect)")
    pr.add_argument(
        "-r",
        "--redundancy",
        type=_redundancy,
        metavar="0-100|none",
        help="Reserve the same recovery setting as create (default: automatic; none disables PAR2)",
    )
    pr.add_argument(
        "--group-by",
        type=grouping,
        default="top-level",
        metavar="top-level|files|depth:N",
        help="Indivisible units (default: top-level); preserve relative paths",
    )
    pr.add_argument(
        "--max-last-free",
        type=free_limit,
        metavar="PERCENT|SIZE",
        help="Allow deferring newest units: maximum last-disc free data budget, "
        "e.g. 5 (percent), 500M (MB), 2G (GB). Omit to include everything.",
    )

    # ── create ──────────────────────────────────────────────────────────
    cr = sub.add_parser(
        "create",
        help="Prepare disc folders or ISO images without burning",
        description="Scan a source directory, preview its size and prepare size-checked discs.",
        add_help=False,
    )
    common = cr.add_argument_group("General Options")
    common.add_argument("-h", "--help", action="help", help="Show help")
    common.add_argument(
        "-s", "--source", required=True, help="Source directory, including subfolders"
    )
    common.add_argument(
        "-n",
        "--name",
        required=True,
        help="Archive name: up to 27 characters, using A-Z, a-z, 0-9, ._+-",
    )
    common.add_argument(
        "--description", default="", help="Optional archive description included in the README"
    )
    common.add_argument(
        "-o",
        "--output",
        required=True,
        help="Output directory; disc folders are saved under discs/",
    )
    common.add_argument(
        "--iso", action="store_true", help="Write ISO images under images/ instead of disc folders"
    )
    common.add_argument(
        "-m",
        "--mode",
        choices=["raw", "dar"],
        default="raw",
        help="Archive format (default: raw; see mode sections below)",
    )
    common.add_argument(
        "-w",
        "--workdir",
        default=None,
        help="Temporary build files (default: <output>/.bd-archive-work/). "
        "Use a tmpfs path for RAM storage.",
    )
    common.add_argument(
        "-r",
        "--redundancy",
        type=_redundancy,
        default=None,
        metavar="0-100|none",
        help="PAR2 recovery data, 0-100%%; 0 or none skips PAR2 "
        "(default: raw fills free space; dar: 5%%)",
    )
    common.add_argument(
        "-D",
        "--device",
        default=None,
        help="Drive for capacity detection, e.g. /dev/sr0 (default: auto-detect)",
    )
    common.add_argument(
        "-b",
        "--bytes",
        type=int,
        default=None,
        help="Override disc capacity in bytes; allows building without a drive",
    )
    common.add_argument(
        "-y", "--yes", action="store_true", help="Start after the preview without asking"
    )

    cr.add_argument_group(
        "Mode: raw",
        "Original files and PAR2 on one disc, without compression. No extra options; "
        "use prepare to split whole files first, or -m dar for sliced archives.",
    )
    dar_options = cr.add_argument_group(
        "Mode: dar",
        "Compressed archives across one or more discs, restored with extract. "
        "The following options require -m dar.",
    )
    dar_options.add_argument(
        "-c",
        "--compression",
        default=None,
        choices=["zstd", "lzma", "lz4", "gzip", "bzip2", "none"],
        help="Compression algorithm (default: zstd). Use none for uncompressed archives.",
    )
    dar_options.add_argument(
        "-l",
        "--level",
        help="Integer level: zstd 1-22, other algorithms 1-9 (DAR default: 9). "
        "Higher levels favor size over speed; e.g. -c zstd -l 3. Ignored with -c none.",
    )
    dar_options.add_argument(
        "--base",
        default=None,
        help="Previous *-catalog.0001.dar for an incremental backup of new/changed files. "
        "Keep the same archive name (-n).",
    )
    dar_options.add_argument(
        "--pack-with",
        default=None,
        metavar="ISO_OR_DIR",
        help="Pack a previous unburned ISO or disc folder into disc 1. "
        "Do not burn the original separately afterwards.",
    )
    dar_options.add_argument(
        "--min-last-disc-fill",
        type=int,
        default=0,
        metavar="PERCENT",
        help="Minimum last-disc fill, 0-100%% (default: 0/off). "
        "Defer newest files; with --base, only files absent from that catalog. "
        "Deferred files need a later archive.",
    )
    ratio_group = dar_options.add_mutually_exclusive_group()
    ratio_group.add_argument(
        "--ratio",
        type=float,
        default=None,
        help="Estimated output/input ratio for the disc-count preview only "
        "(default: 1.0; 0.5 = half size). Alternative to --sample.",
    )
    ratio_group.add_argument(
        "--sample",
        default=None,
        help="Compress this sample directory using -c/-l to estimate disc count. "
        "Alternative to --ratio.",
    )

    # ── burn ────────────────────────────────────────────────────────────
    bu = sub.add_parser(
        "burn",
        help="Burn and verify prepared discs (resumable)",
        description="Burn folders or images from create in order and verify each disc afterwards.",
    )
    bu.add_argument(
        "-i",
        "--input",
        required=True,
        help="Output directory from create, containing discs/ or images/disc_*.iso",
    )
    bu.add_argument(
        "-D",
        "--device",
        default=None,
        help="Burner device, e.g. /dev/sr0 (default: auto-detect)",
    )
    bu.add_argument(
        "-S",
        "--speed",
        help="BD speed multiplier, e.g. 4 for 4x (default: drive/media maximum)",
    )
    bu.add_argument(
        "--write-timeout",
        type=write_timeout,
        metavar="SECONDS",
        default=DEFAULT_WRITE_TIMEOUT,
        help=(
            f"Minimum timeout per drive write command, 1–{MAX_WRITE_TIMEOUT} seconds "
            f"(default: {DEFAULT_WRITE_TIMEOUT})"
        ),
    )
    bu.add_argument(
        "--start", type=int, default=1, help="Resume at disc N, counting from 1 (default: 1)"
    )
    bu.add_argument("--no-verify", action="store_true", help="Skip post-burn verification")
    bu.add_argument(
        "--skip-fit-check",
        action="store_true",
        help="Skip capacity checks, including too-small and oversized-media checks",
    )

    # ── verify ──────────────────────────────────────────────────────────
    sub.add_parser(
        "verify",
        help="Check a disc, directory or ISO with PAR2 or SHA-512",
        description="Check raw or DAR data without modifying it. "
        "Uses SHA-512 checksums when PAR2 is absent. "
        "Exit codes: 0 = OK, 1 = repairable, 2 = broken.",
    ).add_argument(
        "target",
        nargs="?",
        default=None,
        help="Mounted disc/directory, device (e.g. /dev/sr0) or .iso file. "
        "Omit to use the auto-detected drive.",
    )

    # ── extract ─────────────────────────────────────────────────────────
    ex = sub.add_parser(
        "extract",
        help="Restore DAR archives from discs, folders or ISOs",
        description="Restore all collected generations of a DAR archive, with PAR2 repair. "
        "Copy raw-disc files directly; no extraction is needed. "
        "Unrepaired corruption returns exit code 1.",
    )
    ex.add_argument(
        "-o",
        "--output",
        required=True,
        help="Restore directory: empty or a previous restore of this chain",
    )
    ex_source = ex.add_mutually_exclusive_group()
    ex_source.add_argument(
        "-D",
        "--device",
        default=None,
        help="Source drive, e.g. /dev/sr0 (default: auto-detect); alternative to --iso",
    )
    ex_source.add_argument(
        "-i",
        "--input",
        "--iso",
        dest="iso",
        nargs="+",
        default=None,
        metavar="PATH",
        help="Disc folders, ISO files or create output directories. "
        "Reads discs in sorted order; alternative to --device (--iso is a legacy alias).",
    )
    ex.add_argument(
        "-w",
        "--workdir",
        default=None,
        help="Temporary slice storage (default: <output>/.bd-archive-work/). "
        "Use a tmpfs path to keep slices in RAM.",
    )

    return p


def _dispatch(args):
    match args.command:
        case "prepare":
            cmd_prepare(args)
        case "create":
            cmd_create(args)
        case "burn":
            cmd_burn(args)
        case "verify":
            cmd_verify(args)
        case "extract":
            cmd_extract(args)


def main():
    print(f"\n{Logger._c('bold')}bd-archive{Logger._c('reset')} v{__version__}\n")

    parser = build_parser()
    argcomplete.autocomplete(parser)
    args = parser.parse_args()

    try:
        _dispatch(args)
    except KeyboardInterrupt:
        # Newline so the cancel message starts on a fresh line even when
        # ^C was caught mid-progress-bar (which uses \r without \n).
        print()
        log.warn("Cancelled by user (Ctrl+C)")
        sys.exit(130)
    except EOFError:
        # Ctrl+D at a prompt — treat the same as Ctrl+C.
        print()
        log.warn("Cancelled by user (EOF)")
        sys.exit(130)
    except subprocess.CalledProcessError as e:
        # A child tool exited non-zero. Show the bare command name +
        # exit code instead of a full traceback; the tool's own output
        # has already streamed to the terminal via shell.runner.
        tool = e.cmd[0] if e.cmd else "command"
        log.error(f"{tool} failed (exit {e.returncode})")
        sys.exit(1)
    except FileNotFoundError as e:
        log.error(str(e))
        sys.exit(1)
    except PermissionError as e:
        log.error(f"Permission denied: {e}")
        sys.exit(1)
    except ValueError as e:
        log.error(str(e))
        sys.exit(1)
