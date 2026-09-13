from dataclasses import dataclass
from pathlib import Path

from bd_archive.tools.software import software_info


@dataclass
class ArchiveConfig:
    name: str
    disc_bytes: int
    redundancy: int
    compression: str
    comp_level: str | None
    generation: int = 1
    software: str = ""

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
    recovery = (
        "RECOVERY:\n"
        "  Format:     PAR2\n"
        "  Coverage:   DAR slice\n"
        f"  Redundancy: {cfg.redundancy}%\n"
        f"  Index:      {slice_name}.par2\n"
        f"  Volumes:    {slice_name}.vol*.par2\n"
        if cfg.redundancy
        else "RECOVERY:     None\n"
    )
    software = cfg.software or software_info(
        ["dar", "mkisofs", *(["par2"] if cfg.redundancy else [])]
    )
    checksum_files = f"  File:      {slice_name}.sha512\n"
    if disc_num == 1:
        checksum_files += f"  File:      {cfg.dar_name}-catalog.*.dar.sha512\n"
    readme_path.write_text(
        f"ARCHIVE NAME: {cfg.name}\n"
        "FORMAT:       DAR\n"
        f"GENERATION:   {cfg.generation}"
        f" ({'full' if cfg.generation == 1 else 'incremental'})\n"
        f"DISC:         {disc_num}/{total_discs}\n"
        f"COMPRESSION:  {cfg.comp_str}\n\n"
        "CHECKSUM:\n"
        "  Algorithm: SHA-512\n"
        f"{checksum_files}\n"
        f"{recovery}\n"
        f"{software}",
        encoding="utf-8",
    )


def raw_readme(name: str, redundancy: str, recovery_enabled: bool, software: str) -> str:
    """Describe a raw disc; file paths are relative to this README."""
    recovery = (
        "RECOVERY:\n"
        "  Format:     PAR2\n"
        "  Coverage:   Non-empty file contents\n"
        f"  Redundancy: {redundancy}\n"
        "  Index:      recovery.par2\n"
        "  Volumes:    recovery.vol*.par2\n"
        if recovery_enabled
        else "RECOVERY:     None\n"
    )
    return (
        f"ARCHIVE NAME: {name}\n"
        "FORMAT:       UDF / ISO 9660 (Rock Ridge)\n\n"
        "CHECKSUM:\n"
        "  Algorithm: SHA-512\n"
        "  File:      checksums.sha512\n\n"
        f"{recovery}\n"
        f"{software}"
    )
