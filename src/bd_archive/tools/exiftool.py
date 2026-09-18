"""Read selected metadata in bounded batches, without user configuration or writes."""

import json
import os
from pathlib import Path

from bd_archive.shell.runner import run

DATE_TAGS = (
    "DateTimeOriginal",
    "SubSecDateTimeOriginal",
    "DateCreated",
    "CreationDate",
    "RecordingTime",
    "DateReleased",
    "ReleaseDate",
    "OriginalReleaseTime",
    "ReleaseTime",
    "ContentCreateDate",
    "Date",
)


def read_dates(paths: list[Path]) -> dict[Path, dict]:
    """Retain format, tag ID and duplicate instances; never extract media streams."""
    result = {}
    batch: list[Path] = []
    argument_bytes = 0

    def read_batch():
        response = run(
            [
                "exiftool",
                "-config",
                "",
                "-json",
                "-G:0:1:4",
                "-D",
                "-a",
                "-s",
                *(f"-{tag}" for tag in DATE_TAGS),
                "-MIMEType",
                "-Error",
                "--",
                *(str(p) for p in batch),
            ],
            capture=True,
            check=False,
        )
        try:
            records = json.loads(response.stdout)
            if not isinstance(records, list):
                raise ValueError("expected a list")
            found = {Path(r["SourceFile"]): r for r in records}
            if set(found) != set(batch) or len(records) != len(batch):
                raise ValueError("incomplete or duplicate file results")
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("ExifTool returned invalid or incomplete metadata results") from exc
        # Unsupported/damaged individual files may produce exit 1 with an Error
        # record. They use mtime; a broken tool invocation must not pass silently.
        has_errors = any(k.endswith(":Error") for r in records for k in r)
        if response.returncode != 0 and not (response.returncode == 1 and has_errors):
            raise ValueError(f"ExifTool failed: {response.stderr.strip()}")
        result.update(found)

    for path in paths:
        if not path.is_absolute():
            raise ValueError("ExifTool requires absolute source paths")
        size = len(os.fsencode(path)) + 1
        if batch and (len(batch) >= 128 or argument_bytes + size > 32768):
            read_batch()
            batch = []
            argument_bytes = 0
        batch.append(path)
        argument_bytes += size
    if batch:
        read_batch()
    return result
