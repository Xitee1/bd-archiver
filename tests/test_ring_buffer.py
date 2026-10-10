"""The growisofs ring buffer option: parsing, forwarding and memory-lock limits."""

import contextlib
import io
import os
import resource
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bd_archive.cli import build_parser
from bd_archive.constants import MiB
from bd_archive.tools import growisofs

GiB = 1024 * MiB
INFINITY = resource.RLIM_INFINITY


class RingBufferOptionTests(unittest.TestCase):
    def test_sizes_round_up_to_a_power_of_two(self):
        for value, expected in (
            ("512", 512 * MiB),
            ("512M", 512 * MiB),
            ("512m", 512 * MiB),
            ("300M", 512 * MiB),
            ("1G", GiB),
            ("1", MiB),
            ("64G", 64 * GiB),
        ):
            with self.subTest(value=value):
                self.assertEqual(growisofs.ring_buffer(value), expected)

    def test_invalid_sizes_are_rejected(self):
        for value in ("0", "65G", "1.5G", "512K", "abc", "", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                growisofs.ring_buffer(value)

    def test_cli_default_and_override(self):
        parser = build_parser()
        self.assertEqual(
            parser.parse_args(["burn", "-i", "out"]).buffer, growisofs.DEFAULT_RING_BUFFER
        )
        self.assertEqual(parser.parse_args(["burn", "-i", "out", "--buffer", "1G"]).buffer, GiB)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["burn", "-i", "out", "--buffer", "0"])

    def test_iso_and_folder_launch_forward_buffer_size_and_limit_hook(self):
        for filesystem in (None, ["-udf", "-graft-points", "/=folder"]):
            for size in (growisofs.DEFAULT_RING_BUFFER, GiB):
                process = Mock(stdout=iter([]), returncode=0)
                with (
                    patch.object(
                        growisofs,
                        "prepared_burn",
                        side_effect=lambda d, t, e: contextlib.nullcontext({"env": e}),
                    ),
                    patch.object(growisofs.subprocess, "Popen", return_value=process) as popen,
                    patch.object(growisofs.shutil, "which", return_value="/usr/bin/mkisofs"),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    growisofs.burn(
                        "/dev/test",
                        None if filesystem else Path("disc.iso"),
                        filesystem_args=filesystem,
                        ring_buffer=size,
                    )
                command = popen.call_args.args[0]
                with self.subTest(filesystem=filesystem, size=size):
                    self.assertIn(f"-use-the-force-luke=bufsize:{size // MiB}m", command)
                    self.assertLess(
                        command.index("-use-the-force-luke=spare=none"), command.index("-Z")
                    )
                    hook = popen.call_args.kwargs["preexec_fn"]
                    self.assertIsNone(self.run_hook(hook, (8 * MiB, 8 * MiB)))
                    self.assertEqual(
                        self.run_hook(hook, (64 * MiB, 64 * MiB)), (47 * MiB, 47 * MiB)
                    )

    @staticmethod
    def run_hook(hook, limits):
        """Run the child-side hook against simulated limits; return what it set, if anything."""
        with (
            patch.object(growisofs.resource, "getrlimit", return_value=limits),
            patch.object(growisofs.resource, "setrlimit") as setrlimit,
        ):
            hook()
        if not setrlimit.called:
            return None
        setrlimit.assert_called_once()
        (limit, wanted), _ = setrlimit.call_args
        assert limit == resource.RLIMIT_MEMLOCK
        return wanted

    def test_default_buffer_is_forwarded_when_not_given(self):
        process = Mock(stdout=iter([]), returncode=0)
        with (
            patch.object(
                growisofs,
                "prepared_burn",
                side_effect=lambda d, t, e: contextlib.nullcontext({"env": e}),
            ),
            patch.object(growisofs.subprocess, "Popen", return_value=process) as popen,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            growisofs.burn("/dev/test", Path("disc.iso"))
        self.assertIn(
            f"-use-the-force-luke=bufsize:{growisofs.DEFAULT_RING_BUFFER // MiB}m",
            popen.call_args.args[0],
        )


class MemlockLimitTests(unittest.TestCase):
    def test_limits_let_growisofs_lock_the_buffer_or_skip_locking(self):
        buffer = 512 * MiB
        for limits, expected in (
            # Hard limit below growisofs's raise target: it skips locking already.
            ((8 * MiB, 8 * MiB), (8 * MiB, 8 * MiB)),
            ((64 * 1024, 8 * MiB), (64 * 1024, 8 * MiB)),
            # Hard limit between the raise target and the buffer: lower it so
            # growisofs skips locking instead of failing to map the buffer.
            ((64 * MiB, 64 * MiB), (47 * MiB, 47 * MiB)),
            ((8 * MiB, 64 * MiB), (8 * MiB, 47 * MiB)),
            ((48 * MiB, 512 * MiB), (47 * MiB, 47 * MiB)),
            # Hard limit covers buffer plus headroom: lock the whole buffer.
            ((8 * MiB, 576 * MiB), (576 * MiB, 576 * MiB)),
            ((64 * MiB, 2 * GiB), (2 * GiB, 2 * GiB)),
            ((8 * MiB, INFINITY), (INFINITY, INFINITY)),
            ((INFINITY, INFINITY), (INFINITY, INFINITY)),
        ):
            with self.subTest(limits=limits):
                self.assertEqual(growisofs.memlock_limits(limits, buffer), expected)

    def test_hook_applies_limits_in_the_current_process_without_raising_hard_limit(self):
        # Lowering the hard limit is irreversible, so exercise the hook in a child.
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            try:
                growisofs._apply_memlock_limits(512 * MiB)
                soft, hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
                ok = (soft, hard) == growisofs.memlock_limits(
                    resource.getrlimit(resource.RLIMIT_MEMLOCK), 512 * MiB
                ) and (hard == INFINITY or hard >= 576 * MiB or hard < 48 * MiB)
                os._exit(0 if ok else 1)
            except BaseException:
                os._exit(2)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)


if __name__ == "__main__":
    unittest.main()
