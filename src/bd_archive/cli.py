import argparse

from bd_archive import __version__
from bd_archive.commands.burn import cmd_burn
from bd_archive.commands.create import cmd_create
from bd_archive.commands.extract import cmd_extract
from bd_archive.commands.verify import cmd_verify
from bd_archive.ui.logger import Logger


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bd-archive",
        description="Archive data to Blu-ray discs with dar + par2",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True, help="Available commands")

    # ── create ──────────────────────────────────────────────────────────
    cr = sub.add_parser("create", help="Prepare archive + staging (no burning)")
    cr.add_argument("-s", "--source", required=True, help="Source directory")
    cr.add_argument("-n", "--name", required=True, help="Archive name")
    cr.add_argument("-o", "--output", required=True, help="Output directory for ISO images")
    cr.add_argument(
        "-w",
        "--workdir",
        default=None,
        help="Workdir for transient build files "
        "(default: <output>/.bd-archive-work/; specify a "
        "tmpfs path here to keep scratch off disk)",
    )
    cr.add_argument(
        "-r", "--redundancy", type=int, default=5, help="PAR2 redundancy in %% (default: 5)"
    )
    cr.add_argument(
        "-D",
        "--device",
        default=None,
        help="Optical drive for capacity detection (auto-detected if omitted)",
    )
    cr.add_argument(
        "-b",
        "--bytes",
        type=int,
        default=None,
        help="Manual disc capacity in raw bytes (overrides detection)",
    )
    cr.add_argument(
        "-c",
        "--compression",
        default="zstd",
        choices=["zstd", "lzma", "lz4", "gzip", "bzip2", "none"],
        help="Compression algorithm (default: zstd)",
    )
    cr.add_argument("-l", "--level", help="Compression level")
    ratio_group = cr.add_mutually_exclusive_group()
    ratio_group.add_argument(
        "--ratio",
        type=float,
        default=None,
        help="Manual compression ratio "
        "(1.0 = none, 0.5 = 50%% reduction). "
        "Used for the disc-count preview only. "
        "Default: 1.0 if --sample also omitted",
    )
    ratio_group.add_argument(
        "--sample",
        default=None,
        help="Run dar on this directory with -c/-l "
        "and use the measured output/input ratio "
        "for the disc-count preview",
    )
    cr.add_argument(
        "-y", "--yes", action="store_true", help="Skip the pre-archive confirmation prompt"
    )

    # ── burn ────────────────────────────────────────────────────────────
    bu = sub.add_parser("burn", help="Burn staged discs (resumable)")
    bu.add_argument(
        "-i",
        "--input",
        required=True,
        help="Input directory from create step (contains images/disc_*.iso)",
    )
    bu.add_argument(
        "-D",
        "--device",
        default=None,
        help="Optical drive device (auto-detected if omitted)",
    )
    bu.add_argument(
        "-S",
        "--speed",
        help="Burn speed as BD multiplier (e.g. 2, 4, 6); 1x = 4.5 MB/s "
        "(default: drive/media maximum)",
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
        help="Mount point, directory, block device, or ISO file "
        "(auto-detects an optical drive if omitted)",
    )

    # ── extract ─────────────────────────────────────────────────────────
    ex = sub.add_parser("extract", help="Restore archive from discs")
    ex.add_argument("-o", "--output", required=True, help="Output directory")
    ex.add_argument(
        "-D",
        "--device",
        default=None,
        help="Optical drive device (auto-detected if omitted)",
    )
    ex.add_argument(
        "-w",
        "--workdir",
        default=None,
        help="Workdir for staged slices (default: "
        "<output>/.bd-archive-work/; specify a tmpfs "
        "path here to keep scratch off disk)",
    )

    return p


def main():
    print(f"\n{Logger._c('bold')}bd-archive{Logger._c('reset')} v{__version__}\n")

    parser = build_parser()
    args = parser.parse_args()

    match args.command:
        case "create":
            cmd_create(args)
        case "burn":
            cmd_burn(args)
        case "verify":
            cmd_verify(args)
        case "extract":
            cmd_extract(args)
