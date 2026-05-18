import re
from pathlib import Path

from bd_archive.tools import dar

# Matches both Phase-2+ generational filenames and legacy ones:
#   photos-gen3.0001.dar          → ('photos', 3, False)
#   photos-gen3-catalog.0001.dar  → ('photos', 3, True)
#   photos.0001.dar               → ('photos', 1, False)  [legacy]
#   photos-catalog.0001.dar       → ('photos', 1, True)   [legacy]
# The non-greedy archive-name group keeps `-gen<N>` and `-catalog`
# detection deterministic when the archive name itself contains
# hyphens.
_DAR_FILENAME_RE = re.compile(
    r"^(?P<name>.+?)(?:-gen(?P<gen>\d+))?(?P<catalog>-catalog)?\.\d+\.dar$"
)


def parse_dar_filename(filename: str) -> tuple[str, int, bool] | None:
    """Parse a dar slice or catalog filename.

    Returns ``(archive_name, generation, is_catalog)`` or ``None`` if the
    name does not look like a dar slice/catalog file. Generation
    defaults to 1 for legacy (pre-Phase-2) filenames that lack the
    ``-gen<N>`` segment.
    """
    m = _DAR_FILENAME_RE.match(filename)
    if not m:
        return None
    name = m.group("name")
    gen = int(m.group("gen")) if m.group("gen") else 1
    is_catalog = m.group("catalog") is not None
    return name, gen, is_catalog


class DarArchive:
    def __init__(self, name: str, work_dir: Path):
        self.name = name
        self.tmp_dir = work_dir / "tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.base_path = self.tmp_dir / name

    @property
    def slices(self) -> list[Path]:
        return sorted(
            p for p in self.tmp_dir.glob(f"{self.name}.[0-9]*.dar") if "-catalog" not in p.name
        )

    @property
    def catalog_files(self) -> list[Path]:
        return sorted(self.tmp_dir.glob(f"{self.name}-catalog.*.dar"))

    def create(
        self,
        source: Path,
        slice_bytes: int,
        compression: str,
        comp_level: str | None,
        par2_hook: str | None = None,
        ref_catalog: Path | None = None,
        excludes: list[str] | None = None,
    ):
        dar.create_sliced(
            self.base_path,
            source,
            slice_bytes,
            compression,
            comp_level,
            execute_hook=par2_hook,
            ref_catalog=ref_catalog,
            excludes=excludes,
        )

    def isolate_catalog(self):
        dar.isolate_catalog(self.base_path)
