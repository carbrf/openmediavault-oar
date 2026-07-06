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
"""Plan coverage: golden argv sequences for a canonical create and
grow, the grow scenarios from the contract (join / new tier /
unallocatable) and replace planning."""
import unittest

from oar.layout import (
    Disk,
    LayoutError,
    compute_tiers,
    plan_create,
    plan_grow,
    plan_replace,
    usable,
)

MiB = 1024 ** 2
GiB = 1024 ** 3
TiB = 1024 ** 4

GUID = "A19D880F-05FC-4D3B-A006-743F0F84911E"


def argvs(plan):
    return [list(step.argv) for step in plan.steps]


def tier_map(layout):
    return [(t.index, len(t.members)) for t in layout.tiers]


class GoldenCreateTestCase(unittest.TestCase):
    """Exact command sequence for a canonical 3 mixed-disk create.

    10 GiB usable = 10048 MiB, 20 GiB usable = 20288 MiB. Tier 0 spans
    [1 MiB, 10049 MiB) = sectors 2048..20580351, tier 1 spans
    [10049 MiB, 20289 MiB) = sectors 20580352..41551871.
    """

    maxDiff = None

    def test_full_sequence(self):
        plan = plan_create(
            "tank",
            [
                Disk("/dev/sdb", 10 * GiB),
                Disk("/dev/sdc", 20 * GiB),
                Disk("/dev/sdd", 20 * GiB),
            ],
            fs="btrfs",
        )
        self.assertEqual(
            argvs(plan),
            [
                ["sgdisk", "--zap-all", "/dev/sdb"],
                ["wipefs", "-a", "/dev/sdb"],
                ["sgdisk", "--zap-all", "/dev/sdc"],
                ["wipefs", "-a", "/dev/sdc"],
                ["sgdisk", "--zap-all", "/dev/sdd"],
                ["wipefs", "-a", "/dev/sdd"],
                [
                    "sgdisk",
                    "--new=1:2048:20580351",
                    "--change-name=1:oar@tank@t00",
                    "--typecode=1:%s" % GUID,
                    "/dev/sdb",
                ],
                [
                    "sgdisk",
                    "--new=1:2048:20580351",
                    "--change-name=1:oar@tank@t00",
                    "--typecode=1:%s" % GUID,
                    "/dev/sdc",
                ],
                [
                    "sgdisk",
                    "--new=2:20580352:41551871",
                    "--change-name=2:oar@tank@t01",
                    "--typecode=2:%s" % GUID,
                    "/dev/sdc",
                ],
                [
                    "sgdisk",
                    "--new=1:2048:20580351",
                    "--change-name=1:oar@tank@t00",
                    "--typecode=1:%s" % GUID,
                    "/dev/sdd",
                ],
                [
                    "sgdisk",
                    "--new=2:20580352:41551871",
                    "--change-name=2:oar@tank@t01",
                    "--typecode=2:%s" % GUID,
                    "/dev/sdd",
                ],
                ["udevadm", "settle"],
                [
                    "mdadm",
                    "--create",
                    "/dev/md/tank-t00",
                    "--run",
                    "--force",
                    "--level=5",
                    "--metadata=1.2",
                    "--name=tank-t00",
                    "--consistency-policy=ppl",
                    "--raid-devices=3",
                    "/dev/sdb1",
                    "/dev/sdc1",
                    "/dev/sdd1",
                ],
                [
                    "mdadm",
                    "--create",
                    "/dev/md/tank-t01",
                    "--run",
                    "--force",
                    "--level=5",
                    "--metadata=1.2",
                    "--name=tank-t01",
                    "--consistency-policy=ppl",
                    "--raid-devices=2",
                    "/dev/sdc2",
                    "/dev/sdd2",
                ],
                ["pvcreate", "/dev/md/tank-t00"],
                ["pvcreate", "/dev/md/tank-t01"],
                [
                    "vgcreate",
                    "--addtag",
                    "omv-oar",
                    "tank",
                    "/dev/md/tank-t00",
                    "/dev/md/tank-t01",
                ],
                ["lvcreate", "-n", "data", "-l", "100%FREE", "tank"],
                [
                    "mkfs.btrfs",
                    "-L",
                    "tank",
                    "-d",
                    "single",
                    "-m",
                    "dup",
                    "/dev/tank/data",
                ],
            ],
        )
        # Capacity: 2 * 10048 MiB + 1 * 10240 MiB.
        self.assertEqual(plan.layout.usable_capacity, 30336 * MiB)
        self.assertEqual(plan.layout.unallocatable_bytes, 0)

    def test_ppl_fallback_to_bitmap(self):
        plan = plan_create(
            "tank", [Disk("/dev/sdb", 10 * GiB), Disk("/dev/sdc", 10 * GiB)]
        )
        creates = [
            s for s in plan.steps if s.argv[:2] == ("mdadm", "--create")
        ]
        self.assertEqual(len(creates), 1)
        self.assertIn("--consistency-policy=ppl", creates[0].argv)
        self.assertIsNotNone(creates[0].fallback_argv)
        self.assertIn(
            "--consistency-policy=bitmap", creates[0].fallback_argv
        )

    def test_ext4_variant(self):
        plan = plan_create(
            "tank",
            [Disk("/dev/sdb", 10 * GiB), Disk("/dev/sdc", 10 * GiB)],
            fs="ext4",
        )
        self.assertEqual(
            list(plan.steps[-1].argv),
            ["mkfs.ext4", "-L", "tank", "/dev/tank/data"],
        )

    def test_all_argv_items_are_strings(self):
        plan = plan_create(
            "tank", [Disk("/dev/sdb", 10 * GiB), Disk("/dev/sdc", 20 * GiB)]
        )
        for step in plan.steps:
            for item in step.argv:
                self.assertIsInstance(item, str)

    def test_validation(self):
        with self.assertRaises(LayoutError):  # < 2 disks
            plan_create("tank", [Disk("/dev/sdb", 10 * GiB)])
        with self.assertRaises(LayoutError):  # bad fs
            plan_create(
                "tank",
                [Disk("/dev/sdb", 10 * GiB), Disk("/dev/sdc", 10 * GiB)],
                fs="zfs",
            )
        with self.assertRaises(LayoutError):  # bad name
            plan_create(
                "0pool",
                [Disk("/dev/sdb", 10 * GiB), Disk("/dev/sdc", 10 * GiB)],
            )


class GoldenGrowTestCase(unittest.TestCase):
    """Exact command sequence for growing the canonical pool by one
    20 GiB disk: it joins both tiers, no new tier is created."""

    maxDiff = None

    def setUp(self):
        self.layout = compute_tiers(
            [
                Disk("/dev/sdb", 10 * GiB),
                Disk("/dev/sdc", 20 * GiB),
                Disk("/dev/sdd", 20 * GiB),
            ]
        )

    def test_full_sequence(self):
        plan = plan_grow("tank", self.layout, [Disk("/dev/sde", 20 * GiB)])
        self.assertEqual(
            argvs(plan),
            [
                ["sgdisk", "--zap-all", "/dev/sde"],
                ["wipefs", "-a", "/dev/sde"],
                [
                    "sgdisk",
                    "--new=1:2048:20580351",
                    "--change-name=1:oar@tank@t00",
                    "--typecode=1:%s" % GUID,
                    "/dev/sde",
                ],
                [
                    "sgdisk",
                    "--new=2:20580352:41551871",
                    "--change-name=2:oar@tank@t01",
                    "--typecode=2:%s" % GUID,
                    "/dev/sde",
                ],
                ["udevadm", "settle"],
                # PPL dance: the kernel refuses reshape while PPL is
                # active, so switch to bitmap first; finalize restores
                # PPL after the reshape.
                [
                    "mdadm",
                    "--grow",
                    "/dev/md/tank-t00",
                    "--consistency-policy=bitmap",
                ],
                ["mdadm", "--add", "/dev/md/tank-t00", "/dev/sde1"],
                ["mdadm", "--grow", "/dev/md/tank-t00", "--raid-devices=4"],
                [
                    "mdadm",
                    "--grow",
                    "/dev/md/tank-t01",
                    "--consistency-policy=bitmap",
                ],
                ["mdadm", "--add", "/dev/md/tank-t01", "/dev/sde2"],
                ["mdadm", "--grow", "/dev/md/tank-t01", "--raid-devices=3"],
                ["vgchange", "--addtag", "oar.finalize", "tank"],
                [
                    "systemctl",
                    "start",
                    "omv-oar-finalize@tank.service",
                    "--no-block",
                ],
            ],
        )
        self.assertEqual(tier_map(plan.layout), [(0, 4), (1, 3)])
        self.assertEqual(plan.layout.unallocatable_bytes, 0)


class GrowScenarioTestCase(unittest.TestCase):
    """The contract's 4T+8T+8T grow scenarios."""

    def setUp(self):
        self.base = compute_tiers(
            [
                Disk("/dev/sdb", 4 * TiB),
                Disk("/dev/sdc", 8 * TiB),
                Disk("/dev/sdd", 8 * TiB),
            ]
        )
        self.assertEqual(tier_map(self.base), [(0, 3), (1, 2)])

    def test_add_8t_grows_both_tiers(self):
        plan = plan_grow("tank", self.base, [Disk("/dev/sde", 8 * TiB)])
        self.assertEqual(tier_map(plan.layout), [(0, 4), (1, 3)])
        self.assertEqual(plan.layout.unallocatable_bytes, 0)
        # Same capacity as creating [4,8,8,8] from scratch.
        fresh = compute_tiers(
            [
                Disk("/dev/sdb", 4 * TiB),
                Disk("/dev/sdc", 8 * TiB),
                Disk("/dev/sdd", 8 * TiB),
                Disk("/dev/sde", 8 * TiB),
            ]
        )
        self.assertEqual(plan.layout.usable_capacity, fresh.usable_capacity)
        grows = [
            list(s.argv)
            for s in plan.steps
            if s.argv[:2] == ("mdadm", "--grow") and "--raid-devices=4" in s.argv
        ]
        self.assertEqual(
            grows, [["mdadm", "--grow", "/dev/md/tank-t00", "--raid-devices=4"]]
        )
        # No new arrays, no VG extension.
        self.assertFalse(
            [s for s in plan.steps if s.argv[0] in ("vgextend", "pvcreate")]
        )
        self.assertFalse(
            [s for s in plan.steps if s.argv[:2] == ("mdadm", "--create")]
        )

    def test_add_6t_grows_t0_with_honest_unallocatable(self):
        plan = plan_grow("tank", self.base, [Disk("/dev/sde", 6 * TiB)])
        # t0 grows to 4 members; t1 unchanged; the 6T disk's remainder
        # has no partner: ~2T unallocatable.
        self.assertEqual(tier_map(plan.layout), [(0, 4), (1, 2)])
        self.assertEqual(
            plan.layout.unallocatable_bytes,
            usable(6 * TiB) - usable(4 * TiB),
        )
        self.assertLess(
            abs(plan.layout.unallocatable_bytes - 2 * TiB), 1 * GiB
        )
        self.assertFalse(
            [s for s in plan.steps if s.argv[:2] == ("mdadm", "--create")]
        )
        # Only t0 is reshaped, and only after its PPL->bitmap switch.
        kinds = [
            list(s.argv)
            for s in plan.steps
            if s.argv[0] == "mdadm"
        ]
        self.assertEqual(
            kinds,
            [
                [
                    "mdadm",
                    "--grow",
                    "/dev/md/tank-t00",
                    "--consistency-policy=bitmap",
                ],
                ["mdadm", "--add", "/dev/md/tank-t00", "/dev/sde1"],
                ["mdadm", "--grow", "/dev/md/tank-t00", "--raid-devices=4"],
            ],
        )

    def test_add_two_6t_creates_new_two_member_tier(self):
        plan = plan_grow(
            "tank",
            self.base,
            [Disk("/dev/sde", 6 * TiB), Disk("/dev/sdf", 6 * TiB)],
        )
        # Both join t0; their equal remainders form new tier t2.
        self.assertEqual(tier_map(plan.layout), [(0, 5), (1, 2), (2, 2)])
        self.assertEqual(plan.layout.unallocatable_bytes, 0)
        new_tier = plan.layout.tiers[2]
        self.assertEqual(new_tier.height, usable(6 * TiB) - usable(4 * TiB))
        self.assertEqual(
            sorted(p.disk for p in new_tier.members),
            ["/dev/sde", "/dev/sdf"],
        )
        creates = [
            s for s in plan.steps if s.argv[:2] == ("mdadm", "--create")
        ]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0].argv[2], "/dev/md/tank-t02")
        self.assertIn("--raid-devices=2", creates[0].argv)
        self.assertIn(
            ["pvcreate", "/dev/md/tank-t02"], argvs(plan)
        )
        self.assertIn(
            ["vgextend", "tank", "/dev/md/tank-t02"], argvs(plan)
        )

    def test_grow_uses_existing_unallocated_tops(self):
        # [2,4,6,12] leaves ~6T unallocatable on the 12T disk; adding
        # another 12T disk pairs it up into a new tier.
        mixed = compute_tiers(
            [
                Disk("/dev/sdb", 2 * TiB),
                Disk("/dev/sdc", 4 * TiB),
                Disk("/dev/sdd", 6 * TiB),
                Disk("/dev/sde", 12 * TiB),
            ]
        )
        plan = plan_grow("tank", mixed, [Disk("/dev/sdf", 12 * TiB)])
        self.assertEqual(
            tier_map(plan.layout), [(0, 5), (1, 4), (2, 3), (3, 2)]
        )
        self.assertEqual(plan.layout.unallocatable_bytes, 0)

    def test_finalize_handoff_always_last(self):
        for new in ([Disk("/dev/sde", 8 * TiB)],
                    [Disk("/dev/sde", 6 * TiB), Disk("/dev/sdf", 6 * TiB)]):
            plan = plan_grow("tank", self.base, new)
            self.assertEqual(
                argvs(plan)[-2:],
                [
                    ["vgchange", "--addtag", "oar.finalize", "tank"],
                    [
                        "systemctl",
                        "start",
                        "omv-oar-finalize@tank.service",
                        "--no-block",
                    ],
                ],
            )

    def test_useless_disk_rejected(self):
        # Too small to join t0 and no partner for a new tier.
        with self.assertRaises(LayoutError):
            plan_grow("tank", self.base, [Disk("/dev/sde", 500 * GiB)])

    def test_member_disk_rejected(self):
        with self.assertRaises(LayoutError):
            plan_grow("tank", self.base, [Disk("/dev/sdb", 8 * TiB)])


class ReplaceTestCase(unittest.TestCase):
    def setUp(self):
        self.layout = compute_tiers(
            [
                Disk("/dev/sdb", 10 * GiB),
                Disk("/dev/sdc", 20 * GiB),
                Disk("/dev/sdd", 20 * GiB),
            ]
        )

    def test_too_small_replacement_rejected(self):
        # /dev/sdc holds slices for both tiers (20288 MiB); a 15 GiB
        # disk (15168 MiB usable) cannot mirror them.
        with self.assertRaises(LayoutError) as ctx:
            plan_replace(
                "tank", self.layout, "/dev/sdc", Disk("/dev/sde", 15 * GiB)
            )
        self.assertIn("too small", str(ctx.exception))

    def test_unknown_disks_rejected(self):
        with self.assertRaises(LayoutError):
            plan_replace(
                "tank", self.layout, "/dev/sdx", Disk("/dev/sde", 20 * GiB)
            )
        with self.assertRaises(LayoutError):  # new disk already a member
            plan_replace(
                "tank", self.layout, "/dev/sdb", Disk("/dev/sdc", 20 * GiB)
            )

    def test_hot_replace_keeps_redundancy(self):
        plan = plan_replace(
            "tank", self.layout, "/dev/sdb", Disk("/dev/sde", 10 * GiB)
        )
        mdadm = [list(s.argv) for s in plan.steps if s.argv[0] == "mdadm"]
        self.assertEqual(
            mdadm,
            [
                ["mdadm", "--add", "/dev/md/tank-t00", "/dev/sde1"],
                [
                    "mdadm",
                    "/dev/md/tank-t00",
                    "--replace",
                    "/dev/sdb1",
                    "--with",
                    "/dev/sde1",
                ],
            ],
        )
        # New disk mirrors the old slice exactly.
        self.assertIn(
            [
                "sgdisk",
                "--new=1:2048:20580351",
                "--change-name=1:oar@tank@t00",
                "--typecode=1:%s" % GUID,
                "/dev/sde",
            ],
            argvs(plan),
        )
        self.assertNotIn("/dev/sdb", [d.name for d in plan.layout.disks])

    def test_finalize_handoff_always_last_even_without_new_tier(self):
        # Same-size replacement forms no new tier, yet the finalize
        # handoff must still be the final two steps of the plan.
        plan = plan_replace(
            "tank", self.layout, "/dev/sdb", Disk("/dev/sde", 10 * GiB)
        )
        self.assertFalse([s for s in plan.steps if s.argv[0] == "vgextend"])
        steps = argvs(plan)
        self.assertEqual(
            steps[-2:],
            [
                ["vgchange", "--addtag", "oar.finalize", "tank"],
                ["systemctl", "start",
                 "omv-oar-finalize@tank.service", "--no-block"],
            ],
        )

    def test_dead_disk_uses_plain_add(self):
        plan = plan_replace(
            "tank",
            self.layout,
            "/dev/sdb",
            Disk("/dev/sde", 10 * GiB),
            old_alive=False,
        )
        mdadm = [list(s.argv) for s in plan.steps if s.argv[0] == "mdadm"]
        self.assertEqual(
            mdadm, [["mdadm", "--add", "/dev/md/tank-t00", "/dev/sde1"]]
        )

    def test_larger_disk_remainder_is_honest(self):
        # 12 GiB replacing the 10 GiB disk: 2 GiB of usable space has
        # no partner and stays unallocatable.
        plan = plan_replace(
            "tank", self.layout, "/dev/sdb", Disk("/dev/sde", 12 * GiB)
        )
        self.assertEqual(
            plan.layout.unallocatable_bytes,
            usable(12 * GiB) - usable(10 * GiB),
        )
        self.assertFalse(
            [s for s in plan.steps if s.argv[0] == "vgextend"]
        )

    def test_larger_disk_remainder_can_form_new_tier(self):
        # [2,4,6,12]: replacing the 2T disk with a 12T disk pairs its
        # remainder with the existing 12T disk's unallocated top.
        mixed = compute_tiers(
            [
                Disk("/dev/sdb", 2 * TiB),
                Disk("/dev/sdc", 4 * TiB),
                Disk("/dev/sdd", 6 * TiB),
                Disk("/dev/sde", 12 * TiB),
            ]
        )
        plan = plan_replace(
            "tank", mixed, "/dev/sdb", Disk("/dev/sdf", 12 * TiB)
        )
        self.assertEqual(
            tier_map(plan.layout), [(0, 4), (1, 3), (2, 2), (3, 2)]
        )
        new_tier = plan.layout.tiers[3]
        self.assertEqual(new_tier.height, usable(12 * TiB) - usable(6 * TiB))
        self.assertEqual(
            sorted(p.disk for p in new_tier.members),
            ["/dev/sde", "/dev/sdf"],
        )
        # Remaining gap between the mirrored slice and the new tier.
        self.assertEqual(
            plan.layout.unallocatable_bytes,
            usable(6 * TiB) - usable(2 * TiB),
        )
        steps = argvs(plan)
        self.assertIn(["vgextend", "tank", "/dev/md/tank-t03"], steps)
        self.assertIn(["vgchange", "--addtag", "oar.finalize", "tank"], steps)


if __name__ == "__main__":
    unittest.main()
