"""Exercise the real preload helper with a simulated SG_IO backend."""

import contextlib
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bd_archive.cli import build_parser
from bd_archive.tools import burn_timeout, growisofs

ROOT = Path(__file__).resolve().parents[1]


class TimeoutOptionTests(unittest.TestCase):
    def test_cli_default_override_and_invalid_values(self):
        parser = build_parser()
        self.assertEqual(
            parser.parse_args(["burn", "-i", "out"]).write_timeout,
            burn_timeout.DEFAULT_WRITE_TIMEOUT,
        )
        self.assertEqual(
            parser.parse_args(["burn", "-i", "out", "--write-timeout", "900"]).write_timeout, 900
        )
        for value in ("0", "-1", "86401", "1.5"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(["burn", "-i", "out", "--write-timeout", value])

    def test_activation_failure_prevents_burn_process(self):
        with (
            patch.object(growisofs, "prepared_burn", side_effect=ValueError("Cannot activate")),
            patch.object(growisofs.subprocess, "Popen") as start,
            self.assertRaisesRegex(ValueError, "Cannot activate"),
        ):
            growisofs.burn("/dev/test", Path("disc.iso"))
        start.assert_not_called()

    def test_iso_and_folder_launch_forward_timeout_and_preserve_environment(self):
        for filesystem in (None, ["-udf", "-graft-points", "/=folder"]):
            process = Mock(stdout=iter([]), returncode=0)
            with (
                patch.object(
                    growisofs,
                    "prepared_burn",
                    side_effect=lambda d, t, e: contextlib.nullcontext({"env": e}),
                ) as prepare,
                patch.object(growisofs.subprocess, "Popen", return_value=process),
                patch.object(growisofs.shutil, "which", return_value="/usr/bin/mkisofs"),
                patch.dict(os.environ, {"BD_TEST_ENV": "preserved"}),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                growisofs.burn(
                    "/dev/test",
                    None if filesystem else Path("disc.iso"),
                    filesystem_args=filesystem,
                    write_timeout=900,
                )
            self.assertEqual(prepare.call_args.args[:2], ("/dev/test", 900))
            self.assertEqual(prepare.call_args.args[2]["BD_TEST_ENV"], "preserved")


@unittest.skipUnless(shutil.which("cc"), "requires a C compiler")
class NativeTimeoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="bd-timeout-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.library = cls.directory / "helper with spaces.so"
        cls.client = cls.directory / "fake-growisofs"
        flags = ["cc", "-Wall", "-Wextra", "-Werror", "-O2"]
        subprocess.run(
            [
                *flags,
                "-shared",
                "-fPIC",
                "-Wl,-Bsymbolic-functions",
                str(ROOT / "src/bd_archive/_native/burn_timeout.c"),
                "-o",
                str(cls.library),
                "-ldl",
            ],
            check=True,
        )
        subprocess.run(
            [
                *flags,
                "-shared",
                "-fPIC",
                str(ROOT / "tests/native/timeout_backend.c"),
                "-o",
                str(cls.directory / "libbackend.so"),
                "-ldl",
            ],
            check=True,
        )
        subprocess.run(
            [
                *flags,
                str(ROOT / "tests/native/timeout_client.c"),
                "-o",
                str(cls.client),
                f"-L{cls.directory}",
                "-lbackend",
                "-Wl,-rpath,$ORIGIN",
            ],
            check=True,
        )

    @contextlib.contextmanager
    def configuration(self, library=None, executable=None):
        # Real /dev/null substitutes for a block device only in the test backend.
        real_stat = os.stat

        def device_stat(path, *args, **kwargs):
            if path == "/dev/test-burner":
                return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(1, 3))
            return real_stat(path, *args, **kwargs)

        with (
            patch.object(burn_timeout, "LIBRARY", library or self.library),
            patch.object(burn_timeout.shutil, "which", return_value=str(executable or self.client)),
            patch.object(burn_timeout.os, "stat", side_effect=device_stat),
        ):
            yield

    def test_native_interception_and_child_isolation(self):
        for seconds in (burn_timeout.DEFAULT_WRITE_TIMEOUT, 720):
            with (
                self.configuration(),
                burn_timeout.prepared_burn("/dev/test-burner", seconds, dict(os.environ)) as launch,
            ):
                result = subprocess.run(
                    [str(self.client), str(seconds * 1000)],
                    **launch,
                    capture_output=True,
                    text=True,
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "timeout interception verified\n")
            self.assertEqual(result.stderr, "")
            for fd in launch["pass_fds"]:
                with self.assertRaises(OSError):
                    os.fstat(fd)

    def test_missing_wrong_or_ignored_library_is_rejected(self):
        wrong = self.directory / "wrong.so"
        wrong.write_text("not a shared library")
        for library in (self.directory / "missing.so", wrong, self.directory / "libbackend.so"):
            with (
                self.configuration(library=library),
                self.assertRaises(ValueError),
                burn_timeout.prepared_burn(
                    "/dev/test-burner", burn_timeout.DEFAULT_WRITE_TIMEOUT, dict(os.environ)
                ),
            ):
                self.fail("Invalid helper must not permit burning")

    def test_setuid_and_loader_overrides_are_rejected(self):
        privileged = self.directory / "privileged"
        shutil.copy2(self.client, privileged)
        privileged.chmod(0o4755)
        with (
            self.configuration(executable=privileged),
            self.assertRaisesRegex(ValueError, "setuid"),
            burn_timeout.prepared_burn(
                "/dev/test-burner", burn_timeout.DEFAULT_WRITE_TIMEOUT, dict(os.environ)
            ),
        ):
            self.fail("Setuid executable accepted")
        for key in ("LD_PRELOAD", "LD_AUDIT"):
            with (
                self.configuration(),
                self.assertRaisesRegex(ValueError, key),
                burn_timeout.prepared_burn(
                    "/dev/test-burner", burn_timeout.DEFAULT_WRITE_TIMEOUT, {key: "other.so"}
                ),
            ):
                self.fail("Conflicting loader override accepted")

    def test_actual_installed_growisofs_loads_helper_without_entering_main(self):
        executable = shutil.which("growisofs")
        if executable is None:
            self.skipTest("growisofs is unavailable")
        with (
            self.configuration(executable=executable),
            burn_timeout.prepared_burn(
                "/dev/test-burner", burn_timeout.DEFAULT_WRITE_TIMEOUT, dict(os.environ)
            ),
        ):
            pass  # The constructor probe exits without opening any device.
