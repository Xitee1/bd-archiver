from pathlib import Path

from bd_archive.constants import ISO9660_VOLUME_LABEL_MAX
from bd_archive.shell.runner import run


def _command(
    graft_entries: list[tuple[str, Path]],
    volume_label: str,
    publisher: str,
    *,
    rock_ridge: bool = False,
) -> tuple[list[str], list[str]]:
    if len(volume_label.encode("utf-8")) > ISO9660_VOLUME_LABEL_MAX:
        raise ValueError(
            f"Volume label '{volume_label}' exceeds {ISO9660_VOLUME_LABEL_MAX}-byte ISO9660 limit"
        )

    def escape(value: str | Path) -> str:
        return str(value).replace("\\", "\\\\").replace("=", "\\=")

    return [
        "mkisofs",
        "-iso-level",
        "3",
        "-udf",
        *(["-R"] if rock_ridge else []),
        "-V",
        volume_label,
        "-publisher",
        publisher,
        "-input-charset",
        "utf-8",
        "-graft-points",
    ], [f"/{escape(rel)}={escape(src)}" for rel, src in graft_entries]


def estimate_size(
    graft_entries: list[tuple[str, Path]],
    volume_label: str,
    publisher: str,
    *,
    rock_ridge: bool = False,
) -> int:
    """Ask mkisofs for the exact filesystem size (2048-byte sectors)."""
    cmd, graft_args = _command(graft_entries, volume_label, publisher, rock_ridge=rock_ridge)
    result = run([*cmd, "-print-size", *graft_args], capture=True)
    return int(result.stdout.strip().splitlines()[-1]) * 2048


def build(
    iso_path: Path,
    graft_entries: list[tuple[str, Path]],
    volume_label: str,
    publisher: str,
    *,
    rock_ridge: bool = False,
):
    """Build an ISO9660+UDF image at iso_path.

    graft_entries maps each source file to its path inside the image:
    ``(iso_relpath, source_path)`` — e.g. ``("photos-gen1/x.dar", p)``
    places p at /photos-gen1/x.dar. Directories are created implicitly
    by -graft-points, so no staging copies are needed. UDF preserves
    the full filename case+length; ISO9660 level 3 lets the bridge
    filesystem hold GiB-sized dar slices via multi-extent allocation.
    mkisofs always writes both filesystems on the same data blocks —
    the kernel mounts whichever is preferred (UDF on modern Linux).
    """
    cmd, graft_args = _command(graft_entries, volume_label, publisher, rock_ridge=rock_ridge)
    run([*cmd, "-o", str(iso_path), *graft_args], label="mkisofs")
