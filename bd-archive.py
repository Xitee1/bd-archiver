#!/usr/bin/env python3
"""
bd-archive — Archive data to Blu-ray discs with dar + par2

Subcommands:
  create   Archive a directory onto Blu-ray discs
  verify   Check disc integrity (SHA-256 + PAR2)
  extract  Restore archive from discs (with auto-repair)

Dependencies:
  Arch:   pacman -Syu dar par2cmdline dvd+rw-tools cdrtools
  Debian: apt install dar par2 growisofs genisoimage
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from enum import Enum
from pathlib import Path

VERSION = "2.0.0"

# ════════════════════════════════════════════════════════════════════════════
# Disc capacities
# ════════════════════════════════════════════════════════════════════════════

# Raw data area minus 2 MiB filesystem overhead
DISC_CAPACITY = {
    25:  25_025_314_816 - 2 * 1024 * 1024,
    50:  50_050_629_632 - 2 * 1024 * 1024,
    100: 100_101_259_264 - 2 * 1024 * 1024,
}

MiB = 1024 * 1024


# ════════════════════════════════════════════════════════════════════════════
# Logger
# ════════════════════════════════════════════════════════════════════════════

class Logger:
    """Colored console output."""

    COLORS = {
        "red": "\033[0;31m",
        "green": "\033[0;32m",
        "yellow": "\033[1;33m",
        "blue": "\033[0;34m",
        "cyan": "\033[0;36m",
        "bold": "\033[1m",
        "reset": "\033[0m",
    }

    @classmethod
    def _use_color(cls) -> bool:
        return sys.stdout.isatty()

    @classmethod
    def _c(cls, name: str) -> str:
        return cls.COLORS[name] if cls._use_color() else ""

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

def human_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def run(cmd: list[str], *, label: str = "", check: bool = True,
        capture: bool = False) -> subprocess.CompletedProcess:
    """Run an external command with optional prefixed output."""
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True)
    else:
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
    """Ensure external commands are available."""
    missing = [c for c in commands if shutil.which(c) is None]
    if missing:
        log.error(f"Fehlende Abhängigkeiten: {', '.join(missing)}")
        print("  Arch:   pacman -Syu dar par2cmdline dvd+rw-tools cdrtools")
        print("  Debian: apt install dar par2 growisofs genisoimage")
        sys.exit(1)


def prompt_disc(label: str, device: str):
    """Prompt user to insert a disc and wait."""
    log.banner(f"{label}  —  Gerät: {device}")
    resp = input(f"\033[1;33mEnter wenn bereit (q = Abbrechen): \033[0m")
    if resp.strip().lower() == "q":
        log.warn("Abgebrochen")
        sys.exit(0)
    import time
    time.sleep(3)


def prompt_yn(question: str, default_yes: bool = True) -> bool:
    """Ask a yes/no question."""
    hint = "J/n" if default_yes else "j/N"
    resp = input(f"\033[1;33m{question} ({hint}): \033[0m").strip().lower()
    if default_yes:
        return resp != "n"
    return resp == "j"


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
        for cmd in (
            ["umount", str(mount_dir)],
            ["sudo", "umount", str(mount_dir)],
        ):
            if subprocess.run(cmd, capture_output=True).returncode == 0:
                return
        log.warn(f"Konnte {mount_dir} nicht unmounten")

    def eject(self):
        subprocess.run(["eject", self.device], capture_output=True)

    def burn(self, source_dir: Path, volume_label: str, speed: str | None = None):
        cmd = [
            "growisofs", "-Z", self.device,
            "-r", "-J",
            "-V", volume_label,
            "-publisher", f"bd-archive v{VERSION}",
            "-input-charset", "utf-8",
        ]
        if speed:
            cmd += [f"-speed={speed}"]
        cmd.append(str(source_dir) + "/")

        run(cmd, label="burn", check=True)


# ════════════════════════════════════════════════════════════════════════════
# Checksums — SHA-256 generate / verify
# ════════════════════════════════════════════════════════════════════════════

class Checksums:
    FILENAME = "CHECKSUMS.sha256"

    @staticmethod
    def generate(directory: Path):
        """Generate CHECKSUMS.sha256 for all files in directory."""
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
        """Verify CHECKSUMS.sha256, returns (ok_count, fail_count)."""
        checksum_file = directory / Checksums.FILENAME
        if not checksum_file.exists():
            return 0, 0

        ok = fail = 0
        for line in checksum_file.read_text().strip().splitlines():
            expected_hash, filename = line.split("  ", 1)
            filepath = directory / filename
            if not filepath.exists():
                log.error(f"  Fehlt: {filename}")
                fail += 1
                continue
            h = hashlib.sha256()
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            if h.hexdigest() == expected_hash:
                ok += 1
            else:
                log.error(f"  Beschädigt: {filename}")
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
        """Return sorted list of slice files (excluding catalog)."""
        return sorted(
            p for p in self.dar_dir.glob(f"{self.name}.[0-9]*.dar")
            if "-catalog" not in p.name
        )

    @property
    def catalog_files(self) -> list[Path]:
        return sorted(self.dar_dir.glob(f"{self.name}-catalog.*.dar"))

    def create(self, source: Path, slice_bytes: int,
               compression: str, comp_level: str | None):
        cmd = [
            "dar", "-c", str(self.base_path),
            "-R", str(source),
            "-s", str(slice_bytes),
            "-Q",
        ]
        if compression != "none":
            flag = f"-z{compression}"
            if comp_level:
                flag += f":{comp_level}"
            cmd += [flag, "-am"]

        run(cmd, label="dar")

    def isolate_catalog(self):
        run(["dar", "-C", str(self.base_path) + "-catalog",
             "-A", str(self.base_path), "-Q"],
            label="dar", check=True)

    def extract(self, staging_dir: Path, output_dir: Path):
        base = staging_dir / self.name
        run(["dar", "-x", str(base), "-R", str(output_dir), "-O", "-Q"],
            label="dar")


# ════════════════════════════════════════════════════════════════════════════
# Shared: verify_disc
# ════════════════════════════════════════════════════════════════════════════

def verify_disc(disc_path: Path, label: str = "",
                quiet: bool = False) -> VerifyResult:
    """
    Verify a disc directory. Used by all three subcommands.
    Returns OK, REPAIRABLE, or BROKEN.
    """
    if not quiet:
        log.step(f"Verifiziere: {label or disc_path}")

    worst = VerifyResult.OK

    # ── SHA-256 ─────────────────────────────────────────────────────────
    cs_file = disc_path / Checksums.FILENAME
    if cs_file.exists():
        if not quiet:
            log.info("SHA-256-Prüfung...")
        ok_count, fail_count = Checksums.verify(disc_path)
        if fail_count == 0:
            log.ok(f"SHA-256: {ok_count} Dateien intakt")
        else:
            log.error(f"SHA-256: {fail_count} Datei(en) beschädigt!")
            worst = VerifyResult.BROKEN
    elif not quiet:
        log.warn("Keine CHECKSUMS.sha256 gefunden")

    # ── PAR2 ────────────────────────────────────────────────────────────
    par2_indices = sorted(disc_path.glob("*.par2"))
    par2_indices = [p for p in par2_indices if ".vol" not in p.name]

    for par2_index in par2_indices:
        if not quiet:
            log.info(f"PAR2-Prüfung: {par2_index.name}")
        result = Par2.verify(par2_index)
        if result == VerifyResult.OK:
            log.ok("PAR2: Daten intakt")
        elif result == VerifyResult.REPAIRABLE:
            log.warn("PAR2: Beschädigung erkannt — Reparatur möglich")
            if worst == VerifyResult.OK:
                worst = VerifyResult.REPAIRABLE
        else:
            log.error("PAR2: Beschädigung erkannt — Reparatur NICHT möglich")
            worst = VerifyResult.BROKEN

    if not par2_indices and not quiet:
        log.warn("Keine PAR2-Dateien gefunden")

    # ── Summary ─────────────────────────────────────────────────────────
    if worst == VerifyResult.OK:
        log.ok("Verifikation bestanden")
    elif worst == VerifyResult.REPAIRABLE:
        log.warn("Reparatur nötig — kann mit PAR2 behoben werden")
    else:
        log.error("Verifikation FEHLGESCHLAGEN")

    return worst


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
            log.error(f"Konnte {target} nicht mounten")
            mount_dir.rmdir()
            sys.exit(1)
        try:
            result = verify_disc(mount_dir, f"Disc in {target}")
        finally:
            dio.umount(mount_dir)
            mount_dir.rmdir()
        sys.exit(result.value)

    elif target.is_dir():
        result = verify_disc(target)
        sys.exit(result.value)

    else:
        log.error(f"Existiert nicht: {target}")
        sys.exit(1)


# ════════════════════════════════════════════════════════════════════════════
# cmd_extract
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

    log.step("Archiv von Discs wiederherstellen")
    log.info(f"Laufwerk: {args.device}")
    log.info(f"Ziel:     {output_dir}")
    log.info(f"Staging:  {staging}")

    archive_name = None
    disc_num = 0

    while True:
        disc_num += 1
        prompt_disc(f"Disc {disc_num} einlegen", args.device)

        mount_dir = Path(tempfile.mkdtemp(prefix="bd-mount-"))
        if not dio.mount(mount_dir):
            log.error("Konnte Disc nicht mounten")
            mount_dir.rmdir()
            if prompt_yn("Nochmal versuchen?"):
                disc_num -= 1
                continue
            sys.exit(1)

        try:
            # Detect archive name
            if archive_name is None:
                dar_files = [p for p in mount_dir.glob("*.dar")
                             if "-catalog" not in p.name]
                if not dar_files:
                    log.error("Keine dar-Dateien auf der Disc")
                    disc_num -= 1
                    continue
                # name.0001.dar → name
                archive_name = dar_files[0].stem.rsplit(".", 1)[0]
                log.info(f"Archiv erkannt: {archive_name}")

            # Copy catalog
            for cat in mount_dir.glob(f"{archive_name}-catalog.*.dar"):
                dest = staging / cat.name
                if not dest.exists():
                    shutil.copy2(cat, dest)

            # Verify
            log.info(f"Prüfe Disc {disc_num}...")
            result = verify_disc(mount_dir, f"Disc {disc_num}", quiet=True)

            # Copy slices
            slices = sorted(mount_dir.glob(f"{archive_name}.[0-9]*.dar"))
            slices = [s for s in slices if "-catalog" not in s.name]

            for slice_path in slices:
                dest = staging / slice_path.name
                if dest.exists():
                    log.info(f"{slice_path.name} bereits vorhanden")
                    continue

                if result == VerifyResult.REPAIRABLE:
                    log.warn(f"{slice_path.name}: Beschädigung — repariere...")
                    repair_dir = work_dir / f"repair_{disc_num}"
                    repair_dir.mkdir(exist_ok=True)

                    shutil.copy2(slice_path, repair_dir)
                    for pf in mount_dir.glob(f"{slice_path.name}.*par2"):
                        shutil.copy2(pf, repair_dir)

                    par2_idx = list(repair_dir.glob("*.par2"))
                    par2_idx = [p for p in par2_idx if ".vol" not in p.name]

                    repaired = par2_idx and Par2.repair(par2_idx[0])
                    if repaired:
                        shutil.copy2(repair_dir / slice_path.name, dest)
                        log.ok(f"{slice_path.name}: repariert")
                    else:
                        log.error(f"{slice_path.name}: Reparatur fehlgeschlagen!")
                        if prompt_yn("Trotzdem verwenden?", default_yes=False):
                            shutil.copy2(repair_dir / slice_path.name, dest)
                    shutil.rmtree(repair_dir)

                elif result == VerifyResult.BROKEN:
                    log.error(f"{slice_path.name}: nicht reparierbar!")
                    if prompt_yn("Trotzdem kopieren?", default_yes=False):
                        shutil.copy2(slice_path, dest)

                else:
                    shutil.copy2(slice_path, dest)
                    log.ok(f"{slice_path.name} kopiert")

        finally:
            dio.umount(mount_dir)
            mount_dir.rmdir()
            dio.eject()

        # Status
        collected = sorted(staging.glob(f"{archive_name}.[0-9]*.dar"))
        collected = [c for c in collected if "-catalog" not in c.name]
        log.info(f"Gesammelt: {len(collected)} Slice(s)")

        if not prompt_yn("Weitere Disc?"):
            break

    # ── Extract ─────────────────────────────────────────────────────────
    log.step("Extrahiere Archiv")

    collected = sorted(staging.glob(f"{archive_name}.[0-9]*.dar"))
    collected = [c for c in collected if "-catalog" not in c.name]
    log.info(f"Slices: {len(collected)}")
    log.info(f"Ziel:   {output_dir}")

    dar = DarArchive(archive_name, work_dir)
    # Slices are already in staging, dar expects them relative to base path
    # Create symlink or just use the staging dir directly
    dar_base = staging / archive_name
    try:
        run(["dar", "-x", str(dar_base), "-R", str(output_dir), "-O", "-Q"],
            label="dar")
        log.ok("Extraktion erfolgreich!")
    except subprocess.CalledProcessError:
        log.error("dar-Extraktion fehlgeschlagen")
        log.info(f"Slices liegen in: {staging}")
        log.info(f"Manuell: dar -x {dar_base} -R {output_dir}")
        sys.exit(1)

    # Summary
    total = sum(f.stat().st_size for f in output_dir.rglob("*") if f.is_file())
    log.step("Wiederherstellung abgeschlossen")
    print(f"\n  Archiv:  {archive_name}")
    print(f"  Slices:  {len(collected)}")
    print(f"  Discs:   {disc_num}")
    print(f"  Ziel:    {output_dir}")
    print(f"  Größe:   {human_bytes(total)}")
    print(f"\n  Staging aufräumen: rm -rf {work_dir}\n")


# ════════════════════════════════════════════════════════════════════════════
# cmd_create
# ════════════════════════════════════════════════════════════════════════════

def generate_readme(stage_dir: Path, archive_name: str, disc_num: int,
                    total_discs: int, slice_name: str, disc_size: int,
                    redundancy: int, compression: str, comp_level: str | None):
    """Write a minimal README to the staging directory."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    comp_str = compression + (f" ({comp_level})" if comp_level else "")
    readme = stage_dir / "README.txt"
    readme.write_text(
        f"BD-ARCHIVE | {archive_name} | Disc {disc_num}/{total_discs}"
        f" | {ts} | BD-{disc_size} | PAR2 {redundancy}% | {comp_str}\n"
        f"\n"
        f"RESTORE:  dar -x {archive_name} -R /ziel\n"
        f"VERIFY:   par2 verify {slice_name}.par2\n"
        f"REPAIR:   par2 repair {slice_name}.par2\n"
        f"DEPENDS:  pacman -S dar par2cmdline  |  apt install dar par2\n"
    )


def cmd_create(args):
    check_deps("dar", "par2", "growisofs")

    source = Path(args.source).resolve()
    if not source.is_dir():
        log.error(f"Existiert nicht: {source}")
        sys.exit(1)

    disc_bytes = DISC_CAPACITY[args.disc_size]

    # Calculate slice size: disc = slice + par2(slice) + overhead
    overhead = 1 * MiB + 256 * 1024  # PAR2 index + README + checksums
    available = disc_bytes - overhead
    slice_bytes = (available * 100 // (100 + args.redundancy))
    slice_bytes = (slice_bytes // MiB) * MiB  # Round down to MiB

    par2_est = slice_bytes * args.redundancy // 100

    log.step("Konfiguration")
    log.info(f"Disc-Typ:    BD-{args.disc_size} ({human_bytes(disc_bytes)})")
    log.info(f"Slice:       {human_bytes(slice_bytes)}")
    log.info(f"PAR2:        {args.redundancy}% (~{human_bytes(par2_est)})")
    comp_str = args.compression + (f" (Level {args.level})" if args.level else "")
    log.info(f"Kompression: {comp_str}")
    log.info(f"Quelle:      {source}")

    # Work directory
    if args.workdir:
        work_dir = Path(args.workdir)
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="bd-archive-"))
    log.info(f"Workdir:     {work_dir}")

    dio = DiscIO(args.device)
    dar = DarArchive(args.name, work_dir)

    # ── Create archive ──────────────────────────────────────────────────
    log.step("Erstelle dar-Archiv")
    dar.create(source, slice_bytes, args.compression, args.level)

    slices = dar.slices
    slice_count = len(slices)
    log.ok(f"{slice_count} Slice(s) erstellt")

    total_archive = 0
    for s in slices:
        sz = s.stat().st_size
        total_archive += sz
        log.info(f"  {s.name}: {human_bytes(sz)}")
    log.info(f"Gesamt: {human_bytes(total_archive)}")

    # Isolate catalog
    log.info("Isoliere Katalog...")
    dar.isolate_catalog()
    log.ok("Katalog isoliert")

    # ── Process each slice ──────────────────────────────────────────────
    for i, slice_file in enumerate(slices, 1):
        slice_name = slice_file.name

        log.step(f"Disc {i}/{slice_count}: {slice_name}")

        stage = work_dir / "staging" / f"disc_{i}"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)

        # Stage: slice
        shutil.copy2(slice_file, stage)

        # Stage: catalog
        for cat in dar.catalog_files:
            shutil.copy2(cat, stage)

        # Stage: PAR2
        log.info(f"Generiere PAR2 ({args.redundancy}%)...")
        Par2.create(stage / slice_name, args.redundancy)
        log.ok("PAR2 erstellt")

        # Stage: README
        generate_readme(stage, args.name, i, slice_count, slice_name,
                        args.disc_size, args.redundancy,
                        args.compression, args.level)

        # Stage: CHECKSUMS.sha256 (last, covers everything else)
        file_count = Checksums.generate(stage)
        log.ok(f"CHECKSUMS.sha256 ({file_count} Dateien)")

        # Size check
        stage_size = sum(f.stat().st_size for f in stage.iterdir() if f.is_file())
        pct = stage_size * 100 // disc_bytes
        log.info(f"Belegung: {human_bytes(stage_size)} / "
                 f"{human_bytes(disc_bytes)} ({pct}%)")

        if stage_size > disc_bytes:
            log.error("Überschreitet Disc-Kapazität!")
            sys.exit(1)

        # Dry run?
        if args.dry_run:
            log.warn(f"[DRY-RUN] Disc {i} nicht gebrannt")
            continue

        # Burn
        prompt_disc(f"Leere BD-{args.disc_size} einlegen — "
                    f"Disc {i}/{slice_count}", args.device)
        log.info("Brenne...")
        dio.burn(stage, f"{args.name}_{i}", args.speed)
        log.ok(f"Disc {i} gebrannt")

        # Post-burn verify
        if not args.no_verify:
            log.info("Post-Burn-Verifikation...")
            import time
            time.sleep(5)
            mount_dir = Path(tempfile.mkdtemp(prefix="bd-verify-"))
            if dio.mount(mount_dir):
                try:
                    result = verify_disc(mount_dir,
                                         f"Disc {i} (Post-Burn)", quiet=True)
                    if result == VerifyResult.BROKEN:
                        log.error("Post-Burn-Verifikation fehlgeschlagen!")
                        if not prompt_yn("Fortfahren?", default_yes=False):
                            sys.exit(1)
                finally:
                    dio.umount(mount_dir)
                    mount_dir.rmdir()
            else:
                log.warn("Konnte nicht mounten — prüfe manuell")
                mount_dir.rmdir()

        dio.eject()
        log.ok(f"Disc {i}/{slice_count} fertig")

    # ── Summary ─────────────────────────────────────────────────────────
    source_size = sum(f.stat().st_size for f in source.rglob("*") if f.is_file())
    ratio = total_archive * 100 // max(source_size, 1)

    log.step("Zusammenfassung")
    print(f"\n  Quelle:       {human_bytes(source_size)}")
    print(f"  Archiv:       {human_bytes(total_archive)} ({ratio}%)")
    print(f"  Discs:        {slice_count} × BD-{args.disc_size}")
    print(f"  PAR2:         {args.redundancy}% pro Disc")
    print(f"  Kompression:  {comp_str}")
    if args.dry_run:
        print(f"\n  [DRY-RUN] Keine Discs gebrannt.")
    print(f"\n  Aufräumen: rm -rf {work_dir}\n")


# ════════════════════════════════════════════════════════════════════════════
# Argument parsing
# ════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bd-archive",
        description="Archive data to Blu-ray discs with dar + par2",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = p.add_subparsers(dest="command", required=True,
                           help="Verfügbare Befehle")

    # ── create ──────────────────────────────────────────────────────────
    cr = sub.add_parser("create", help="Archivieren und auf Discs brennen")
    cr.add_argument("-s", "--source", required=True,
                    help="Quellverzeichnis")
    cr.add_argument("-n", "--name", required=True,
                    help="Archivname")
    cr.add_argument("-r", "--redundancy", type=int, default=5,
                    help="PAR2-Redundanz in %% (Standard: 5)")
    cr.add_argument("-d", "--disc-size", type=int, default=25,
                    choices=[25, 50, 100],
                    help="Disc-Größe in GB (Standard: 25)")
    cr.add_argument("-D", "--device", default="/dev/sr0",
                    help="Laufwerk (Standard: /dev/sr0)")
    cr.add_argument("-c", "--compression", default="zstd",
                    choices=["zstd", "lzma", "lz4", "gzip", "bzip2", "none"],
                    help="Kompressionsalgorithmus (Standard: zstd)")
    cr.add_argument("-l", "--level",
                    help="Kompressionsstufe")
    cr.add_argument("-w", "--workdir",
                    help="Arbeitsverzeichnis (Standard: auto in /tmp)")
    cr.add_argument("-S", "--speed",
                    help="Brenngeschwindigkeit")
    cr.add_argument("--no-verify", action="store_true",
                    help="Post-Burn-Verifikation überspringen")
    cr.add_argument("--dry-run", action="store_true",
                    help="Alles vorbereiten, aber nicht brennen")

    # ── verify ──────────────────────────────────────────────────────────
    vr = sub.add_parser("verify", help="Disc-Integrität prüfen")
    vr.add_argument("target",
                    help="Pfad zum Mountpoint, Verzeichnis oder Block-Gerät")

    # ── extract ─────────────────────────────────────────────────────────
    ex = sub.add_parser("extract", help="Archiv von Discs wiederherstellen")
    ex.add_argument("-o", "--output", required=True,
                    help="Zielverzeichnis")
    ex.add_argument("-D", "--device", default="/dev/sr0",
                    help="Laufwerk (Standard: /dev/sr0)")
    ex.add_argument("-w", "--workdir",
                    help="Arbeitsverzeichnis (Standard: auto in /tmp)")

    return p


# ════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════

def main():
    print(f"\n{Logger._c('bold')}bd-archive{Logger._c('reset')} v{VERSION}\n")

    parser = build_parser()
    args = parser.parse_args()

    match args.command:
        case "create":
            cmd_create(args)
        case "verify":
            cmd_verify(args)
        case "extract":
            cmd_extract(args)


if __name__ == "__main__":
    main()
