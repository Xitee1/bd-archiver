"""--fill-recovery: one rule for every disc, skipped when it adds too little."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd_archive import _par2_helper
from bd_archive.archive.share import (
    fill_skipped_message,
    fill_worthwhile,
    parse_redundancy,
)
from bd_archive.archive.sizing import dar_fill_layout, dar_recovery_bytes

GB = 10**9
MB = 10**6


class FillRuleTests(unittest.TestCase):
    def test_worthwhile_from_half_a_percent_of_the_data(self):
        self.assertTrue(fill_worthwhile(125 * MB, 25 * GB))
        self.assertFalse(fill_worthwhile(124 * MB, 25 * GB))
        self.assertFalse(fill_worthwhile(0, 0))
        self.assertEqual(
            fill_skipped_message(6 * MB),
            "Filling free space would add only 5.7 MiB recovery data "
            "(below 0.5% of the disc's data); skipped.",
        )

    def test_dar_slices_use_the_same_rule_with_their_actual_size(self):
        five, zero = parse_redundancy("5"), parse_redundancy("0")
        available = 24 * GB
        # Full slices as compute_slice_bytes cuts them: floored to whole MiB.
        full = int(available * 100 / 105) // 2**20 * 2**20
        full_zero = available // 2**20 * 2**20
        last = 8 * GB
        self.assertEqual(dar_recovery_bytes(full, available, five, False), five.bytes_of(full))
        self.assertEqual(dar_recovery_bytes(last, available, five, False), five.bytes_of(last))
        # Full disc: too little to fill, the minimum stays.
        self.assertEqual(dar_recovery_bytes(full, available, five, True), five.bytes_of(full))
        self.assertEqual(dar_recovery_bytes(full_zero, available, zero, True), 0)
        # Last disc: the free space is filled.
        self.assertEqual(dar_recovery_bytes(last, available, five, True), available - last)
        self.assertEqual(dar_recovery_bytes(last, available, zero, True), available - last)

    def test_fill_layout_counts_packet_headers(self):
        for size, slice_size in ((16 * GB, 8 * GB), (20 * GB, 10 * MB)):
            with self.subTest(size=size, slice_size=slice_size):
                block_size, count = dar_fill_layout(size, slice_size)
                self.assertLessEqual(count * (block_size + 68), size)
                self.assertGreater(count * block_size, 0)


class Par2HookTests(unittest.TestCase):
    def run_hook(self, slice_size, redundancy, *, fill, number="0002", available=None):
        with tempfile.TemporaryDirectory() as temp:
            slice_path = Path(temp) / f"Test-gen1.{number}.dar"
            with slice_path.open("wb") as stream:
                stream.truncate(slice_size)
            env = {
                "BD_ARCHIVE_SLICE_DIR": temp,
                "BD_ARCHIVE_SLICE_BASENAME": "Test-gen1",
                "BD_ARCHIVE_SLICE_AVAILABLE": str(available or 24 * GB),
                "BD_ARCHIVE_FIRST_SLICE_AVAILABLE": str(available or 24 * GB),
            }
            argv = ["helper", number, redundancy, *(["fill"] if fill else [])]
            with (
                patch.dict(os.environ, env),
                patch.object(sys, "argv", argv),
                patch("bd_archive._par2_helper.par2.create") as create,
            ):
                _par2_helper.main()
            return create

    def test_without_fill_the_minimum_is_created_as_before(self):
        create = self.run_hook(8 * MB, "5", fill=False)
        create.assert_called_once()
        self.assertEqual(create.call_args.args[1:], (5,))

    def test_fill_sizes_recovery_from_the_finished_slice(self):
        create = self.run_hook(8 * MB, "5", fill=True, available=24 * MB)
        block_size = create.call_args.kwargs["block_size"]
        count = create.call_args.kwargs["recovery_blocks"]
        self.assertEqual((block_size, count), dar_fill_layout(16 * MB, 8 * MB))

    def test_fill_that_is_not_worthwhile_keeps_the_minimum_or_creates_nothing(self):
        create = self.run_hook(8 * MB, "5", fill=True, available=8 * MB + 400_000 + 10_000)
        self.assertEqual(create.call_args.args[1:], (5,))
        create = self.run_hook(8 * MB, "0", fill=True, available=8 * MB + 10_000)
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
