"""PAR2 verification command and result handling."""

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
                        passthrough=True,
                    )
