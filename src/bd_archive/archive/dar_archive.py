from pathlib import Path

from bd_archive.tools import dar


class DarArchive:
    def __init__(self, name: str, work_dir: Path):
        self.name = name
        self.tmp_dir = work_dir / "tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.base_path = self.tmp_dir / name

    @property
    def slices(self) -> list[Path]:
        return sorted(
            p for p in self.tmp_dir.glob(f"{self.name}.[0-9]*.dar")
            if "-catalog" not in p.name
        )

    @property
    def catalog_files(self) -> list[Path]:
        return sorted(self.tmp_dir.glob(f"{self.name}-catalog.*.dar"))

    def create(self, source: Path, slice_bytes: int,
               compression: str, comp_level: str | None):
        dar.create_sliced(self.base_path, source, slice_bytes,
                          compression, comp_level)

    def isolate_catalog(self):
        dar.isolate_catalog(self.base_path)
