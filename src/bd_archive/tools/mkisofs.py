from pathlib import Path

from bd_archive.constants import ISO9660_VOLUME_LABEL_MAX
from bd_archive.shell.runner import run


def build(iso_path: Path, source_files: list[Path],
          volume_label: str, publisher: str):
    """Build an ISO9660+UDF image at iso_path containing every file
    in source_files at the ISO root.

    -graft-points lets us map each input file from its real path to
    /<basename> in the ISO root, so we don't need to copy files into
    a staging directory first. UDF preserves the full filename
    case+length; ISO9660 level 3 lets the bridge filesystem hold
    GiB-sized dar slices via multi-extent allocation. mkisofs
    always writes both filesystems on the same data blocks — the
    kernel mounts whichever is preferred (UDF on modern Linux).
    """
    if len(volume_label.encode("utf-8")) > ISO9660_VOLUME_LABEL_MAX:
        raise ValueError(
            f"Volume label '{volume_label}' exceeds "
            f"{ISO9660_VOLUME_LABEL_MAX}-byte ISO9660 limit"
        )

    graft_args = [f"/{src.name}={src}" for src in source_files]
    cmd = ["mkisofs",
           "-iso-level", "3", "-udf",
           "-V", volume_label,
           "-publisher", publisher,
           "-input-charset", "utf-8",
           "-graft-points",
           "-o", str(iso_path),
           *graft_args]
    run(cmd, label="mkisofs")
