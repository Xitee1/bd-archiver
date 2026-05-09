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
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

VERSION = "4.1.0"

MiB = 1024 * 1024

# Refuse to burn if disc capacity exceeds staging size by more than this
# factor — guards against wasting a larger disc on a smaller archive
# (e.g. a 50 GB BD-DL when the archive was sized for 25 GB BD-R).
DISC_OVERSIZE_TOLERANCE = 1.05

# Per-disc overhead beyond slice + par2 recovery: par2 index file, par2
# packet headers, par2 block rounding, sha512 hash files, README. The
# isolated dar catalog (usually the dominant item) scales with file
# count and is computed separately by estimate_catalog_size().
PAR2_AND_MISC_OVERHEAD = 4 * MiB

# Seconds to wait for a freshly burned disc to become mountable before
# giving up (drive needs to finalise + re-read TOC).
POST_BURN_MOUNT_TIMEOUT = 30

# ISO9660 caps the Primary Volume Descriptor's Volume Identifier at 32
# bytes. mkisofs/growisofs reject longer labels outright. Volume labels
# here are "<archive_name>_NNNN", so archive_name must leave room for
# the 5-char disc suffix.
ISO9660_VOLUME_LABEL_MAX = 32

# PAR2 recovery volumes are named "<base>.volNNN+NN.par2"; the index file
# is plain "<base>.par2". This pattern matches recovery volumes only.
PAR2_RECOVERY_RE = re.compile(r"\.vol\d+\+\d+\.par2$")


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
        return subprocess.run(cmd, capture_output=True, text=True, check=check)

    prefix = f"  [{label}] " if label else "  "
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
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


def is_par2_index(path: Path) -> bool:
    """True for the PAR2 index file, False for recovery volumes."""
    return path.suffix == ".par2" and not PAR2_RECOVERY_RE.search(path.name)


@dataclass
class SourceScan:
    total_bytes: int     # sum of regular file sizes
    entry_count: int     # files + dirs + symlinks + ...
    catalog_est: int     # estimated isolated dar catalog size


def scan_source(source: Path) -> SourceScan:
    """Walk source once; return size, entry count, and catalog estimate.

    Catalog estimate: dar's isolated catalog stores ~256 B per entry
    (metadata + sha512 hash + record framing) plus the relative path
    length. Used to size per-disc overhead and for capacity planning.
    """
    PER_ENTRY = 256
    HEADER = 64 * 1024
    catalog = HEADER
    total = 0
    count = 0
    for p in source.rglob("*"):
        count += 1
        try:
            rel = p.relative_to(source).as_posix()
            catalog += PER_ENTRY + len(rel.encode("utf-8"))
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except (OSError, ValueError):
            catalog += PER_ENTRY + 256
    return SourceScan(total_bytes=total, entry_count=count, catalog_est=catalog)


def measure_compression_ratio(sample: Path, compression: str,
                              level: str | None) -> float:
    """Run dar on sample with the given compression; return output/input ratio.

    Uses a temp directory for the test archive (cleaned up automatically).
    The user picks a representative subset — the ratio is only meaningful
    if the sample's file-type mix matches the full source. Small samples
    (<50 MiB) inflate the ratio because the embedded dar catalog +
    per-archive overhead become a noticeable fraction of the output.
    """
    if not sample.is_dir():
        log.error(f"Sample must be a directory: {sample}")
        sys.exit(1)

    sample_size = sum(p.stat().st_size for p in sample.rglob("*")
                      if p.is_file() and not p.is_symlink())
    if sample_size == 0:
        log.error(f"Sample {sample} contains no files")
        sys.exit(1)
    if sample_size < 50 * MiB:
        log.warn(f"Small sample ({human_bytes(sample_size)}) — ratio "
                 f"likely inflated by per-archive overhead")

    label = compression + (f":{level}" if level else "")
    log.info(f"Test-compressing {human_bytes(sample_size)} sample with {label}...")

    with tempfile.TemporaryDirectory(prefix="bd-sample-") as tmp:
        archive = Path(tmp) / "sample"
        cmd = ["dar", "-c", str(archive), "-R", str(sample),
               "--hash", "sha512", "-Q"]
        if compression != "none":
            flag = f"-z{compression}"
            if level:
                flag += f":{level}"
            cmd += [flag, "-am"]
        run(cmd, label="dar")
        output_size = sum(p.stat().st_size for p in Path(tmp).glob("*.dar"))

    ratio = output_size / sample_size
    log.ok(f"Measured ratio {ratio:.3f}: "
           f"{human_bytes(sample_size)} → {human_bytes(output_size)}")
    return ratio


def compute_slice_bytes(disc_bytes: int, catalog_est: int,
                        redundancy: int) -> int:
    """Largest slice that fits on a disc with overhead. Returns 0 if it doesn't fit."""
    per_disc_overhead = catalog_est + PAR2_AND_MISC_OVERHEAD
    if per_disc_overhead >= disc_bytes:
        return 0
    available = disc_bytes - per_disc_overhead
    slice_bytes = (available * 100 // (100 + redundancy))
    return (slice_bytes // MiB) * MiB


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

class DeviceBusyError(Exception):
    """growisofs couldn't grab the associated sg device — typically held
    by a tool like MakeMKV, K3b, or a desktop auto-mount probe."""
    def __init__(self, device: str):
        super().__init__(device)
        self.device = device


def find_sg_device(block_device: str) -> str | None:
    """Map /dev/srX → /dev/sgY via sysfs. Returns None if not found."""
    name = Path(block_device).name
    sg_dir = Path(f"/sys/block/{name}/device/scsi_generic")
    if sg_dir.is_dir():
        for entry in sg_dir.iterdir():
            return f"/dev/{entry.name}"
    return None


def find_device_holders(*devices: str) -> list[str]:
    """Return 'PID COMMAND' lines for processes holding any of the given
    devices open. Empty if lsof is unavailable or finds nothing."""
    if shutil.which("lsof") is None:
        return []
    paths = [d for d in devices if d and Path(d).exists()]
    if not paths:
        return []
    r = run(["lsof", "-Fpc", "--", *paths], capture=True, check=False)
    if r.returncode != 0 or not r.stdout:
        return []
    holders = []
    pid = None
    for line in r.stdout.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("c") and pid:
            holders.append(f"{pid} {line[1:]}")
            pid = None
    return holders


class DiscIO:
    def __init__(self, device: str):
        self.device = device

    def mount(self, preferred_dir: Path) -> Path | None:
        """Mount the disc read-only. Returns the actual mount path, or
        None on failure.

        Tries plain `mount` first (works if the user has permission via
        fstab or sudoers NOPASSWD). Falls back to `udisksctl mount`,
        which uses Polkit and works for the active desktop user without
        a password — but picks its own mount path under /run/media/...
        so the returned path may differ from preferred_dir.

        Never uses interactive sudo: an unattended verify pass shouldn't
        block on a password prompt.
        """
        preferred_dir.mkdir(parents=True, exist_ok=True)
        if run(["mount", "-o", "ro", self.device, str(preferred_dir)],
               capture=True, check=False).returncode == 0:
            return preferred_dir

        if shutil.which("udisksctl"):
            r = run(["udisksctl", "mount", "-b", self.device,
                     "--no-user-interaction"],
                    capture=True, check=False)
            if r.returncode == 0:
                # udisksctl prints "Mounted /dev/sr0 at /run/media/.../LABEL."
                m = re.search(r"^Mounted .+? at (.+?)\.?\s*$",
                              (r.stdout or "").strip(),
                              re.MULTILINE)
                if m:
                    return Path(m.group(1))
        return None

    def mount_with_retry(self, preferred_dir: Path,
                         timeout: int = POST_BURN_MOUNT_TIMEOUT) -> Path | None:
        """Poll the device until it is mountable or timeout expires.

        Useful right after a burn, where the drive needs a few seconds
        to finalise the disc and re-read the TOC.
        """
        deadline = time.monotonic() + timeout
        while True:
            mounted = self.mount(preferred_dir)
            if mounted is not None:
                return mounted
            if time.monotonic() >= deadline:
                return None
            time.sleep(1)

    def umount(self, mount_path: Path):
        if run(["umount", str(mount_path)],
               capture=True, check=False).returncode == 0:
            return
        if shutil.which("udisksctl"):
            if run(["udisksctl", "unmount", "-b", self.device,
                    "--no-user-interaction"],
                   capture=True, check=False).returncode == 0:
                return
        log.warn(f"Could not unmount {mount_path}")

    def eject(self):
        run(["eject", self.device], capture=True, check=False)

    def burn(self, source_dir: Path, volume_label: str,
             speed: str | None = None):
        # -udf: primary filesystem (native Unicode names, POSIX metadata,
        # no file-size limit). Linux/Windows/macOS all read UDF.
        # -iso-level 3: mkisofs always writes an ISO9660 bridge alongside
        # UDF; level 3 lets that bridge also hold our GiB-sized dar
        # slices via multi-extent. Without it, ISO9660 level 1 silently
        # drops files >4 GiB.
        # -use-the-force-luke=notray: skip growisofs's post-burn tray
        # eject/reload (some drives physically pop the tray, requiring
        # the user to re-insert before verify can run).
        cmd = ["growisofs", "-use-the-force-luke=notray",
               "-Z", self.device,
               "-iso-level", "3", "-udf",
               "-V", volume_label,
               "-publisher", f"bd-archive v{VERSION}",
               "-input-charset", "utf-8"]
        if speed:
            cmd += [f"-speed={speed}"]
        cmd.append(str(source_dir) + "/")

        # Stream output while watching for the "device busy" marker so
        # the caller can retry without re-running the whole script.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        assert proc.stdout is not None
        sg_locked = False
        for line in proc.stdout:
            print(f"  [burn] {line}", end="")
            if "failed to grab associated sg device" in line:
                sg_locked = True
        proc.wait()
        if proc.returncode != 0:
            if sg_locked:
                raise DeviceBusyError(self.device)
            raise subprocess.CalledProcessError(proc.returncode, cmd)


# ════════════════════════════════════════════════════════════════════════════
# SHA-512 hashes — verify dar's per-slice .sha512 files (sha512sum-compatible)
# ════════════════════════════════════════════════════════════════════════════

HASH_CHUNK_SIZE = 65536


def _hash_file_sha512(path: Path) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_dar_hashes(directory: Path) -> tuple[int, int]:
    """Verify every *.sha512 file in directory against its target.

    dar writes one sha512sum-format line per slice ("<hex>  <filename>")
    into a sibling file named "<slice>.sha512". Returns (ok, fail);
    missing or empty hash files count as fail.
    """
    ok = fail = 0
    for hash_file in sorted(directory.glob("*.sha512")):
        text = hash_file.read_text().strip()
        if not text:
            log.error(f"  Empty hash file: {hash_file.name}")
            fail += 1
            continue
        expected, filename = text.splitlines()[0].split("  ", 1)
        target = directory / filename
        if not target.exists():
            log.error(f"  Missing: {filename}")
            fail += 1
            continue
        if _hash_file_sha512(target) == expected:
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
               "-R", str(source), "-s", str(slice_bytes),
               "--hash", "sha512", "--min-digits", "4", "-Q"]
        if compression != "none":
            flag = f"-z{compression}"
            if comp_level:
                flag += f":{comp_level}"
            cmd += [flag, "-am"]
        run(cmd, label="dar")

    def isolate_catalog(self):
        run(["dar", "-C", str(self.base_path) + "-catalog",
             "-A", str(self.base_path),
             "--hash", "sha512", "--min-digits", "4", "-Q"],
            label="dar", check=True)


# ════════════════════════════════════════════════════════════════════════════
# Shared: verify_disc (used by verify, burn, and extract)
# ════════════════════════════════════════════════════════════════════════════

def verify_disc(disc_path: Path, label: str = "",
                quiet: bool = False) -> VerifyResult:
    if not quiet:
        log.step(f"Verifying: {label or disc_path}")

    worst = VerifyResult.OK

    # SHA-512 — dar emits one .sha512 file per slice (and per catalog
    # slice). PAR2 and README have no hash by design: PAR2 is
    # self-verifying and README is non-load-bearing.
    hash_files = sorted(disc_path.glob("*.sha512"))
    if hash_files:
        if not quiet:
            log.info(f"Checking SHA-512 hashes ({len(hash_files)} file(s))...")
        ok_count, fail_count = verify_dar_hashes(disc_path)
        if fail_count == 0:
            log.ok(f"SHA-512: all {ok_count} file(s) intact")
        else:
            log.error(f"SHA-512: {fail_count} file(s) corrupted!")
            worst = VerifyResult.BROKEN
    elif not quiet:
        log.warn("No .sha512 hash files found")

    # PAR2
    par2_indices = [p for p in sorted(disc_path.glob("*.par2"))
                    if is_par2_index(p)]
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
# cmd_create — prepare archive + staging (no burning)
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class ArchiveConfig:
    name: str
    disc_bytes: int
    redundancy: int
    compression: str
    comp_level: str | None

    @property
    def comp_str(self) -> str:
        return self.compression + (f" ({self.comp_level})" if self.comp_level else "")


def generate_readme(stage_dir: Path, cfg: ArchiveConfig,
                    disc_num: int, total_discs: int, slice_name: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    (stage_dir / "README.txt").write_text(
        f"BD-ARCHIVE | {cfg.name} | Disc {disc_num}/{total_discs}"
        f" | {ts} | Capacity {human_bytes(cfg.disc_bytes)}"
        f" | PAR2 {cfg.redundancy}% | {cfg.comp_str}\n\n"
        f"RESTORE:  dar -x {cfg.name} -R /target\n"
        f"VERIFY:   sha512sum -c {slice_name}.sha512\n"
        f"          par2 verify {slice_name}.par2\n"
        f"REPAIR:   par2 repair {slice_name}.par2\n"
        f"DEPENDS:  pacman -S dar par2cmdline  |  apt install dar par2\n"
    )


def cmd_create(args):
    check_deps("dar", "par2", "dvd+rw-mediainfo")

    max_name_len = ISO9660_VOLUME_LABEL_MAX - 5  # "_NNNN" suffix
    if len(args.name) > max_name_len:
        log.error(f"--name '{args.name}' is {len(args.name)} chars; "
                  f"max {max_name_len} (ISO9660 volume label limit "
                  f"{ISO9660_VOLUME_LABEL_MAX} minus 5-char disc suffix)")
        sys.exit(1)

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

    log.info("Scanning source...")
    scan = scan_source(source)

    slice_bytes = compute_slice_bytes(disc_bytes, scan.catalog_est,
                                      args.redundancy)
    if slice_bytes == 0:
        log.error(f"Per-disc overhead "
                  f"({human_bytes(scan.catalog_est + PAR2_AND_MISC_OVERHEAD)}) "
                  f"exceeds disc capacity ({human_bytes(disc_bytes)})")
        sys.exit(1)

    par2_est = slice_bytes * args.redundancy // 100
    cfg = ArchiveConfig(
        name=args.name,
        disc_bytes=disc_bytes,
        redundancy=args.redundancy,
        compression=args.compression,
        comp_level=args.level,
    )

    log.step("Configuration")
    log.info(f"Disc capacity: {human_bytes(disc_bytes)}")
    log.info(f"Slice size:    {human_bytes(slice_bytes)}")
    log.info(f"PAR2:          {cfg.redundancy}% (~{human_bytes(par2_est)})")
    log.info(f"Catalog:       ~{human_bytes(scan.catalog_est)} "
             f"({scan.entry_count} entries, estimated)")
    log.info(f"Compression:   {cfg.comp_str}")
    log.info(f"Source:        {source}")
    log.info(f"Workdir:       {work_dir}")

    dar = DarArchive(cfg.name, work_dir)

    # ── Create dar archive ──────────────────────────────────────────────
    log.step("Creating dar archive")
    dar.create(source, slice_bytes, cfg.compression, cfg.comp_level)

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
    catalog_actual = sum(c.stat().st_size for c in dar.catalog_files)
    log.ok(f"Catalog isolated ({human_bytes(catalog_actual)})")
    if catalog_actual > scan.catalog_est:
        log.warn(f"Catalog exceeds estimate by "
                 f"{human_bytes(catalog_actual - scan.catalog_est)} — "
                 f"per-disc fit check may fail")

    # ── Stage each disc ─────────────────────────────────────────────────
    log.step("Preparing disc staging directories")

    for i, slice_file in enumerate(slices, 1):
        slice_name = slice_file.name
        stage = work_dir / "staging" / f"disc_{i:04d}"

        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)

        # Slice + its dar-generated SHA-512 hash file
        shutil.copy2(slice_file, stage)
        slice_hash = Path(str(slice_file) + ".sha512")
        if slice_hash.exists():
            shutil.copy2(slice_hash, stage)

        # Isolated catalog slices + their hash files
        for cat in dar.catalog_files:
            shutil.copy2(cat, stage)
            cat_hash = Path(str(cat) + ".sha512")
            if cat_hash.exists():
                shutil.copy2(cat_hash, stage)

        # PAR2 (covers slice; PAR2 files are self-verifying)
        log.info(f"Disc {i}/{slice_count}: generating PAR2 ({cfg.redundancy}%)...")
        Par2.create(stage / slice_name, cfg.redundancy)

        # README
        generate_readme(stage, cfg, i, slice_count, slice_name)

        file_count = sum(1 for f in stage.iterdir() if f.is_file())

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

    # ── Summary ─────────────────────────────────────────────────────────
    ratio = total_archive * 100 // max(scan.total_bytes, 1)

    log.step("Summary")
    print(f"\n  Source:       {human_bytes(scan.total_bytes)}")
    print(f"  Archive:      {human_bytes(total_archive)} ({ratio}%)")
    print(f"  Discs:        {slice_count} x {human_bytes(disc_bytes)}")
    print(f"  PAR2:         {cfg.redundancy}% per disc")
    print(f"  Compression:  {cfg.comp_str}")
    print(f"  Staging:      {work_dir / 'staging'}")
    print(f"\n  Next step:    bd-archive.py burn -w {work_dir}")
    print(f"  Cleanup:      rm -rf {work_dir}\n")


# ════════════════════════════════════════════════════════════════════════════
# cmd_estimate — estimate disc count + last-disc headroom (no archive created)
# ════════════════════════════════════════════════════════════════════════════

def cmd_estimate(args):
    """Preview disc count and per-disc fill without running dar/par2.

    Compression ratio comes from one of three sources, in order of
    accuracy: --sample <path> runs dar with the given compression on a
    representative subset and measures the actual ratio; --ratio <float>
    uses a manually supplied ratio; otherwise 1.0 (worst case, no
    compression). Disc count and last-disc fill are computed with the
    same slice-sizing math as cmd_create.
    """
    if args.sample:
        check_deps("dar")

    source = Path(args.source).resolve()
    if not source.is_dir():
        log.error(f"Does not exist: {source}")
        sys.exit(1)

    if args.bytes is not None:
        raw_capacity = args.bytes
    else:
        check_deps("dvd+rw-mediainfo")
        raw_capacity = detect_disc_capacity(args.device)
        if raw_capacity is None:
            log.error(f"No disc detected at {args.device}.")
            log.info("Insert a blank disc, or specify capacity manually "
                     "with -b/--bytes <int>.")
            sys.exit(1)
    disc_bytes = raw_capacity - 2 * MiB

    log.info("Scanning source...")
    scan = scan_source(source)

    slice_bytes = compute_slice_bytes(disc_bytes, scan.catalog_est,
                                      args.redundancy)
    if slice_bytes == 0:
        log.error(f"Per-disc overhead "
                  f"({human_bytes(scan.catalog_est + PAR2_AND_MISC_OVERHEAD)}) "
                  f"exceeds disc capacity ({human_bytes(disc_bytes)})")
        sys.exit(1)

    if args.sample:
        ratio = measure_compression_ratio(
            Path(args.sample).resolve(), args.compression, args.level)
        ratio_source = f"measured from {args.sample}"
    elif args.ratio is not None:
        ratio = args.ratio
        ratio_source = "manual"
    else:
        ratio = 1.0
        ratio_source = "default (no compression assumed)"

    archive_est = int(scan.total_bytes * ratio)
    n_discs = max(1, (archive_est + slice_bytes - 1) // slice_bytes)

    # Slices 1..N-1 are exactly slice_bytes; the last slice is whatever
    # remains. If the archive is an exact multiple, last_slice = slice_bytes.
    last_slice = archive_est - (n_discs - 1) * slice_bytes
    if last_slice == 0:
        last_slice = slice_bytes

    last_disc_content = (
        last_slice
        + last_slice * args.redundancy // 100
        + scan.catalog_est
        + PAR2_AND_MISC_OVERHEAD
    )
    last_disc_free = max(0, disc_bytes - last_disc_content)
    # Convert archive-byte headroom back to raw source bytes via ratio.
    last_disc_free_raw = int(last_disc_free / max(ratio, 0.001))

    log.step("Source")
    log.info(f"Path:             {source}")
    log.info(f"Size:             {human_bytes(scan.total_bytes)} "
             f"({scan.entry_count} entries)")
    log.info(f"Catalog:          ~{human_bytes(scan.catalog_est)} (estimated)")

    log.step("Disc layout")
    log.info(f"Disc capacity:    {human_bytes(disc_bytes)}")
    log.info(f"Slice size:       {human_bytes(slice_bytes)}")
    log.info(f"PAR2 redundancy:  {args.redundancy}%")
    log.info(f"Compression:      ratio {ratio:.3f} ({ratio_source})")
    log.info(f"Estimated archive: {human_bytes(archive_est)}")

    log.step("Result")
    fill_pct = last_disc_content * 100 // disc_bytes
    print(f"\n  Discs needed:    {n_discs}")
    print(f"  Last disc fill:  {human_bytes(last_disc_content)} / "
          f"{human_bytes(disc_bytes)}  ({fill_pct}%)")
    print(f"  Free on last:    {human_bytes(last_disc_free)} archive")
    if abs(ratio - 1.0) > 0.001:
        print(f"                   ~{human_bytes(last_disc_free_raw)} raw "
              f"(at ratio {ratio:.3f})")
    print()


# ════════════════════════════════════════════════════════════════════════════
# cmd_burn — burn staged discs (resumable)
# ════════════════════════════════════════════════════════════════════════════

def cmd_burn(args):
    check_deps("growisofs", "dvd+rw-mediainfo")

    work_dir = Path(args.workdir)
    staging_root = work_dir / "staging"

    if not staging_root.is_dir():
        log.error(f"No staging directory found at {staging_root}")
        log.info("Run 'create' first to prepare the archive.")
        sys.exit(1)

    disc_dirs = sorted(
        d for d in staging_root.iterdir()
        if d.is_dir() and d.name.startswith("disc_")
    )
    disc_count = len(disc_dirs)
    if disc_count == 0:
        log.error(f"No disc_* subdirectories under {staging_root}")
        log.info("Run 'create' first to prepare the archive.")
        sys.exit(1)

    # Derive archive name from the first non-catalog .dar in the first disc.
    # Filename is "<name>.NNN.dar"; strip the slice number.
    first_disc = disc_dirs[0]
    first_dar = next(
        (p for p in first_disc.glob("*.dar") if "-catalog" not in p.name),
        None,
    )
    if first_dar is None:
        log.error(f"No dar slice found in {first_disc}")
        sys.exit(1)
    archive_name = first_dar.stem.rsplit(".", 1)[0]

    start = args.start
    if start < 1 or start > disc_count:
        log.error(f"--start must be between 1 and {disc_count}")
        sys.exit(1)

    dio = DiscIO(args.device)

    log.step("Burn staged discs")
    log.info(f"Archive:  {archive_name}")
    log.info(f"Discs:    {disc_count}")
    log.info(f"Device:   {args.device}")
    if start > 1:
        log.info(f"Resuming from disc {start}")

    for i in range(start, disc_count + 1):
        stage = staging_root / f"disc_{i:04d}"
        if not stage.exists():
            log.error(f"Staging directory not found: {stage}")
            log.info("Run 'create' first to prepare the archive.")
            sys.exit(1)

        log.step(f"Disc {i}/{disc_count}")

        stage_size = sum(f.stat().st_size for f in stage.iterdir()
                         if f.is_file())
        log.info(f"Size: {human_bytes(stage_size)}")

        prompt_disc(f"Insert blank disc {i}/{disc_count}", args.device)

        # Pre-burn fit check
        if not args.skip_fit_check:
            actual = detect_disc_capacity(args.device)
            if actual is None:
                log.warn("Could not detect disc capacity — skipping fit check")
            elif actual < stage_size:
                log.error(
                    f"Disc too small: {human_bytes(actual)} < "
                    f"staging {human_bytes(stage_size)}"
                )
                log.info(f"Resume later with: bd-archive.py burn "
                         f"-w {work_dir} --start {i}")
                sys.exit(1)
            elif actual > stage_size * DISC_OVERSIZE_TOLERANCE:
                pct_over = int((DISC_OVERSIZE_TOLERANCE - 1) * 100)
                log.error(
                    f"Disc too large: {human_bytes(actual)} > "
                    f"{human_bytes(stage_size)} + {pct_over}% — refusing "
                    f"to waste space"
                )
                log.info("Insert a smaller disc, or pass --skip-fit-check "
                         "to override.")
                log.info(f"Resume later with: bd-archive.py burn "
                         f"-w {work_dir} --start {i}")
                sys.exit(1)
            else:
                log.ok(f"Disc capacity {human_bytes(actual)} fits "
                       f"staging {human_bytes(stage_size)}")

        # Burn
        log.info("Burning...")
        suffix = f"_{i:04d}"
        volume_label = f"{archive_name}{suffix}"
        if len(volume_label) > ISO9660_VOLUME_LABEL_MAX:
            volume_label = archive_name[:ISO9660_VOLUME_LABEL_MAX - len(suffix)] + suffix
            log.warn(f"Volume label truncated to fit ISO9660 "
                     f"{ISO9660_VOLUME_LABEL_MAX}-char limit: {volume_label}")
        while True:
            try:
                dio.burn(stage, volume_label, args.speed)
                break
            except DeviceBusyError:
                log.error(f"Optical device {args.device} is locked by "
                          f"another process (growisofs couldn't grab "
                          f"the associated sg device).")
                sg = find_sg_device(args.device)
                holders = find_device_holders(args.device, sg)
                if holders:
                    log.info("Holding processes:")
                    for h in holders:
                        log.info(f"  {h}")
                else:
                    log.info("Common culprits: MakeMKV, K3b, Brasero, "
                             "or a desktop auto-mount probe.")
                resp = input("\033[1;33mClose the program, then press "
                             "Enter to retry (q = cancel): \033[0m")
                if resp.strip().lower() == "q":
                    log.warn("Cancelled by user")
                    log.info(f"Resume later with: bd-archive.py burn "
                             f"-w {work_dir} --start {i}")
                    sys.exit(1)
        log.ok(f"Disc {i} burned")

        # Post-burn verify
        verify_failed = False
        if not args.no_verify:
            log.info("Post-burn verification...")
            mount_dir = Path(tempfile.mkdtemp(prefix="bd-verify-"))
            mounted = dio.mount_with_retry(mount_dir)
            if mounted is not None:
                try:
                    result = verify_disc(mounted,
                                         f"Disc {i} (post-burn)", quiet=True)
                    if result == VerifyResult.BROKEN:
                        verify_failed = True
                        log.error("Post-burn verification failed!")
                        if not prompt_yn("Continue?", default_yes=False):
                            log.info(f"Resume later with: "
                                     f"bd-archive.py burn -w {work_dir} "
                                     f"--start {i}")
                            sys.exit(1)
                finally:
                    dio.umount(mounted)
            else:
                log.warn("Could not mount — verify manually")
            try:
                mount_dir.rmdir()
            except OSError:
                pass

        # Keep a broken disc in the drive for inspection; eject good
        # discs so the user can swap in the next blank.
        if not verify_failed:
            dio.eject()
        log.ok(f"Disc {i}/{disc_count} done")

        if i < disc_count:
            remaining = disc_count - i
            log.info(f"{remaining} disc(s) remaining. "
                     f"Resume: bd-archive.py burn -w {work_dir} "
                     f"--start {i + 1}")

    log.step("All discs burned")
    print(f"\n  Archive:  {archive_name}")
    print(f"  Discs:    {disc_count}")
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
        mounted = dio.mount(mount_dir)
        if mounted is None:
            log.error(f"Could not mount {target}")
            mount_dir.rmdir()
            sys.exit(1)
        try:
            result = verify_disc(mounted, f"Disc at {target}")
        finally:
            dio.umount(mounted)
            try:
                mount_dir.rmdir()
            except OSError:
                pass
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
        target = disc_num + 1

        # Mount retry loop — keeps prompting until mount succeeds or user gives up.
        mount_dir = Path(tempfile.mkdtemp(prefix="bd-mount-"))
        mounted: Path | None
        while True:
            prompt_disc(f"Insert disc {target}", args.device)
            mounted = dio.mount(mount_dir)
            if mounted is not None:
                break
            log.error("Could not mount disc")
            if not prompt_yn("Retry?"):
                mount_dir.rmdir()
                sys.exit(1)

        try:
            # Detect archive name on the first disc that has dar files.
            # If the user inserted the wrong disc, retry without consuming
            # the slot.
            if archive_name is None:
                dar_files = [p for p in mounted.glob("*.dar")
                             if "-catalog" not in p.name]
                if not dar_files:
                    log.error("No dar files found on disc — try another")
                    continue
                archive_name = dar_files[0].stem.rsplit(".", 1)[0]
                log.info(f"Archive detected: {archive_name}")

            disc_num = target

            # Copy catalog
            for cat in mounted.glob(f"{archive_name}-catalog.*.dar"):
                dest = staging / cat.name
                if not dest.exists():
                    shutil.copy2(cat, dest)

            # Verify
            log.info(f"Checking disc {disc_num}...")
            result = verify_disc(mounted, f"Disc {disc_num}", quiet=True)

            # Copy slices
            slices = sorted(mounted.glob(f"{archive_name}.[0-9]*.dar"))
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
                    try:
                        shutil.copy2(sp, repair_dir)
                        for pf in mounted.glob(f"{sp.name}.*par2"):
                            shutil.copy2(pf, repair_dir)

                        par2_idx = [p for p in repair_dir.glob("*.par2")
                                    if is_par2_index(p)]
                        if par2_idx and Par2.repair(par2_idx[0]):
                            shutil.copy2(repair_dir / sp.name, dest)
                            log.ok(f"{sp.name}: repaired successfully")
                        else:
                            log.error(f"{sp.name}: repair failed!")
                            if prompt_yn("Use anyway?", default_yes=False):
                                shutil.copy2(repair_dir / sp.name, dest)
                    finally:
                        shutil.rmtree(repair_dir, ignore_errors=True)

                elif result == VerifyResult.BROKEN:
                    log.error(f"{sp.name}: unrepairable damage!")
                    if prompt_yn("Copy anyway?", default_yes=False):
                        shutil.copy2(sp, dest)

                else:
                    shutil.copy2(sp, dest)
                    log.ok(f"{sp.name} copied")

        finally:
            dio.umount(mounted)
            try:
                mount_dir.rmdir()
            except OSError:
                pass
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
    catalog_base = staging / f"{archive_name}-catalog"
    has_catalog = any(staging.glob(f"{archive_name}-catalog.*.dar"))

    cmd = ["dar", "-x", str(dar_base), "-R", str(output_dir), "-O", "-Q"]
    if has_catalog:
        # -A uses the isolated catalog as rescue source — handles
        # corruption of the in-archive catalog (PAR2 covers slice bytes
        # but the embedded catalog inside the slice can still be lost
        # past PAR2's repair threshold).
        cmd += ["-A", str(catalog_base)]

    try:
        run(cmd, label="dar")
        log.ok("Extraction complete!")
    except subprocess.CalledProcessError:
        log.error("dar extraction failed")
        log.info(f"Slices are in: {staging}")
        if has_catalog:
            log.info(f"Retry without rescue catalog: "
                     f"dar -x {dar_base} -R {output_dir}")
        else:
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

    # ── estimate ────────────────────────────────────────────────────────
    es = sub.add_parser("estimate",
                        help="Estimate disc count + last-disc headroom")
    es.add_argument("-s", "--source", required=True,
                    help="Source directory")
    es.add_argument("-r", "--redundancy", type=int, default=5,
                    help="PAR2 redundancy in %% (default: 5)")
    es.add_argument("-D", "--device", default="/dev/sr0",
                    help="Optical drive for capacity detection "
                         "(default: /dev/sr0)")
    es.add_argument("-b", "--bytes", type=int, default=None,
                    help="Manual disc capacity in raw bytes "
                         "(overrides detection)")
    ratio_group = es.add_mutually_exclusive_group()
    ratio_group.add_argument("--ratio", type=float, default=None,
                             help="Manual compression ratio "
                                  "(1.0 = none, 0.5 = 50%% reduction). "
                                  "Default: 1.0 if --sample also omitted")
    ratio_group.add_argument("--sample", default=None,
                             help="Run dar on this directory with -c/-l "
                                  "and use the measured output/input ratio")
    es.add_argument("-c", "--compression", default="zstd",
                    choices=["zstd", "lzma", "lz4", "gzip", "bzip2", "none"],
                    help="Compression algorithm for --sample (default: zstd)")
    es.add_argument("-l", "--level",
                    help="Compression level for --sample")

    # ── burn ────────────────────────────────────────────────────────────
    bu = sub.add_parser("burn",
                        help="Burn staged discs (resumable)")
    bu.add_argument("-w", "--workdir", required=True,
                    help="Working directory from create step")
    bu.add_argument("-D", "--device", default="/dev/sr0",
                    help="Optical drive device (default: /dev/sr0)")
    bu.add_argument("-S", "--speed",
                    help="Burn speed as BD multiplier (e.g. 2, 4, 6); 1x = 4.5 MB/s "
                         "(default: drive/media maximum)")
    bu.add_argument("--start", type=int, default=1,
                    help="Start from disc N (default: 1)")
    bu.add_argument("--no-verify", action="store_true",
                    help="Skip post-burn verification")
    bu.add_argument("--skip-fit-check", action="store_true",
                    help="Skip pre-burn disc capacity check")

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
        case "estimate": cmd_estimate(args)
        case "burn":    cmd_burn(args)
        case "verify":  cmd_verify(args)
        case "extract": cmd_extract(args)


if __name__ == "__main__":
    main()
