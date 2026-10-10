"""PAR2 verification command and result handling."""

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from bd_archive.tools.par2 import VerifyResult, verify


class Par2VerificationTests(unittest.TestCase):
    def test_verification_limits_parallel_file_reads_and_preserves_results(self):
        index = Path("/disc/archive/recovery.par2")
        for base_dir in (None, Path("/disc")):
            for code, expected in (
                (0, VerifyResult.OK),
                (1, VerifyResult.REPAIRABLE),
                (2, VerifyResult.BROKEN),
                (3, VerifyResult.BROKEN),
            ):
                with self.subTest(base_dir=base_dir, code=code):
                    with patch(
                        "bd_archive.tools.par2.run", return_value=Mock(returncode=code)
                    ) as run:
                        self.assertEqual(verify(index, base_dir=base_dir), expected)
                    base_args = [f"-B{base_dir}"] if base_dir is not None else []
                    run.assert_called_once_with(
                        ["par2", "verify", "-T1", *base_args, str(index)],
                        check=False,
                        output_transform=ANY,
                    )

    def test_verification_ends_the_live_scan_line_after_par2_exits(self):
        index = Path("/disc/archive/recovery.par2")
        for base_dir, expected_base in ((None, index.parent), (Path("/disc"), Path("/disc"))):
            with self.subTest(base_dir=base_dir):
                progress = Mock()
                progress.finish.return_value = "END\n"
                with (
                    patch("bd_archive.tools.par2.run", return_value=Mock(returncode=0)) as run,
                    patch("bd_archive.tools.par2.Par2ScanProgress", return_value=progress) as cls,
                    redirect_stdout(io.StringIO()) as out,
                ):
                    self.assertEqual(verify(index, base_dir=base_dir), VerifyResult.OK)
                cls.assert_called_once_with(expected_base)
                self.assertIs(run.call_args.kwargs["output_transform"], progress)
                progress.finish.assert_called_once_with()
                self.assertEqual(out.getvalue(), "END\n")
