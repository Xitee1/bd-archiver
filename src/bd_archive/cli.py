# PYTHON_ARGCOMPLETE_OK
import argparse
import subprocess
import sys

import argcomplete

from bd_archive import __version__
from bd_archive.commands.burn import cmd_burn
from bd_archive.commands.create import cmd_create
from bd_archive.commands.extract import cmd_extract
from bd_archive.commands.verify import cmd_verify
from bd_archive.ui.logger import Logger, log


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bd-archive",
        description="Archive files to Blu-ray with PAR2 recovery",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True, help="Available commands")

    # ── create ──────────────────────────────────────────────────────────
    cr = sub.add_parser("create", help="Build disc images", add_help=False)
    common = cr.add_argument_group("General Options")
    common.add_argument("-h", "--help", action="help", help="Show help")
    common.add_argument("-s", "--source", required=True, help="Source directory")
    common.add_argument("-n", "--name", required=True, help="Archive name")
    common.add_argument("-o", "--output", required=True, help="ISO output directory")
    common.add_argument(
        "-m",
        "--mode",
        choices=["raw", "dar"],
        default="raw",
        help="Mode (default: raw)",
    )
    common.add_argument(
        "-w",
        "--workdir",
        default=None,
        help="Scratch directory (default: <output>/.bd-archive-work/)",
    )
    common.add_argument(
        "-r",
        "--redundancy",
        type=int,
        default=None,
        help="PAR2 %% (default: raw fills free space; dar: 5%%)",
    )
    common.add_argument(
        "-D",
        "--device",
        default=None,
        help="Optical drive (default: auto-detect)",
    )
    common.add_argument(
        "-b",
        "--bytes",
        type=int,
        default=None,
        help="Disc capacity in bytes (default: auto-detect)",
    )
    common.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    cr.add_argument_group("Mode: raw", "Readable files on one disc. No extra options.")
    dar_options = cr.add_argument_group(
        "Mode: dar", "Archives across multiple discs. Requires -m dar."
    )
    dar_options.add_argument(
        "-c",
        "--compression",
        default=None,
        choices=["zstd", "lzma", "lz4", "gzip", "bzip2", "none"],
        help="Compression (default: zstd; none disables it)",
    )
    dar_options.add_argument("-l", "--level", help="Compression level")
    dar_options.add_argument(
        "--base",
        default=None,
        help="Previous catalog for an incremental archive; keep the same -n",
    )
    dar_options.add_argument(
        "--pack-with",
        default=None,
        metavar="ISO",
        help="Merge an unburned ISO into disc 1; do not burn the old ISO afterwards",
    )
    dar_options.add_argument(
        "--min-last-disc-fill",
        type=int,
        default=0,
        metavar="PERCENT",
        help="Defer newest files to reach this fill (0-100; default: 0/off). "
        "Deferred files need a later archive.",
    )
    ratio_group = dar_options.add_mutually_exclusive_group()
    ratio_group.add_argument(
        "--ratio",
        type=float,
        default=None,
        help="Preview output/input ratio (default: 1.0; 0.5 = half size)",
    )
    ratio_group.add_argument(
        "--sample",
        default=None,
        help="Measure preview ratio from this directory using -c/-l",
    )

    # ── burn ────────────────────────────────────────────────────────────
    bu = sub.add_parser("burn", help="Burn disc images (resumable)")
    bu.add_argument(
        "-i",
        "--input",
        required=True,
        help="Output directory from create",
    )
    bu.add_argument(
        "-D",
        "--device",
        default=None,
        help="Optical drive (default: auto-detect)",
    )
    bu.add_argument(
        "-S",
        "--speed",
        help="BD speed multiplier, e.g. 4 (default: maximum)",
    )
    bu.add_argument("--start", type=int, default=1, help="Start from disc N (default: 1)")
    bu.add_argument("--no-verify", action="store_true", help="Skip post-burn verification")
    bu.add_argument(
        "--skip-fit-check", action="store_true", help="Skip pre-burn disc capacity check"
    )

    # ── verify ──────────────────────────────────────────────────────────
    sub.add_parser("verify", help="Check disc integrity").add_argument(
        "target",
        nargs="?",
        default=None,
        help="Directory, device or ISO (default: auto-detect drive)",
    )

    # ── extract ─────────────────────────────────────────────────────────
    ex = sub.add_parser("extract", help="Restore archive from discs or ISO images")
    ex.add_argument("-o", "--output", required=True, help="Output directory")
    ex_source = ex.add_mutually_exclusive_group()
    ex_source.add_argument(
        "-D",
        "--device",
        default=None,
        help="Optical drive (default: auto-detect)",
    )
    ex_source.add_argument(
        "-i",
        "--iso",
        nargs="+",
        default=None,
        metavar="PATH",
        help="ISO files or create output directories (instead of a drive)",
    )
    ex.add_argument(
        "-w",
        "--workdir",
        default=None,
        help="Scratch directory (default: <output>/.bd-archive-work/)",
    )

    return p


def _dispatch(args):
    match args.command:
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
