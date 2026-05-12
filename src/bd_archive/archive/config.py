from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bd_archive.shell.format import human_bytes


@dataclass
class ArchiveConfig:
    name: str
    disc_bytes: int
    redundancy: int
    compression: str
    comp_level: str | None
    generation: int = 1

    @property
    def comp_str(self) -> str:
        return self.compression + (f" ({self.comp_level})" if self.comp_level else "")

    @property
    def dar_name(self) -> str:
        """Internal dar archive name including generation suffix.

        File naming uses `<name>-gen<N>` so slices from different
        generations of the same chain coexist in one staging dir during
        extract. The user-facing `name` (from `-n`) is the chain
        identity — see project README for the rule that name must stay
        identical across all generations of one chain.
        """
        return f"{self.name}-gen{self.generation}"


def write_readme(
    readme_path: Path, cfg: ArchiveConfig, disc_num: int, total_discs: int, slice_name: str
):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    readme_path.write_text(
        f"BD-ARCHIVE | {cfg.name} | Gen {cfg.generation} | Disc {disc_num}/{total_discs}"
        f" | {ts} | Capacity {human_bytes(cfg.disc_bytes)}"
        f" | PAR2 {cfg.redundancy}% | {cfg.comp_str}\n\n"
        f"RESTORE:  dar -x {cfg.dar_name} -R /target\n"
        f"VERIFY:   sha512sum -c {slice_name}.sha512\n"
        f"          par2 verify {slice_name}.par2\n"
        f"REPAIR:   par2 repair {slice_name}.par2\n"
        f"DEPENDS:  pacman -S dar par2cmdline  |  apt install dar par2\n"
        f"\nCHAIN:    Name '{cfg.name}' identifies this archive chain.\n"
        f"          Future incremental generations must use the same name.\n"
    )
