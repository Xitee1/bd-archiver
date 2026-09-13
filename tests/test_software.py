import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_archive.cli import build_parser
from bd_archive.commands.create import cmd_create
from bd_archive.tools.software import software_info


class SoftwareVersionTests(unittest.TestCase):
    def test_query_failures_are_not_hidden(self):
        for outcome in (
            OSError("unavailable"),
            subprocess.TimeoutExpired(["mkisofs", "--version"], 5),
            subprocess.CompletedProcess([], 1, "mkisofs 3.02a09"),
            subprocess.CompletedProcess([], 0, "unrecognized output"),
        ):
            with (
                self.subTest(outcome=outcome),
                patch(
                    "bd_archive.tools.software.subprocess.run",
                    side_effect=outcome if isinstance(outcome, Exception) else None,
                    return_value=outcome,
                ),
                self.assertRaises(ValueError),
            ):
                software_info(["mkisofs"])

    def test_both_modes_abort_before_creating_output_on_version_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "payload").write_text("data")
            for mode in ("raw", "dar"):
                output = root / mode
                args = build_parser().parse_args(
                    [
                        "create",
                        "-m",
                        mode,
                        "-s",
                        str(source),
                        "-n",
                        "Test",
                        "-o",
                        str(output),
                        "-b",
                        "10000000",
                        "-y",
                    ]
                )
                module = "create_raw" if mode == "raw" else "create"
                with (
                    self.subTest(mode=mode),
                    patch(f"bd_archive.commands.{module}.check_deps"),
                    patch(
                        f"bd_archive.commands.{module}.software_info",
                        side_effect=ValueError("Version query failed"),
                    ),
                    self.assertRaises((ValueError, SystemExit)),
                ):
                    cmd_create(args)
                self.assertFalse(output.exists())
