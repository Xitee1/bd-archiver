"""Write the final archive fact sheet shared by RAW and DAR creation."""

from pathlib import Path


def write_readme(
    path: Path,
    *,
    name: str,
    description: str,
    archive_format: str,
    details: dict[str, str],
    checksum_files: list[str],
    recovery: dict[str, str] | None,
    software: str,
) -> None:
    """Write once per disc after archive and recovery details are final.

    All file paths are relative to the README. Software information is supplied
    by the caller once per creation run; rendering never queries external tools.
    """
    fields = {"ARCHIVE NAME": name}
    if description:
        fields["DESCRIPTION"] = description
    fields["FORMAT"] = archive_format
    fields.update(details)
    header = "".join(
        f"{key + ':':14}{value.replace(chr(10), chr(10) + ' ' * 14)}\n"
        for key, value in fields.items()
    )
    checksums = "CHECKSUM:\n  Algorithm: SHA-512\n" + "".join(
        f"  File:      {file}\n" for file in checksum_files
    )
    recovery_text = (
        "RECOVERY:\n" + "".join(f"  {key + ':':12}{value}\n" for key, value in recovery.items())
        if recovery
        else "RECOVERY:     None\n"
    )
    path.write_text(f"{header}\n{checksums}\n{recovery_text}\n{software}", encoding="utf-8")
