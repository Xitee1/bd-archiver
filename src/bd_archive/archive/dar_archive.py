import re
from dataclasses import dataclass
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

# A dar slice or catalog filename ends in ".NNNN.dar"; stripping that
# off yields the dar archive basename (e.g. "photos-gen1" or, on legacy
# pre-Phase-2 archives, just "photos"). That basename is what dar -x
# wants as input, what groups files by generation in extract staging,
# and what names an archive's top-level folder on disc.
_SLICE_SUFFIX_RE = re.compile(r"\.\d+\.dar$")


def dar_basename(filename: str) -> str:
    return _SLICE_SUFFIX_RE.sub("", filename)


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


@dataclass(frozen=True)
class DiscArchive:
    """One archive found on a (mounted) disc or disc image.

    ``directory`` is where its files live: a top-level folder on
    foldered-layout discs, or the disc root on legacy flat discs.
    ``rel_dir`` is ``directory`` relative to the scanned root ("" for
    the root itself) — stable across re-mounts at different paths.
    """

    chain_name: str
    generation: int
    basename: str
    directory: Path
    rel_dir: str


def find_disc_archives(root: Path) -> list[DiscArchive]:
    """Discover every archive on a mounted disc / disc image.

    Foldered layout (v1.1+): one top-level folder per archive, named
    after its dar basename. Legacy flat layout: slice files at the
    root. Both are detected from the slice *filenames* (authoritative —
    folder names are only a location hint). A directory yields one
    entry per distinct basename found, so a hand-built disc with two
    archives' files mixed in one folder still resolves.
    """
    found: list[DiscArchive] = []
    seen: set[str] = set()
    search_dirs = [d for d in sorted(root.iterdir()) if d.is_dir()] + [root]
    for d in search_dirs:
        for f in sorted(d.glob("*.dar")):
            if "-catalog" in f.name:
                continue
            parsed = parse_dar_filename(f.name)
            if parsed is None:
                continue
            basename = dar_basename(f.name)
            if basename in seen:
                continue
            seen.add(basename)
            name, gen, _ = parsed
            rel = "" if d == root else d.name
            found.append(DiscArchive(name, gen, basename, d, rel))
    return found


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
        first_slice_bytes: int | None = None,
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
            first_slice_bytes=first_slice_bytes,
        )

    def isolate_catalog(self):
        dar.isolate_catalog(self.base_path)
