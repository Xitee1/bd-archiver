#!/usr/bin/env python3
"""
bd-archive — Archive data to Blu-ray discs with dar + par2

Subcommands:
  create   Prepare archive + staging (no burning)
  burn     Burn staged discs (resumable with --start N)
  verify   Check disc integrity (SHA-256 + PAR2)
  extract  Restore archive from discs (with auto-repair)

Dependencies:
  Arch:   pacman -Syu dar par2cmdline dvd+rw-tools cdrtools
  Debian: apt install dar par2 growisofs genisoimage
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from enum import Enum
from pathlib import Path

VERSION = "3.0.0"

MiB = 1024 * 1024
METADATA_FILE = "bd-archive.json"


# ════════════════════════════════════════════════════════════════════════════
# Logger
# ════════════════════════════════════════════════════════════════════════════

class Logger:
    """Colored console output."""

    COLORS = {
        "red": "\033[0;31m", "green": "\033[0;32m",
        "yellow": "\033[1;33m", "blue": "\033[0;34m",
        "cyan": "\033[0;36m", "bold": "\033[1m", "reset": "\033[0m",
    }

    @classmethod
    def _c(cls, name: str) -> str:
        return cls.COLORS[name] if sys.stdout.isatty() else ""

    @classmethod
    def info(cls, msg: str):
        print(f"{cls._c('blue')}[INFO]{cls._c('reset')}  {msg}")

    @classmethod
    def ok(cls, msg: str):
        print(f"{cls._c('green')}[  OK]{cls._c('reset')}  {msg}")

    @classmethod
    def warn(cls, msg: str):
        print(f"{cls._c('yellow')}[WARN]{cls._c('reset')}  {msg}")

    @classmethod
    def error(cls, msg: str):
        print(f"{cls._c('red')}[ ERR]{cls._c('reset')}  {msg}", file=sys.stderr)

    @classmethod
    def step(cls, msg: str):
        print(f"\n{cls._c('cyan')}{cls._c('bold')}── {msg} ──{cls._c('reset')}")

    @classmethod
    def banner(cls, msg: str):
        b, c, r = cls._c("bold"), cls._c("cyan"), cls._c("reset")
        print(f"\n{b}{c}╔{'═' * 62}╗{r}")
        print(f"{b}{c}║  {msg:<60s}║{r}")
        print(f"{b}{c}╚{'═' * 62}╝{r}\n")


log = Logger


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def human_bytes(n: int | float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PiB"


def run(cmd: list[str], *, label: str = "", check: bool = True,
        capture: bool = False) -> subprocess.CompletedProcess:
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True)

    prefix = f"  [{label}] " if label else "  "
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        print(f"{prefix}{line}", end="")
    proc.wait()
    r = subprocess.CompletedProcess(cmd, proc.returncode)
    if check and r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, cmd)
    return r


def check_deps(*commands: str):
    missing = [c for c in commands if shutil.which(c) is None]
    if missing:
        log.error(f"Missing dependencies: {', '.join(missing)}")
        print("  Arch:   pacman -Syu dar par2cmdline dvd+rw-tools cdrtools")
        print("  Debian: apt install dar par2 growisofs genisoimage")
        sys.exit(1)


def detect_disc_capacity(device: str) -> int | None:
    """Read raw writable bytes from the inserted disc via dvd+rw-mediainfo.

    Returns None if no disc is present, the command fails, or the
    output cannot be parsed. The caller decides how to handle that
    (hard error in create, soft warn in burn).
    """
    try:
        r = run(["dvd+rw-mediainfo", device],
                capture=True, check=False)
    except FileNotFoundError:
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"Free Blocks:\s+(\d+)\*2KB", r.stdout)
    if not m:
        return None
    return int(m.group(1)) * 2048


def prompt_disc(label: str, device: str):
    log.banner(f"{label}  —  Device: {device}")
    resp = input("\033[1;33mPress Enter when ready (q = cancel): \033[0m")
    if resp.strip().lower() == "q":
        log.warn("Cancelled by user")
        sys.exit(0)
    time.sleep(3)


def prompt_yn(question: str, default_yes: bool = True) -> bool:
    hint = "Y/n" if default_yes else "y/N"
    resp = input(f"\033[1;33m{question} ({hint}): \033[0m").strip().lower()
    return resp != "n" if default_yes else resp == "y"


# ════════════════════════════════════════════════════════════════════════════
# DiscIO — mount / unmount / eject / burn
# ════════════════════════════════════════════════════════════════════════════

class DiscIO:
    def __init__(self, device: str):
        self.device = device

    def mount(self, mount_dir: Path) -> bool:
        mount_dir.mkdir(parents=True, exist_ok=True)
        for cmd in (
            ["mount", "-o", "ro", self.device, str(mount_dir)],
            ["sudo", "mount", "-o", "ro", self.device, str(mount_dir)],
        ):
            if subprocess.run(cmd, capture_output=True).returncode == 0:
                return True
        return False

    def umount(self, mount_dir: Path):
        for cmd in (["umount", str(mount_dir)],
                    ["sudo", "umount", str(mount_dir)]):
            if subprocess.run(cmd, capture_output=True).returncode == 0:
                return
        log.warn(f"Could not unmount {mount_dir}")

    def eject(self):
        subprocess.run(["eject", self.device], capture_output=True)

    def burn(self, source_dir: Path, volume_label: str,
             speed: str | None = None):
        cmd = ["growisofs", "-Z", self.device, "-r", "-J",
               "-V", volume_label,
               "-publisher", f"bd-archive v{VERSION}",
               "-input-charset", "utf-8"]
        if speed:
            cmd += [f"-speed={speed}"]
        cmd.append(str(source_dir) + "/")
        run(cmd, label="burn", check=True)


# ════════════════════════════════════════════════════════════════════════════
# Checksums — SHA-256 generate / verify (using hashlib, no shell-out)
# ════════════════════════════════════════════════════════════════════════════

class Checksums:
    FILENAME = "CHECKSUMS.sha256"

    @staticmethod
    def generate(directory: Path) -> int:
        checksum_file = directory / Checksums.FILENAME
        lines = []
        for p in sorted(directory.iterdir()):
            if p.is_file() and p.name != Checksums.FILENAME:
                h = hashlib.sha256()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                lines.append(f"{h.hexdigest()}  {p.name}")
        checksum_file.write_text("\n".join(lines) + "\n")
        return len(lines)

    @staticmethod
    def verify(directory: Path) -> tuple[int, int]:
        """Returns (ok_count, fail_count)."""
        checksum_file = directory / Checksums.FILENAME
        if not checksum_file.exists():
            return 0, 0
        ok = fail = 0
        for line in checksum_file.read_text().strip().splitlines():
            expected, filename = line.split("  ", 1)
            filepath = directory / filename
            if not filepath.exists():
                log.error(f"  Missing: {filename}")
                fail += 1
                continue
            h = hashlib.sha256()
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            if h.hexdigest() == expected:
                ok += 1
            else:
                log.error(f"  Corrupted: {filename}")
                fail += 1
        return ok, fail


# ════════════════════════════════════════════════════════════════════════════
# Par2 — create / verify / repair
# ════════════════════════════════════════════════════════════════════════════

class VerifyResult(Enum):
    OK = 0
    REPAIRABLE = 1
    BROKEN = 2


class Par2:
    @staticmethod
    def create(target_file: Path, redundancy: int):
        par2_base = target_file.parent / f"{target_file.name}.par2"
        run(["par2", "create", f"-r{redundancy}", "-n1",
             str(par2_base), str(target_file)], label="par2")

    @staticmethod
    def verify(par2_index: Path) -> VerifyResult:
        r = run(["par2", "verify", str(par2_index)],
                check=False, capture=True)
        out = r.stdout + r.stderr
        if "All files are correct" in out:
            return VerifyResult.OK
        if "Repair is required" in out:
            return VerifyResult.REPAIRABLE
        return VerifyResult.BROKEN

    @staticmethod
    def repair(par2_index: Path) -> bool:
        r = run(["par2", "repair", str(par2_index)],
                label="par2", check=False)
        return r.returncode == 0


# ════════════════════════════════════════════════════════════════════════════
# DarArchive — create / extract / isolate catalog
# ════════════════════════════════════════════════════════════════════════════

class DarArchive:
    def __init__(self, name: str, work_dir: Path):
        self.name = name
        self.dar_dir = work_dir / "dar"
        self.dar_dir.mkdir(parents=True, exist_ok=True)
        self.base_path = self.dar_dir / name

    @property
    def slices(self) -> list[Path]:
        return sorted(
            p for p in self.dar_dir.glob(f"{self.name}.[0-9]*.dar")
            if "-catalog" not in p.name
        )

    @property
    def catalog_files(self) -> list[Path]:
        return sorted(self.dar_dir.glob(f"{self.name}-catalog.*.dar"))

    def create(self, source: Path, slice_bytes: int,
               compression: str, comp_level: str | None):
        cmd = ["dar", "-c", str(self.base_path),
               "-R", str(source), "-s", str(slice_bytes), "-Q"]
        if compression != "none":
            flag = f"-z{compression}"
            if comp_level:
                flag += f":{comp_level}"
            cmd += [flag, "-am"]
        run(cmd, label="dar")

    def isolate_catalog(self):
        run(["dar", "-C", str(self.base_path) + "-catalog",
             "-A", str(self.base_path), "-Q"], label="dar", check=True)


# ════════════════════════════════════════════════════════════════════════════
# Shared: verify_disc (used by verify, burn, and extract)
# ════════════════════════════════════════════════════════════════════════════

def verify_disc(disc_path: Path, label: str = "",
                quiet: bool = False) -> VerifyResult:
    if not quiet:
        log.step(f"Verifying: {label or disc_path}")

    worst = VerifyResult.OK

    # SHA-256
    cs_file = disc_path / Checksums.FILENAME
    if cs_file.exists():
        if not quiet:
            log.info("Checking SHA-256 checksums...")
        ok_count, fail_count = Checksums.verify(disc_path)
        if fail_count == 0:
            log.ok(f"SHA-256: all {ok_count} files intact")
        else:
            log.error(f"SHA-256: {fail_count} file(s) corrupted!")
            worst = VerifyResult.BROKEN
    elif not quiet:
        log.warn("No CHECKSUMS.sha256 found")

    # PAR2
    par2_indices = [p for p in sorted(disc_path.glob("*.par2"))
                    if ".vol" not in p.name]
    for par2_index in par2_indices:
        if not quiet:
            log.info(f"PAR2 check: {par2_index.name}")
        result = Par2.verify(par2_index)
        if result == VerifyResult.OK:
            log.ok("PAR2: data intact")
        elif result == VerifyResult.REPAIRABLE:
            log.warn("PAR2: damage detected — repair possible")
            if worst == VerifyResult.OK:
                worst = VerifyResult.REPAIRABLE
        else:
            log.error("PAR2: damage detected — repair NOT possible")
            worst = VerifyResult.BROKEN

    if not par2_indices and not quiet:
        log.warn("No PAR2 files found")

    if worst == VerifyResult.OK:
        log.ok("Verification passed")
    elif worst == VerifyResult.REPAIRABLE:
        log.warn("Repair needed — can be fixed with PAR2")
    else:
        log.error("Verification FAILED")

    return worst


# ════════════════════════════════════════════════════════════════════════════
# Metadata — persisted in workdir so burn knows what create prepared
# ════════════════════════════════════════════════════════════════════════════

def save_metadata(work_dir: Path, **kwargs):
    meta_path = work_dir / METADATA_FILE
    meta_path.write_text(json.dumps(kwargs, indent=2) + "\n")


def load_metadata(work_dir: Path) -> dict:
    meta_path = work_dir / METADATA_FILE
    if not meta_path.exists():
        log.error(f"No {METADATA_FILE} found in {work_dir}")
        log.info("Run 'create' first to prepare the archive.")
        sys.exit(1)
    return json.loads(meta_path.read_text())


# ════════════════════════════════════════════════════════════════════════════
# cmd_create — prepare archive + staging (no burning)
# ════════════════════════════════════════════════════════════════════════════

def generate_readme(stage_dir: Path, archive_name: str, disc_num: int,
                    total_discs: int, slice_name: str, disc_bytes: int,
                    redundancy: int, compression: str,
                    comp_level: str | None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    comp_str = compression + (f" ({comp_level})" if comp_level else "")
    (stage_dir / "README.txt").write_text(
        f"BD-ARCHIVE | {archive_name} | Disc {disc_num}/{total_discs}"
        f" | {ts} | Capacity {human_bytes(disc_bytes)}"
        f" | PAR2 {redundancy}% | {comp_str}\n\n"
        f"RESTORE:  dar -x {archive_name} -R /target\n"
        f"VERIFY:   par2 verify {slice_name}.par2\n"
        f"REPAIR:   par2 repair {slice_name}.par2\n"
        f"DEPENDS:  pacman -S dar par2cmdline  |  apt install dar par2\n"
    )


def cmd_create(args):
    check_deps("dar", "par2", "dvd+rw-mediainfo")

    source = Path(args.source).resolve()
    if not source.is_dir():
        log.error(f"Does not exist: {source}")
        sys.exit(1)

    work_dir = Path(args.workdir)
    work_dir.mkdir(parents=True, exist_ok=True)

    if args.bytes is not None:
        raw_capacity = args.bytes
        log.info(f"Using manual capacity: {human_bytes(raw_capacity)}")
    else:
        raw_capacity = detect_disc_capacity(args.device)
        if raw_capacity is None:
            log.error(f"No disc detected at {args.device}.")
            log.info("Insert a blank disc, or specify capacity manually "
                     "with -b/--bytes <int>.")
            sys.exit(1)
        log.info(f"Detected {human_bytes(raw_capacity)} free space, "
                 f"splitting with this size")

    disc_bytes = raw_capacity - 2 * MiB  # ISO/UDF filesystem overhead

    # slice + par2(slice) + overhead = disc
    overhead = 1 * MiB + 256 * 1024
    available = disc_bytes - overhead
    slice_bytes = (available * 100 // (100 + args.redundancy))
    slice_bytes = (slice_bytes // MiB) * MiB

    par2_est = slice_bytes * args.redundancy // 100
    comp_str = args.compression + (f" (level {args.level})" if args.level else "")

    log.step("Configuration")
    log.info(f"Disc capacity: {human_bytes(disc_bytes)}")
    log.info(f"Slice size:    {human_bytes(slice_bytes)}")
    log.info(f"PAR2:          {args.redundancy}% (~{human_bytes(par2_est)})")
    log.info(f"Compression:   {comp_str}")
    log.info(f"Source:        {source}")
    log.info(f"Workdir:       {work_dir}")

    dar = DarArchive(args.name, work_dir)

    # ── Create dar archive ──────────────────────────────────────────────
    log.step("Creating dar archive")
    dar.create(source, slice_bytes, args.compression, args.level)

    slices = dar.slices
    slice_count = len(slices)
    log.ok(f"{slice_count} slice(s) created")

    total_archive = 0
    for s in slices:
        sz = s.stat().st_size
        total_archive += sz
        log.info(f"  {s.name}: {human_bytes(sz)}")
    log.info(f"Total: {human_bytes(total_archive)}")

    log.info("Isolating catalog...")
    dar.isolate_catalog()
    log.ok("Catalog isolated")

    # ── Stage each disc ─────────────────────────────────────────────────
    log.step("Preparing disc staging directories")

    for i, slice_file in enumerate(slices, 1):
        slice_name = slice_file.name
        stage = work_dir / "staging" / f"disc_{i}"

        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)

        # Slice + catalog
        shutil.copy2(slice_file, stage)
        for cat in dar.catalog_files:
            shutil.copy2(cat, stage)

        # PAR2
        log.info(f"Disc {i}/{slice_count}: generating PAR2 ({args.redundancy}%)...")
        Par2.create(stage / slice_name, args.redundancy)

        # README
        generate_readme(stage, args.name, i, slice_count, slice_name,
                        disc_bytes, args.redundancy,
                        args.compression, args.level)

        # CHECKSUMS.sha256 (last — covers everything else)
        file_count = Checksums.generate(stage)

        # Size check
        stage_size = sum(f.stat().st_size for f in stage.iterdir()
                         if f.is_file())
        pct = stage_size * 100 // disc_bytes
        log.ok(f"Disc {i}/{slice_count}: {human_bytes(stage_size)} "
               f"({pct}% of {human_bytes(disc_bytes)}), "
               f"{file_count} files")

        if stage_size > disc_bytes:
            log.error(f"Disc {i} exceeds capacity!")
            sys.exit(1)

    # ── Save metadata ───────────────────────────────────────────────────
    source_size = sum(f.stat().st_size for f in source.rglob("*")
                      if f.is_file())
    save_metadata(
        work_dir,
        version=VERSION,
        archive_name=args.name,
        source=str(source),
        source_size=source_size,
        archive_size=total_archive,
        disc_count=slice_count,
        disc_bytes=disc_bytes,
        redundancy=args.redundancy,
        compression=args.compression,
        comp_level=args.level,
        created=datetime.now().isoformat(),
    )

    # ── Summary ─────────────────────────────────────────────────────────
    ratio = total_archive * 100 // max(source_size, 1)

    log.step("Summary")
    print(f"\n  Source:       {human_bytes(source_size)}")
    print(f"  Archive:      {human_bytes(total_archive)} ({ratio}%)")
    print(f"  Discs:        {slice_count} x {human_bytes(disc_bytes)}")
    print(f"  PAR2:         {args.redundancy}% per disc")
    print(f"  Compression:  {comp_str}")
    print(f"  Staging:      {work_dir / 'staging'}")
    print(f"\n  Next step:    bd-archive.py burn -w {work_dir}")
    print(f"  Cleanup:      rm -rf {work_dir}\n")


# ════════════════════════════════════════════════════════════════════════════
# cmd_burn — burn staged discs (resumable)
# ════════════════════════════════════════════════════════════════════════════

def cmd_burn(args):
    check_deps("growisofs")

    work_dir = Path(args.workdir)
    meta = load_metadata(work_dir)

    disc_count = meta["disc_count"]
    disc_size = meta["disc_size"]
    archive_name = meta["archive_name"]
    start = args.start

    if start < 1 or start > disc_count:
        log.error(f"--start must be between 1 and {disc_count}")
        sys.exit(1)

    dio = DiscIO(args.device)

    log.step("Burn staged discs")
    log.info(f"Archive:  {archive_name}")
    log.info(f"Discs:    {disc_count} x BD-{disc_size}")
    log.info(f"Device:   {args.device}")
    if start > 1:
        log.info(f"Resuming from disc {start}")

    for i in range(start, disc_count + 1):
        stage = work_dir / "staging" / f"disc_{i}"
        if not stage.exists():
            log.error(f"Staging directory not found: {stage}")
            log.info("Run 'create' first to prepare the archive.")
            sys.exit(1)

        log.step(f"Disc {i}/{disc_count}")

        # Show contents
        stage_size = sum(f.stat().st_size for f in stage.iterdir()
                         if f.is_file())
        log.info(f"Size: {human_bytes(stage_size)}")

        prompt_disc(f"Insert blank BD-{disc_size} — "
                    f"Disc {i}/{disc_count}", args.device)

        # Burn
        log.info("Burning...")
        dio.burn(stage, f"{archive_name}_{i}", args.speed)
        log.ok(f"Disc {i} burned")

        # Post-burn verify
        if not args.no_verify:
            log.info("Post-burn verification...")
            time.sleep(5)
            mount_dir = Path(tempfile.mkdtemp(prefix="bd-verify-"))
            if dio.mount(mount_dir):
                try:
                    result = verify_disc(mount_dir,
                                         f"Disc {i} (post-burn)", quiet=True)
                    if result == VerifyResult.BROKEN:
                        log.error("Post-burn verification failed!")
                        if not prompt_yn("Continue?", default_yes=False):
                            log.info(f"Resume later with: "
                                     f"bd-archive.py burn -w {work_dir} "
                                     f"--start {i}")
                            sys.exit(1)
                finally:
                    dio.umount(mount_dir)
                    mount_dir.rmdir()
            else:
                log.warn("Could not mount — verify manually")
                mount_dir.rmdir()

        dio.eject()
        log.ok(f"Disc {i}/{disc_count} done")

        # Show resume hint if not last disc
        if i < disc_count:
            remaining = disc_count - i
            log.info(f"{remaining} disc(s) remaining. "
                     f"Resume: bd-archive.py burn -w {work_dir} --start {i + 1}")

    log.step("All discs burned")
    print(f"\n  Archive:  {archive_name}")
    print(f"  Discs:    {disc_count} x BD-{disc_size}")
    print(f"  Cleanup:  rm -rf {work_dir}\n")


# ════════════════════════════════════════════════════════════════════════════
# cmd_verify
# ════════════════════════════════════════════════════════════════════════════

def cmd_verify(args):
    check_deps("par2")
    target = Path(args.target)

    if target.is_block_device():
        dio = DiscIO(str(target))
        mount_dir = Path(tempfile.mkdtemp(prefix="bd-verify-"))
        if not dio.mount(mount_dir):
            log.error(f"Could not mount {target}")
            mount_dir.rmdir()
            sys.exit(1)
        try:
            result = verify_disc(mount_dir, f"Disc at {target}")
        finally:
            dio.umount(mount_dir)
            mount_dir.rmdir()
        sys.exit(result.value)

    elif target.is_dir():
        result = verify_disc(target)
        sys.exit(result.value)

    else:
        log.error(f"Path does not exist: {target}")
        sys.exit(1)


# ════════════════════════════════════════════════════════════════════════════
# cmd_extract — restore archive from discs with auto-repair
# ════════════════════════════════════════════════════════════════════════════

def cmd_extract(args):
    check_deps("dar", "par2")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    work_dir = Path(args.workdir) if args.workdir else Path(
        tempfile.mkdtemp(prefix="bd-extract-"))
    staging = work_dir / "slices"
    staging.mkdir(parents=True, exist_ok=True)

    dio = DiscIO(args.device)

    log.step("Restore archive from discs")
    log.info(f"Device:   {args.device}")
    log.info(f"Output:   {output_dir}")
    log.info(f"Staging:  {staging}")

    archive_name = None
    disc_num = 0

    while True:
        disc_num += 1
        prompt_disc(f"Insert disc {disc_num}", args.device)

        mount_dir = Path(tempfile.mkdtemp(prefix="bd-mount-"))
        if not dio.mount(mount_dir):
            log.error("Could not mount disc")
            mount_dir.rmdir()
            if prompt_yn("Retry?"):
                disc_num -= 1
                continue
            sys.exit(1)

        try:
            # Detect archive name
            if archive_name is None:
                dar_files = [p for p in mount_dir.glob("*.dar")
                             if "-catalog" not in p.name]
                if not dar_files:
                    log.error("No dar files found on disc")
                    disc_num -= 1
                    continue
                archive_name = dar_files[0].stem.rsplit(".", 1)[0]
                log.info(f"Archive detected: {archive_name}")

            # Copy catalog
            for cat in mount_dir.glob(f"{archive_name}-catalog.*.dar"):
                dest = staging / cat.name
                if not dest.exists():
                    shutil.copy2(cat, dest)

            # Verify
            log.info(f"Checking disc {disc_num}...")
            result = verify_disc(mount_dir, f"Disc {disc_num}", quiet=True)

            # Copy slices
            slices = sorted(mount_dir.glob(f"{archive_name}.[0-9]*.dar"))
            slices = [s for s in slices if "-catalog" not in s.name]

            for sp in slices:
                dest = staging / sp.name
                if dest.exists():
                    log.info(f"{sp.name} already present — skipping")
                    continue

                if result == VerifyResult.REPAIRABLE:
                    log.warn(f"{sp.name}: damage detected — repairing...")
                    repair_dir = work_dir / f"repair_{disc_num}"
                    repair_dir.mkdir(exist_ok=True)
                    shutil.copy2(sp, repair_dir)
                    for pf in mount_dir.glob(f"{sp.name}.*par2"):
                        shutil.copy2(pf, repair_dir)

                    par2_idx = [p for p in repair_dir.glob("*.par2")
                                if ".vol" not in p.name]
                    if par2_idx and Par2.repair(par2_idx[0]):
                        shutil.copy2(repair_dir / sp.name, dest)
                        log.ok(f"{sp.name}: repaired successfully")
                    else:
                        log.error(f"{sp.name}: repair failed!")
                        if prompt_yn("Use anyway?", default_yes=False):
                            shutil.copy2(repair_dir / sp.name, dest)
                    shutil.rmtree(repair_dir)

                elif result == VerifyResult.BROKEN:
                    log.error(f"{sp.name}: unrepairable damage!")
                    if prompt_yn("Copy anyway?", default_yes=False):
                        shutil.copy2(sp, dest)

                else:
                    shutil.copy2(sp, dest)
                    log.ok(f"{sp.name} copied")

        finally:
            dio.umount(mount_dir)
            mount_dir.rmdir()
            dio.eject()

        collected = sorted(staging.glob(f"{archive_name}.[0-9]*.dar"))
        collected = [c for c in collected if "-catalog" not in c.name]
        log.info(f"Collected: {len(collected)} slice(s)")

        if not prompt_yn("Insert another disc?"):
            break

    # ── Extract ─────────────────────────────────────────────────────────
    log.step("Extracting archive")
    collected = [c for c in sorted(staging.glob(f"{archive_name}.[0-9]*.dar"))
                 if "-catalog" not in c.name]
    log.info(f"Slices: {len(collected)}")
    log.info(f"Output: {output_dir}")

    dar_base = staging / archive_name
    try:
        run(["dar", "-x", str(dar_base), "-R", str(output_dir), "-O", "-Q"],
            label="dar")
        log.ok("Extraction complete!")
    except subprocess.CalledProcessError:
        log.error("dar extraction failed")
        log.info(f"Slices are in: {staging}")
        log.info(f"Manual: dar -x {dar_base} -R {output_dir}")
        sys.exit(1)

    total = sum(f.stat().st_size for f in output_dir.rglob("*") if f.is_file())
    log.step("Restore complete")
    print(f"\n  Archive: {archive_name}")
    print(f"  Slices:  {len(collected)}")
    print(f"  Discs:   {disc_num}")
    print(f"  Output:  {output_dir}")
    print(f"  Size:    {human_bytes(total)}")
    print(f"\n  Cleanup staging: rm -rf {work_dir}\n")


# ════════════════════════════════════════════════════════════════════════════
# Argument parsing
# ════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bd-archive",
        description="Archive data to Blu-ray discs with dar + par2",
    )
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {VERSION}")
    sub = p.add_subparsers(dest="command", required=True,
                           help="Available commands")

    # ── create ──────────────────────────────────────────────────────────
    cr = sub.add_parser("create",
                        help="Prepare archive + staging (no burning)")
    cr.add_argument("-s", "--source", required=True,
                    help="Source directory")
    cr.add_argument("-n", "--name", required=True,
                    help="Archive name")
    cr.add_argument("-w", "--workdir", required=True,
                    help="Working directory for archive + staging")
    cr.add_argument("-r", "--redundancy", type=int, default=5,
                    help="PAR2 redundancy in %% (default: 5)")
    cr.add_argument("-D", "--device", default="/dev/sr0",
                    help="Optical drive for capacity detection "
                         "(default: /dev/sr0)")
    cr.add_argument("-b", "--bytes", type=int, default=None,
                    help="Manual disc capacity in raw bytes "
                         "(overrides detection)")
    cr.add_argument("-c", "--compression", default="zstd",
                    choices=["zstd", "lzma", "lz4", "gzip", "bzip2", "none"],
                    help="Compression algorithm (default: zstd)")
    cr.add_argument("-l", "--level",
                    help="Compression level")

    # ── burn ────────────────────────────────────────────────────────────
    bu = sub.add_parser("burn",
                        help="Burn staged discs (resumable)")
    bu.add_argument("-w", "--workdir", required=True,
                    help="Working directory from create step")
    bu.add_argument("-D", "--device", default="/dev/sr0",
                    help="Optical drive device (default: /dev/sr0)")
    bu.add_argument("-S", "--speed",
                    help="Burn speed")
    bu.add_argument("--start", type=int, default=1,
                    help="Start from disc N (default: 1)")
    bu.add_argument("--no-verify", action="store_true",
                    help="Skip post-burn verification")

    # ── verify ──────────────────────────────────────────────────────────
    sub.add_parser("verify",
                   help="Check disc integrity").add_argument(
        "target", help="Mount point, directory, or block device")

    # ── extract ─────────────────────────────────────────────────────────
    ex = sub.add_parser("extract",
                        help="Restore archive from discs")
    ex.add_argument("-o", "--output", required=True,
                    help="Output directory")
    ex.add_argument("-D", "--device", default="/dev/sr0",
                    help="Optical drive device (default: /dev/sr0)")
    ex.add_argument("-w", "--workdir",
                    help="Staging directory (default: auto in /tmp)")

    return p


# ════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════

def main():
    print(f"\n{Logger._c('bold')}bd-archive{Logger._c('reset')} v{VERSION}\n")

    parser = build_parser()
    args = parser.parse_args()

    match args.command:
        case "create":  cmd_create(args)
        case "burn":    cmd_burn(args)
        case "verify":  cmd_verify(args)
        case "extract": cmd_extract(args)


if __name__ == "__main__":
    main()
