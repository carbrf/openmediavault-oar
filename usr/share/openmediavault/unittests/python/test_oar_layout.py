# -*- coding: utf-8 -*-
#
# This file is part of the openmediavault-oar plugin.
#
# @license   https://www.gnu.org/licenses/gpl.html GPL Version 3
# @author    carbrf <carbrf@gmail.com>
# @copyright Copyright (c) 2026 carbrf
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""Pure tier layout coverage: alignment/reserve math, the OAR-1
reference capacities and layout structure invariants."""
import unittest

from oar import layout
from oar.layout import (
    ALIGN,
    END_RESERVE,
    START_OFFSET,
    Disk,
    LayoutError,
    compute_tiers,
    preview,
    reconstruct,
    usable,
)

MiB = 1024 ** 2
GiB = 1024 ** 3
TiB = 1024 ** 4


def disks(*sizes):
    return [
        Disk("/dev/sd%s" % chr(ord("b") + i), size)
        for i, size in enumerate(sizes)
    ]


class UsableTestCase(unittest.TestCase):
    """usable() = floor((size - START_OFFSET - END_RESERVE)/ALIGN)*ALIGN"""

    def test_constants(self):
        self.assertEqual(START_OFFSET, 1 * MiB)
        self.assertEqual(END_RESERVE, 128 * MiB)
        self.assertEqual(ALIGN, 64 * MiB)

    def test_zero_for_tiny_disks(self):
        self.assertEqual(usable(0), 0)
        self.assertEqual(usable(START_OFFSET + END_RESERVE), 0)
        # One byte short of the first aligned chunk.
        self.assertEqual(usable(START_OFFSET + END_RESERVE + ALIGN - 1), 0)

    def test_first_chunk(self):
        self.assertEqual(usable(START_OFFSET + END_RESERVE + ALIGN), ALIGN)
        # Anything below the next boundary still yields one chunk.
        self.assertEqual(
            usable(START_OFFSET + END_RESERVE + 2 * ALIGN - 1), ALIGN
        )

    def test_reserve_subtracted(self):
        # 4 TiB is ALIGN-aligned, so the 129 MiB overhead costs
        # ceil(129/64) = 3 chunks = 192 MiB.
        self.assertEqual(usable(4 * TiB), 4 * TiB - 192 * MiB)

    def test_always_aligned(self):
        for size in (10 * GiB, 10 * GiB + 12345, 3 * TiB - 1, 8 * TiB):
            self.assertEqual(usable(size) % ALIGN, 0, size)

    def test_monotonic(self):
        previous = -1
        for size in range(0, 3 * ALIGN + START_OFFSET + END_RESERVE, 7 * MiB):
            value = usable(size)
            self.assertGreaterEqual(value, previous)
            previous = value


class ReferenceTestCase(unittest.TestCase):
    """The four OAR-1 reference capacity cases (single parity:
    usable = sum over tiers of (members - 1) x height)."""

    def capacity(self, *sizes_tib):
        lay = compute_tiers(disks(*[s * TiB for s in sizes_tib]))
        return lay

    def test_4_8_8_8_gives_20t(self):
        lay = self.capacity(4, 8, 8, 8)
        expected = usable(4 * TiB) + 2 * usable(8 * TiB)
        self.assertEqual(lay.usable_capacity, expected)
        self.assertLess(abs(lay.usable_capacity - 20 * TiB), 1 * GiB)
        self.assertEqual(lay.unallocatable_bytes, 0)
        self.assertEqual(
            [(t.index, len(t.members)) for t in lay.tiers], [(0, 4), (1, 3)]
        )

    def test_1_1_2_2_2_gives_6t(self):
        lay = self.capacity(1, 1, 2, 2, 2)
        expected = 2 * usable(1 * TiB) + 2 * usable(2 * TiB)
        self.assertEqual(lay.usable_capacity, expected)
        self.assertLess(abs(lay.usable_capacity - 6 * TiB), 1 * GiB)
        self.assertEqual(lay.unallocatable_bytes, 0)
        self.assertEqual(
            [(t.index, len(t.members)) for t in lay.tiers], [(0, 5), (1, 3)]
        )

    def test_4_4_gives_4t(self):
        # A 2-member RAID5 is valid (grows without level change).
        lay = self.capacity(4, 4)
        self.assertEqual(lay.usable_capacity, usable(4 * TiB))
        self.assertLess(abs(lay.usable_capacity - 4 * TiB), 1 * GiB)
        self.assertEqual(len(lay.tiers), 1)
        self.assertEqual(len(lay.tiers[0].members), 2)
        self.assertEqual(lay.tiers[0].level, "raid5")

    def test_2_4_6_12_gives_12t_plus_6t_unallocatable(self):
        lay = self.capacity(2, 4, 6, 12)
        expected = usable(2 * TiB) + usable(4 * TiB) + usable(6 * TiB)
        self.assertEqual(lay.usable_capacity, expected)
        self.assertLess(abs(lay.usable_capacity - 12 * TiB), 1 * GiB)
        # The 12T disk's top span has no partner: honest reporting.
        self.assertEqual(
            lay.unallocatable_bytes, usable(12 * TiB) - usable(6 * TiB)
        )
        self.assertLess(abs(lay.unallocatable_bytes - 6 * TiB), 1 * GiB)
        self.assertEqual(
            [(t.index, len(t.members)) for t in lay.tiers],
            [(0, 4), (1, 3), (2, 2)],
        )
        self.assertEqual(len(lay.unallocatable), 1)
        self.assertEqual(lay.unallocatable[0].disk, "/dev/sde")
        self.assertEqual(lay.unallocatable[0].tier, -1)


class LayoutStructureTestCase(unittest.TestCase):
    def test_slices_stack_from_start_offset(self):
        lay = compute_tiers(disks(10 * GiB, 20 * GiB, 20 * GiB))
        for disk in ("/dev/sdc", "/dev/sdd"):
            parts = sorted(
                (p for t in lay.tiers for p in t.members if p.disk == disk),
                key=lambda p: p.start,
            )
            self.assertEqual(parts[0].start, START_OFFSET)
            for a, b in zip(parts, parts[1:]):
                self.assertEqual(a.end, b.start)

    def test_tier_members_share_height(self):
        lay = compute_tiers(disks(1 * TiB, 2 * TiB, 3 * TiB, 3 * TiB))
        for tier in lay.tiers:
            heights = {p.size for p in tier.members}
            self.assertEqual(len(heights), 1)
            self.assertEqual(tier.height % ALIGN, 0)

    def test_capacity_formula(self):
        lay = compute_tiers(disks(1 * TiB, 2 * TiB, 3 * TiB, 3 * TiB))
        self.assertEqual(
            lay.usable_capacity,
            sum((len(t.members) - 1) * t.height for t in lay.tiers),
        )

    def test_single_disk_is_all_unallocatable(self):
        lay = compute_tiers(disks(4 * TiB))
        self.assertEqual(lay.tiers, ())
        self.assertEqual(lay.unallocatable_bytes, usable(4 * TiB))

    def test_duplicate_disks_rejected(self):
        with self.assertRaises(LayoutError):
            compute_tiers([Disk("/dev/sdb", TiB), Disk("/dev/sdb", TiB)])

    def test_too_small_disk_rejected(self):
        with self.assertRaises(LayoutError):
            compute_tiers(disks(4 * TiB, 64 * MiB))

    def test_pool_name_validation(self):
        layout.validate_pool_name("tank")
        layout.validate_pool_name("a" * 32)
        for bad in ("", "0tank", "-tank", "a" * 33, "ta:nk", "ta nk"):
            with self.assertRaises(LayoutError, msg=bad):
                layout.validate_pool_name(bad)

    def test_partition_device_naming(self):
        self.assertEqual(layout.partition_device("/dev/sdb", 1), "/dev/sdb1")
        self.assertEqual(
            layout.partition_device("/dev/nvme0n1", 2), "/dev/nvme0n1p2"
        )
        self.assertEqual(
            layout.partition_device("/dev/loop0", 1), "/dev/loop0p1"
        )

    def test_names_and_labels(self):
        self.assertEqual(layout.tier_name("tank", 3), "tank-t03")
        self.assertEqual(layout.md_device("tank", 0), "/dev/md/tank-t00")
        self.assertEqual(layout.partlabel("tank", 12), "oar:tank:t12")


class PreviewTestCase(unittest.TestCase):
    def test_preview_shape(self):
        result = preview(disks(2 * TiB, 4 * TiB, 6 * TiB, 12 * TiB))
        self.assertEqual(
            sorted(result), ["tiers", "unallocatable", "usable"]
        )
        self.assertIsInstance(result["usable"], int)
        self.assertIsInstance(result["unallocatable"], int)
        self.assertEqual(
            result["tiers"],
            [
                {
                    "index": 0,
                    "height": usable(2 * TiB),
                    "members": 4,
                    "level": "raid5",
                },
                {
                    "index": 1,
                    "height": usable(4 * TiB) - usable(2 * TiB),
                    "members": 3,
                    "level": "raid5",
                },
                {
                    "index": 2,
                    "height": usable(6 * TiB) - usable(4 * TiB),
                    "members": 2,
                    "level": "raid5",
                },
            ],
        )


class ReconstructTestCase(unittest.TestCase):
    def test_roundtrip_matches_compute_tiers(self):
        source = compute_tiers(disks(2 * TiB, 4 * TiB, 6 * TiB, 12 * TiB))
        rebuilt = reconstruct(
            list(source.disks),
            [
                (t.index, t.height, [p.disk for p in t.members])
                for t in source.tiers
            ],
        )
        self.assertEqual(rebuilt.tiers, source.tiers)
        self.assertEqual(rebuilt.unallocatable, source.unallocatable)
        self.assertEqual(rebuilt.usable_capacity, source.usable_capacity)

    def test_unknown_disk_rejected(self):
        with self.assertRaises(LayoutError):
            reconstruct(disks(4 * TiB), [(0, ALIGN, ["/dev/nope"])])


if __name__ == "__main__":
    unittest.main()
