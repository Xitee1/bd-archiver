"""Portable disc folders and their off-disc burn manifest."""

import errno
import json
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from bd_archive.tools import mkisofs
from bd_archive.ui.progress import copy_with_progress

MANIFEST = "manifest.json"


def check_output_available(output: Path) -> None:
    """Never mix a new run with existing or incomplete disc sets."""
    images = output / "images"
    discs = output / "discs"
    if discs.exists() and not discs.is_dir():
        raise ValueError(f"Disc output path is not a directory: {discs}")
    if list(images.glob("disc_*.iso")) or (discs.exists() and any(discs.iterdir())):
        raise ValueError(f"{output} already contains disc output; choose another output directory")


def tree_signature(root: Path) -> dict[str, list[int]]:
    """Fast metadata guard, not a replacement for checksum verification.

    Relative paths and preserved mtimes allow moving/copying the complete
    output tree. Directory sizes/mtimes vary when copying and are excluded.
    Links and special files are never valid prepared disc contents.
    """
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Not a regular disc directory: {root}")
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError(f"Unsupported entry in disc folder: {path}")
        result[path.relative_to(root).as_posix()] = (
            [info.st_mode, info.st_size, info.st_mtime_ns]
            if stat.S_ISREG(info.st_mode)
            else [info.st_mode]
        )
    if not result:
        raise ValueError(f"Disc folder is empty: {root}")
    return result


@dataclass
class DiscFolder:
    root: Path
    volume_label: str
    publisher: str
    rock_ridge: bool
    image_bytes: int
    signature: dict[str, list[int]]

    @property
    def entries(self) -> list[tuple[str, Path]]:
        return [("", self.root)]

    def check_unchanged(self) -> None:
        if tree_signature(self.root) != self.signature:
            raise ValueError(f"Disc folder changed since creation: {self.root}; recreate the set")

    def measure(self) -> int:
        self.check_unchanged()
        size = mkisofs.estimate_size(
            self.entries, self.volume_label, self.publisher, rock_ridge=self.rock_ridge
        )
        self.check_unchanged()
        # A changed mkisofs version may have different filesystem overhead.
        # The current calculation is authoritative for the upcoming burn.
        return size


def prepare_folder(
    destination: Path,
    entries: list[tuple[str, Path]],
    volume_label: str,
    publisher: str,
    capacity: int,
    *,
    rock_ridge: bool = False,
    move_sources: set[Path] | None = None,
) -> DiscFolder:
    """Materialize a self-contained disc, moving our scratch files when possible.

    Source payloads and packed archives are copied, never hard-linked or moved.
    A cross-filesystem workdir requires a copy of generated files as well.
    Failed preparations remain available for inspection, without a ready manifest.
    """
    destination.mkdir(parents=True, exist_ok=False)
    move_sources = move_sources or set()

    def transfer(source: Path, target: Path) -> None:
        info = source.lstat()
        if stat.S_ISDIR(info.st_mode):
            target.mkdir(parents=True, exist_ok=True)
            for child in sorted(source.iterdir()):
                transfer(child, target / child.name)
            # The grafted root is a merge of metadata and payload directories.
            if target != destination:
                shutil.copystat(source, target)
        elif stat.S_ISREG(info.st_mode):
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise ValueError(f"Conflicting disc file: {target}")
            if source in move_sources:
                try:
                    source.rename(target)
                    return
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
            copy_with_progress(source, target)
        else:
            raise ValueError(f"Unsupported disc source: {source}")

    for rel, source in entries:
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise ValueError(f"Invalid disc path: {rel}")
        transfer(source, destination / rel)
    folder = DiscFolder(
        destination, volume_label, publisher, rock_ridge, 0, tree_signature(destination)
    )
    folder.image_bytes = folder.measure()
    if folder.image_bytes > capacity:
        raise ValueError(
            f"Disc requires {folder.image_bytes} bytes, exceeding writable capacity {capacity}"
        )
    return folder


def save_disc_set(discs_dir: Path, folders: list[DiscFolder]) -> None:
    """Publish the complete set only after every disc passed its size check."""
    data = {"version": 1, "discs": []}
    for i, folder in enumerate(folders, 1):
        if folder.root != discs_dir / f"disc_{i:04d}":
            raise ValueError("Disc folders must be numbered consecutively from 1")
        folder.check_unchanged()
        data["discs"].append(
            {
                "directory": folder.root.name,
                "volume_label": folder.volume_label,
                "publisher": folder.publisher,
                "rock_ridge": folder.rock_ridge,
                "image_bytes": folder.image_bytes,
                "signature": folder.signature,
            }
        )
    # Exclusive creation prevents overwriting an existing user manifest.
    with (discs_dir / MANIFEST).open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=True, indent=2)
        stream.write("\n")


def load_disc_set(discs_dir: Path) -> list[DiscFolder]:
    manifest = discs_dir / MANIFEST
    if not manifest.is_file():
        raise ValueError(f"Incomplete disc set: missing {manifest}; finish/recreate the archive")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data["version"] != 1 or not isinstance(data["discs"], list) or not data["discs"]:
            raise ValueError("unsupported version or empty disc list")
        folders = []
        for i, item in enumerate(data["discs"], 1):
            if item["directory"] != f"disc_{i:04d}":
                raise ValueError("invalid disc sequence")
            if (
                not isinstance(item["volume_label"], str)
                or not isinstance(item["publisher"], str)
                or type(item["rock_ridge"]) is not bool
                or type(item["image_bytes"]) is not int
                or item["image_bytes"] <= 0
                or not isinstance(item["signature"], dict)
            ):
                raise ValueError("invalid disc metadata")
            folders.append(
                DiscFolder(
                    discs_dir / item["directory"],
                    item["volume_label"],
                    item["publisher"],
                    item["rock_ridge"],
                    item["image_bytes"],
                    item["signature"],
                )
            )
        actual = set(discs_dir.glob("disc_*"))
        if actual != {folder.root for folder in folders}:
            raise ValueError("missing or unexpected disc folders")
        return folders
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid disc set manifest {manifest}: {exc}") from exc
