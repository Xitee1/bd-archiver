"""Detect repeated regular-file identities within the selected source paths."""

from collections.abc import Collection


class HardlinkTracker:
    def __init__(self) -> None:
        self.paths: dict[tuple[int, int], list[str]] = {}

    def add(self, path: str, device: int, inode: int) -> None:
        self.paths.setdefault((device, inode), []).append(path)

    def check(self, excluded_paths: Collection[str] = ()) -> None:
        """External links do not matter; only repeated selected paths do."""
        excluded = set(excluded_paths)
        for paths in self.paths.values():
            selected = sorted(path for path in paths if path not in excluded)
            if len(selected) > 1:
                raise ValueError(
                    "Hardlinks within the selected source are not supported: "
                    + ", ".join(repr(path) for path in selected)
                    + ". Select only one name, or explicitly create independent copies "
                    "if you want to archive the content multiple times."
                )
