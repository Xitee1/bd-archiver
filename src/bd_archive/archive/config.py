from dataclasses import dataclass


@dataclass
class ArchiveConfig:
    name: str
    disc_bytes: int
    redundancy: int
    compression: str
    comp_level: str | None
    generation: int = 1
    description: str = ""

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
