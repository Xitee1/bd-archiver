import contextlib
import io
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_archive.archive.content_dates import (
    ContentDate,
    parse_date,
    resolve_date,
    scan_dates,
    weighted_median,
)
from bd_archive.archive.prepare import make_units
from bd_archive.archive.raw import scan_raw_source
from bd_archive.cli import build_parser
from bd_archive.commands.prepare import cmd_prepare
from bd_archive.tools.exiftool import read_dates


def tag(group, family, name, tag_id, value, instance=""):
    return {f"{group}:{family}:{instance}:{name}": {"id": tag_id, "val": value}}


def matroska(tags):
    """A tiny EBML fixture with a recent mux date and independent content tags."""

    def element(identifier, value):
        width = next(n for n in range(1, 9) if len(value) < (1 << (7 * n)) - 1)
        return bytes.fromhex(identifier) + ((1 << (7 * width)) | len(value)).to_bytes(width) + value

    header = element("1a45dfa3", element("4282", b"matroska"))
    info = element("1549a966", element("4461", (800_000_000 * 10**9).to_bytes(8)))
    simple = b"".join(
        element("67c8", element("45a3", name.encode()) + element("4487", value.encode()))
        for name, value in tags
    )
    return header + element("18538067", info + element("1254c367", element("7373", simple)))


class DateTests(unittest.TestCase):
    def test_calendar_formats_offsets_and_subseconds(self):
        expected = 1493856000 * 10**9
        for value in (20170504, "20170504", "2017-05-04", "2017:05:04 00:00:00"):
            self.assertEqual(parse_date(value), expected)
        self.assertEqual(parse_date("2017-05-04T02:00:00+0200"), expected)
        self.assertEqual(parse_date("2017:05:04 02:00:00.123456789+02:00"), expected + 123456789)
        self.assertEqual(parse_date("2017-05-04T00:00:00Z"), expected)
        self.assertEqual(parse_date("1969-12-31T23:59:59.5Z"), -500_000_000)

    def test_incomplete_invalid_and_ambiguous_dates_are_not_guessed(self):
        for value in (
            None,
            [],
            True,
            2017,
            "2017:05",
            "20170230",
            "0000:00:00",
            "04/05/2017",
            "2017-05-04T25:00:00Z",
            "2017-05-04 garbage",
            "2017-05-04T00:00:00+25:00",
            "2017-05-04T00:00:00+00:99",
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_date(value))

    def test_recording_precedes_release_but_muxing_is_never_a_content_date(self):
        record = tag("Matroska", "Matroska", "DateTimeOriginal", 1121, "2026:09:18 12:00:00")
        self.assertEqual(resolve_date(record, 123).source, "mtime")
        record.update(tag("Matroska", "Matroska", "Date", "DATE", 20170504))
        self.assertEqual(resolve_date(record, 123).timestamp, parse_date(20170504))
        record.update(tag("Matroska", "Matroska", "DateReleased", "DATE_RELEASED", "2018-03-02"))
        record.update(tag("Matroska", "Track1", "DateTimeOriginal", "DATE_RECORDED", "2016-02-01"))
        self.assertEqual(resolve_date(record, 123).timestamp, parse_date("2016-02-01"))

    def test_supported_formats_and_encoding_dates(self):
        for group, family, name, identifier in (
            ("EXIF", "ExifIFD", "DateTimeOriginal", 36867),
            ("XMP", "XMP-exif", "DateTimeOriginal", "DateTimeOriginal"),
            ("XMP", "XMP-photoshop", "DateCreated", "DateCreated"),
            ("IPTC", "IPTC", "DateCreated", 55),
            ("QuickTime", "Keys", "CreationDate", "creationdate"),
            ("QuickTime", "UserData", "DateTimeOriginal", "IDIT"),
            ("QuickTime", "ItemList", "ContentCreateDate", "\xa9day"),
            ("ID3", "ID3v2_4", "RecordingTime", "TDRC"),
            ("ID3", "ID3v2_4", "ReleaseTime", "TDRL"),
            ("Vorbis", "Vorbis", "Date", "DATE"),
        ):
            with self.subTest(group=group, name=name):
                record = tag(group, family, name, identifier, "2017-05-04")
                self.assertEqual(resolve_date(record, 123).timestamp, parse_date(20170504))
        for group, name, identifier in (
            ("QuickTime", "CreateDate", 1),
            ("QuickTime", "TrackCreateDate", 1),
            ("Matroska", "DateEncoded", "DATE_ENCODED"),
            ("ID3", "EncodingTime", "TDEN"),
            ("XMP", "CreateDate", "CreateDate"),
        ):
            record = tag(group, group, name, identifier, "2017-05-04")
            self.assertEqual(resolve_date(record, 123), ContentDate(123, "mtime"))

    def test_exif_composite_keeps_timezone_only_with_valid_exif_original(self):
        record = tag(
            "Composite",
            "Composite",
            "SubSecDateTimeOriginal",
            "SubSecDateTimeOriginal",
            "2017:05:04 02:00:00.1+02:00",
        )
        self.assertEqual(resolve_date(record, 123).source, "mtime")
        record.update(tag("EXIF", "ExifIFD", "DateTimeOriginal", 36867, "2017:05:04 02:00:00"))
        self.assertEqual(resolve_date(record, 123).timestamp, parse_date(20170504) + 100_000_000)

    def test_bad_dates_fall_through_and_duplicates_are_deterministic(self):
        record = tag("Matroska", "Matroska", "DateTimeOriginal", "DATE_RECORDED", "invalid")
        record.update(tag("Matroska", "Matroska", "DateReleased", "DATE_RELEASED", "2018-01-01"))
        record.update(
            tag("Matroska", "Matroska", "DateReleased", "DATE_RELEASED", "2017-01-01", "Copy1")
        )
        self.assertEqual(resolve_date(record, 123).timestamp, parse_date("2017-01-01"))
        self.assertEqual(
            resolve_date(record, 123), resolve_date(dict(reversed(list(record.items()))), 123)
        )
        record.update(tag("ExifTool", "ExifTool", "Error", "Error", "Invalid file"))
        self.assertEqual(resolve_date(record, 123).source, "mtime")

    def test_weighted_median_handles_small_sidecars_ties_and_empty_files(self):
        self.assertEqual(weighted_median([(2017, 1000), (2026, 1)]), 2017)
        self.assertEqual(weighted_median([(2017, 5), (2020, 5), (2026, 1)]), 2020)
        self.assertEqual(weighted_median([(2017, 5), (2020, 5)]), 2017)
        self.assertEqual(weighted_median([(2017, 0), (2020, 10)]), 2020)
        self.assertEqual(weighted_median([(2017, 0), (2020, 0), (2021, 0)]), 2020)

    def test_units_use_resolved_dates_and_media_ranges_not_sidecars(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            (source / "group").mkdir()
            for name, size in (("old.mkv", 100), ("new.mkv", 10), ("info.json", 1)):
                (source / "group" / name).write_bytes(b"x" * size)
            inventory = scan_raw_source(source)
            dates = {
                "group/old.mkv": ContentDate(2017, "metadata", True),
                "group/new.mkv": ContentDate(2024, "metadata", True),
                "group/info.json": ContentDate(2026, "mtime"),
            }
            (unit,) = make_units(inventory, 1, dates)
            self.assertEqual((unit.date, unit.date_start, unit.date_end), (2017, 2017, 2024))
            self.assertEqual(
                [u.date for u in make_units(inventory, None, dates)], [2017, 2024, 2026]
            )


class ReaderTests(unittest.TestCase):
    def test_batching_preserves_filenames_and_disables_configuration(self):
        paths = [Path(f"/tmp/-name {i} $(unused).mkv") for i in range(260)]

        def response(command, **kwargs):
            self.assertEqual(command[:3], ["exiftool", "-config", ""])
            selected = command[command.index("--") + 1 :]
            self.assertLessEqual(len(selected), 128)
            return subprocess.CompletedProcess(
                command, 0, json.dumps([{"SourceFile": p} for p in selected]), ""
            )

        with patch("bd_archive.tools.exiftool.run", side_effect=response) as run:
            self.assertEqual(set(read_dates(paths)), set(paths))
        self.assertEqual(run.call_count, 3)

    def test_tool_failures_and_missing_records_do_not_silently_fall_back(self):
        for stdout, status in (("not json", 1), ("[]", 0), ('[{"SourceFile":"/tmp/file"}]', 2)):
            with (
                patch(
                    "bd_archive.tools.exiftool.run",
                    return_value=subprocess.CompletedProcess([], status, stdout, "failed"),
                ),
                self.assertRaises(ValueError),
            ):
                read_dates([Path("/tmp/file")])

    def test_per_file_error_is_accepted_as_explicit_mtime_fallback(self):
        record = {
            "SourceFile": "/tmp/file",
            **tag("ExifTool", "ExifTool", "Error", "Error", "Unknown file"),
        }
        with patch(
            "bd_archive.tools.exiftool.run",
            return_value=subprocess.CompletedProcess([], 1, json.dumps([record]), ""),
        ):
            result = read_dates([Path("/tmp/file")])
        self.assertEqual(resolve_date(result[Path("/tmp/file")], 123).source, "mtime")


@unittest.skipUnless(shutil.which("exiftool"), "requires exiftool")
class MetadataIntegrationTests(unittest.TestCase):
    def test_actual_photo_exif_date_with_offset(self):
        # Minimal JPEG with an ExifIFD containing Original and OffsetTimeOriginal.
        tiff = b"II" + struct.pack("<HI", 42, 8)
        tiff += struct.pack("<HHHII", 1, 0x8769, 4, 1, 26) + struct.pack("<I", 0)
        tiff += struct.pack("<H", 2)
        tiff += struct.pack("<HHII", 0x9003, 2, 20, 56)
        tiff += struct.pack("<HHII", 0x9011, 2, 7, 76)
        tiff += struct.pack("<I", 0) + b"2017:05:04 02:00:00\0+02:00\0"
        payload = b"Exif\0\0" + tiff
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "photo.jpg"
            path.write_bytes(
                b"\xff\xd8\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + b"\xff\xd9"
            )
            date = resolve_date(read_dates([path])[path], 123)
            self.assertEqual(date.timestamp, parse_date(20170504))
            self.assertTrue(date.media)

    def test_actual_matroska_tag_ids_override_mux_date_without_mutating_files(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            (source / "generic.mkv").write_bytes(matroska([("DATE", "20170504")]))
            (source / "recorded.mkv").write_bytes(
                matroska([("DATE_RECORDED", "2016-01-02"), ("DATE_RELEASED", "2018-01-02")])
            )
            (source / "mux-only.mkv").write_bytes(matroska([]))
            (source / "unknown.bin").write_bytes(b"unsupported")
            for path in source.iterdir():
                os.utime(path, ns=(123, 123))
            before = scan_raw_source(source)
            dates = scan_dates(source, before)
            self.assertEqual(dates["generic.mkv"].timestamp, parse_date(20170504))
            self.assertEqual(dates["recorded.mkv"].timestamp, parse_date("2016-01-02"))
            self.assertEqual(dates["mux-only.mkv"].timestamp, 123)
            self.assertEqual(dates["unknown.bin"].source, "mtime")
            self.assertEqual(scan_raw_source(source), before)
            self.assertTrue(dates["generic.mkv"].media)

    @unittest.skipUnless(shutil.which("mkisofs"), "requires mkisofs")
    def test_preview_reports_metadata_fallback_and_dates_before_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.mkdir()
            (source / "video").mkdir()
            (source / "video/movie.mkv").write_bytes(matroska([("DATE", "20170504")]))
            (source / "video/info.json").write_text("{}")
            before = scan_raw_source(source)
            output = Path(temp) / "output"
            args = build_parser().parse_args(
                ["prepare", "-s", str(source), "-o", str(output), "-b", "20000000", "-r", "none"]
            )
            captured = io.StringIO()
            with patch("builtins.input", return_value="n"), contextlib.redirect_stdout(captured):
                cmd_prepare(args)
            text = captured.getvalue()
            self.assertIn("1 files from content metadata, 1 using modification time", text)
            self.assertIn("center 2017-05-04, range 2017-05-04 to 2017-05-04", text)
            self.assertIn("no unit-center overlap", text)
            self.assertFalse(output.exists())
            self.assertEqual(scan_raw_source(source), before)
