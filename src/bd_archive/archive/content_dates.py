"""Resolve content dates conservatively; retain explicit modification-time fallback."""

import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bd_archive.archive.raw import RawEntry
from bd_archive.tools.exiftool import read_dates


@dataclass(frozen=True)
class ContentDate:
    timestamp: int
    source: str
    media: bool = False


def parse_date(value: object) -> int | None:
    """Accept complete calendar dates, with optional time and numeric timezone.

    Date-only values and timestamps without an offset use UTC consistently;
    incomplete years/months and invalid dates are not guessed.
    """
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    match = re.fullmatch(
        r"(\d{4})[:-](\d{2})[:-](\d{2})"
        r"(?:[ T](\d{2}):(\d{2}):(\d{2})(\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?)?",
        text,
    )
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    if offset and offset != "Z":
        numeric_offset = offset[1:].replace(":", "")
        if int(numeric_offset[:2]) >= 24 or int(numeric_offset[2:]) >= 60:
            return None
    try:
        parsed = datetime.fromisoformat(
            f"{year}-{month}-{day}T{hour or '00'}:{minute or '00'}:{second or '00'}"
            f"{offset or '+00:00'}"
        ).astimezone(UTC)
        elapsed = parsed - datetime(1970, 1, 1, tzinfo=UTC)
        nanos = int(((fraction or ".")[1:] + "000000000")[:9])
        return (elapsed.days * 86400 + elapsed.seconds) * 10**9 + nanos
    except (ValueError, OverflowError):
        return None


def priority(group: str, family: str, name: str, tag_id: object) -> int | None:
    """Whitelist content fields; a familiar display name alone is insufficient."""
    if not isinstance(tag_id, (str, int)):
        return None
    if group == "EXIF" and name == "DateTimeOriginal" and tag_id == 36867:
        return 0
    if group == "XMP" and (family, name) in {
        ("XMP-exif", "DateTimeOriginal"),
        ("XMP-photoshop", "DateCreated"),
    }:
        return 1
    if group == "IPTC" and name == "DateCreated" and tag_id == 55:
        return 2
    if group == "Matroska":
        # Numeric ID 1121 is DateUTC (muxing), also named DateTimeOriginal
        # by ExifTool. Only the textual content tags are suitable here.
        return {"DATE_RECORDED": 0, "DATE_RELEASED": 10, "DATE": 11}.get(tag_id)
    if group == "QuickTime":
        if (name == "DateTimeOriginal" and tag_id in ("IDIT", "date")) or (
            family == "Keys" and name == "CreationDate" and tag_id == "creationdate"
        ):
            return 0
        if name == "ReleaseDate" and tag_id == "rldt":
            return 10
        if name == "ContentCreateDate" and tag_id == "\xa9day":
            return 11
    if group == "ID3":
        return {"TDRC": 0, "TDOR": 10, "XDOR": 10, "TDRL": 11}.get(tag_id)
    if group == "Vorbis" and name == "Date" and tag_id == "DATE":
        return 11
    return None


def resolve_date(record: dict, mtime_ns: int) -> ContentDate:
    media = any(
        key.endswith(":MIMEType")
        and isinstance(value, dict)
        and str(value.get("val", "")).startswith(("video/", "audio/", "image/"))
        for key, value in record.items()
    )
    fallback = ContentDate(mtime_ns, "mtime", media)
    if any(key.endswith(":Error") for key in record):
        return fallback
    candidates = []
    exif_original = False
    for key, value in record.items():
        parts = key.split(":")
        if len(parts) != 4 or not isinstance(value, dict):
            continue
        group, family, _, name = parts
        rank = priority(group, family, name, value.get("id"))
        date = parse_date(value.get("val"))
        if rank is not None and date is not None:
            candidates.append((rank, date, key))
            exif_original |= group == "EXIF" and name == "DateTimeOriginal"
    if exif_original:
        # This composite combines EXIF Original with its subsecond/offset tags.
        # Require a valid EXIF original, rather than trusting arbitrary composites.
        for key, value in record.items():
            if key.startswith("Composite:") and key.endswith(":SubSecDateTimeOriginal"):
                date = parse_date(value.get("val")) if isinstance(value, dict) else None
                if date is not None:
                    candidates.append((-1, date, key))
    if not candidates:
        return fallback
    # Equal-priority duplicates use the earliest date, independent of tag order.
    _, date, key = min(candidates)
    return ContentDate(date, key, media)


def scan_dates(source: Path, inventory: list[RawEntry]) -> dict[str, ContentDate]:
    files = [entry for entry in inventory if stat.S_ISREG(entry.mode)]
    records = read_dates([source / entry.path for entry in files])
    return {
        entry.path: resolve_date(records[source / entry.path], entry.mtime_ns) for entry in files
    }


def weighted_median(values: list[tuple[int, int]]) -> int:
    """Use bytes as weights, equal weights only if every file is empty."""
    total = sum(weight for _, weight in values)
    if not total:
        values = [(date, 1) for date, _ in values]
        total = len(values)
    cumulative = 0
    for date, weight in sorted(values):
        cumulative += weight
        if cumulative * 2 >= total:
            return date
    raise ValueError("Cannot determine a date for an empty unit")
