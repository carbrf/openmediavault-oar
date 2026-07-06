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
"""System introspection coverage: the lsblk/mdstat/LVM parser contracts,
pool discovery via partlabels and VG tags, status/state derivation and
candidate disk selection — all over a synthetic 3-disk 'tank' pool
(sdb 10 GiB, sdc/sdd 20 GiB, tiers t00/t01 on md126/md127)."""
import unittest

from oar import layout
from oar.sysinfo import (
    SystemState,
    candidates,
    dm_name,
    format_binary,
    parse_lsblk,
    parse_lvm_report,
    parse_mdstat,
    parse_sync_completed,
    pool_disks,
    pool_layout,
    pool_names,
    pool_status,
    pool_tiers,
    stale_slices,
    vg_tags,
)

MiB = 1024 ** 2
GiB = 1024 ** 3

#: Tier slice heights exactly as plan_create would carve them, so the
#: reconstructed layout of the fixture pool has no unallocatable space.
T0_HEIGHT = layout.usable(10 * GiB)
T1_HEIGHT = layout.usable(20 * GiB) - T0_HEIGHT

LV_SIZE = 25 * GiB
VG_SIZE = 2 * T0_HEIGHT + T1_HEIGHT
VG_FREE = VG_SIZE - LV_SIZE
#: Injected statvfs numbers; deliberately different from the VG numbers
#: so the tests can tell which source pool_status used.
ST_SIZE = 24 * GiB + 512 * MiB
ST_FREE = 9 * GiB
TANK_MNT = "/srv/dev-disk-by-uuid-0a1b2c3d"
PART_GUID = "a19d880f-05fc-4d3b-a006-743f0f84911e"


# -----------------------------------------------------------------------
# Raw-text fixtures (real /proc/mdstat and lsblk -J -b syntax).
# -----------------------------------------------------------------------

MDSTAT_MIXED = """\
Personalities : [raid1] [raid6] [raid5] [raid4]
md126 : active raid5 sdb1[0] sdc1[1](F) sdd1[2](S) sde1[3](R)
      20961280 blocks super 1.2 level 5, 512k chunk, algorithm 2 [3/2] [U_U]
      bitmap: 1/1 pages [4KB], 65536KB chunk

md127 : inactive sdf1[0](S)
      10480640 blocks super external:imsm

unused devices: <none>
"""

TANK_MDSTAT = """\
Personalities : [raid6] [raid5] [raid4]
md126 : active raid5 sdd1[2] sdc1[1] sdb1[0]
      20578304 blocks super 1.2 level 5, 512k chunk, algorithm 2 [3/3] [UUU]
      bitmap: 0/1 pages [0KB], 65536KB chunk

md127 : active raid5 sdd2[1] sdc2[0]
      10289152 blocks super 1.2 level 5, 512k chunk, algorithm 2 [2/2] [UU]

unused devices: <none>
"""

TANK_MDSTAT_FAULTY = """\
Personalities : [raid6] [raid5] [raid4]
md126 : active raid5 sdd1[2] sdc1[1](F) sdb1[0]
      20578304 blocks super 1.2 level 5, 512k chunk, algorithm 2 [3/2] [U_U]

md127 : active raid5 sdd2[1] sdc2[0]
      10289152 blocks super 1.2 level 5, 512k chunk, algorithm 2 [2/2] [UU]

unused devices: <none>
"""

TANK_MDSTAT_DISK_GONE = """\
Personalities : [raid6] [raid5] [raid4]
md126 : active raid5 sdc1[1] sdb1[0]
      20578304 blocks super 1.2 level 5, 512k chunk, algorithm 2 [3/2] [UU_]

md127 : active raid5 sdc2[0]
      10289152 blocks super 1.2 level 5, 512k chunk, algorithm 2 [2/1] [U_]

unused devices: <none>
"""

#: t00 array full again after a hot-replace: the replacement sde1 is
#: active and the old sdb1 lingers as a faulty extra member (slots==up).
STALE_MDSTAT_HOT_FAULTY = """\
Personalities : [raid6] [raid5] [raid4]
md126 : active raid5 sde1[3] sdd1[2] sdc1[1] sdb1[0](F)
      20578304 blocks super 1.2 level 5, 512k chunk, algorithm 2 [3/3] [UUU]

md127 : active raid5 sdd2[1] sdc2[0]
      10289152 blocks super 1.2 level 5, 512k chunk, algorithm 2 [2/2] [UU]

unused devices: <none>
"""

#: t00 array full again, but the old sdb1 has been evicted entirely --
#: it is no longer a member at all (orphaned slice).
STALE_MDSTAT_HOT_ORPHAN = """\
Personalities : [raid6] [raid5] [raid4]
md126 : active raid5 sde1[3] sdd1[2] sdc1[1]
      20578304 blocks super 1.2 level 5, 512k chunk, algorithm 2 [3/3] [UUU]

md127 : active raid5 sdd2[1] sdc2[0]
      10289152 blocks super 1.2 level 5, 512k chunk, algorithm 2 [2/2] [UU]

unused devices: <none>
"""

#: sdc (a two-tier disk) replaced by sde: both arrays are full again,
#: with sdc1 lingering faulty in t00 and sdc2 evicted from t01.
STALE_MDSTAT_TWO_TIER = """\
Personalities : [raid6] [raid5] [raid4]
md126 : active raid5 sde1[3] sdd1[2] sdc1[1](F) sdb1[0]
      20578304 blocks super 1.2 level 5, 512k chunk, algorithm 2 [3/3] [UUU]

md127 : active raid5 sde2[2] sdd2[1]
      10289152 blocks super 1.2 level 5, 512k chunk, algorithm 2 [2/2] [UU]

unused devices: <none>
"""

#: Older lsblk (string-typed size/rota/ro), padded vendor, a child
#: without "pkname" and a 4-level disk->part->md->lvm chain.
LSBLK_TEXT = """\
{
   "blockdevices": [
      {"name":"sdb", "kname":"sdb", "path":"/dev/sdb", "size":10737418240,
       "type":"disk", "partlabel":null, "parttype":null, "pkname":null,
       "model":"QEMU HARDDISK", "serial":"QM00007", "vendor":"ATA     ",
       "rota":"1", "ro":"0", "tran":"sata", "mountpoint":null,
       "fstype":null,
       "children": [
          {"name":"sdb1", "kname":"sdb1", "path":"/dev/sdb1",
           "size":"10736369664", "type":"part", "partlabel":"oar:tank:t00",
           "parttype":"a19d880f-05fc-4d3b-a006-743f0f84911e",
           "model":null, "serial":null, "vendor":null, "rota":"1",
           "ro":"0", "tran":null, "mountpoint":null,
           "fstype":"linux_raid_member",
           "children": [
              {"name":"md126", "kname":"md126", "path":"/dev/md126",
               "size":10736369664, "type":"raid5", "pkname":"sdb1",
               "rota":false, "ro":false, "mountpoint":null,
               "fstype":"LVM2_member",
               "children": [
                  {"name":"tank-data", "kname":"dm-0", "path":"/dev/dm-0",
                   "size":10736369664, "type":"lvm", "pkname":"md126",
                   "rota":false, "ro":false,
                   "mountpoint":"/srv/dev-disk-by-uuid-0a1b2c3d",
                   "fstype":"btrfs"}
               ]}
           ]}
       ]}
   ]
}
"""

VGS_JSON_TEXT = """\
  {
      "report": [
          {
              "vg": [
                  {"vg_name":"tank", "vg_size":"31809601536",
                   "vg_free":"4966055936", "vg_tags":"omv-oar"},
                  {"vg_name":"ubuntu-vg", "vg_size":"274726256640",
                   "vg_free":"0", "vg_tags":""}
              ]
          }
      ]
  }
"""


# -----------------------------------------------------------------------
# The synthetic 'tank' pool.
# -----------------------------------------------------------------------

def _lsblk_node(name, size, type_, **extra):
    """One lsblk JSON node with every LSBLK_COLUMNS key present."""
    node = {
        "name": name,
        "kname": name,
        "path": "/dev/%s" % name,
        "size": size,
        "type": type_,
        "partlabel": None,
        "parttype": None,
        "pkname": None,
        "model": None,
        "serial": None,
        "vendor": None,
        "rota": False,
        "ro": False,
        "tran": None,
        "mountpoint": None,
        "fstype": None,
    }
    node.update(extra)
    return node


def _tank_lsblk():
    """lsblk tree for the tank pool. As on a live system the md devices
    are repeated under every member partition and the LV under every md,
    and the LV shows up as name 'tank-data' type 'lvm' with a /dev/dm-N
    path (not /dev/mapper/...)."""

    def lv(pk):
        return _lsblk_node(
            "tank-data", LV_SIZE, "lvm", kname="dm-0", path="/dev/dm-0",
            pkname=pk, fstype="btrfs", mountpoint=TANK_MNT,
        )

    def md126(pk):
        return _lsblk_node(
            "md126", 2 * T0_HEIGHT, "raid5", pkname=pk,
            fstype="LVM2_member", children=[lv("md126")],
        )

    def md127(pk):
        return _lsblk_node(
            "md127", T1_HEIGHT, "raid5", pkname=pk,
            fstype="LVM2_member", children=[lv("md127")],
        )

    def part(disk, number, tier, size, md):
        name = "%s%d" % (disk, number)
        return _lsblk_node(
            name, size, "part", pkname=disk,
            partlabel="oar:tank:t%02d" % tier, parttype=PART_GUID,
            fstype="linux_raid_member", children=[md(name)],
        )

    return {
        "blockdevices": [
            _lsblk_node(
                "sda", 64 * GiB, "disk", model="Samsung SSD 870",
                serial="S5Y1NX0R", tran="sata",
                children=[
                    _lsblk_node(
                        "sda1", 64 * GiB - MiB, "part", pkname="sda",
                        fstype="ext4", mountpoint="/",
                    )
                ],
            ),
            _lsblk_node(
                "sdb", 10 * GiB, "disk", model="WDC WD101EFRX",
                serial="WD-1", vendor="ATA     ", rota=True, tran="sata",
                children=[part("sdb", 1, 0, T0_HEIGHT, md126)],
            ),
            _lsblk_node(
                "sdc", 20 * GiB, "disk", model="WDC WD201EFRX",
                serial="WD-2", vendor="ATA     ", rota=True, tran="sata",
                children=[
                    part("sdc", 1, 0, T0_HEIGHT, md126),
                    part("sdc", 2, 1, T1_HEIGHT, md127),
                ],
            ),
            _lsblk_node(
                "sdd", 20 * GiB, "disk", model="WDC WD201EFRX",
                serial="WD-3", vendor="ATA     ", rota=True, tran="sata",
                children=[
                    part("sdd", 1, 0, T0_HEIGHT, md126),
                    part("sdd", 2, 1, T1_HEIGHT, md127),
                ],
            ),
        ]
    }


def _md_attrs(**over):
    attrs = {
        "level": "raid5",
        "array_state": "clean",
        "degraded": "0",
        "sync_action": "idle",
        "sync_completed": "none",
        "consistency_policy": "bitmap",
        "raid_disks": "3",
    }
    attrs.update(over)
    return attrs


def tank_state():
    """Fresh healthy 'tank' snapshot; safe to mutate per test."""
    return SystemState(
        devices=parse_lsblk(_tank_lsblk()),
        mdstat=parse_mdstat(TANK_MDSTAT),
        md_attrs={
            "md126": _md_attrs(raid_disks="3"),
            "md127": _md_attrs(raid_disks="2"),
        },
        vgs=[
            {
                "vg_name": "tank",
                "vg_size": str(VG_SIZE),
                "vg_free": str(VG_FREE),
                "vg_tags": "omv-oar",
            }
        ],
        pvs=[
            {"pv_name": "/dev/md126", "vg_name": "tank",
             "pv_size": str(2 * T0_HEIGHT)},
            {"pv_name": "/dev/md127", "vg_name": "tank",
             "pv_size": str(T1_HEIGHT)},
        ],
        lvs=[
            {"lv_name": "data", "vg_name": "tank",
             "lv_size": str(LV_SIZE), "lv_path": "/dev/tank/data"}
        ],
        mounts={TANK_MNT: {"size": ST_SIZE, "free": ST_FREE}},
    )


def _set_md(state, kname, **over):
    state.md_attrs[kname].update(over)


def _drop_devices(state, *knames):
    state.devices = [d for d in state.devices if d.kname not in knames]


def _stale_state(mdstat_text, replacement_tiers=()):
    """Fresh tank lsblk tree plus a replacement disk 'sde' carrying a
    labeled slice (partlabel oar:tank:t<NN>, assembled into the tier md)
    for every index in ``replacement_tiers``, paired with the given
    /proc/mdstat text. stale_slices only reads devices + mdstat."""
    md_for = {0: ("md126", T0_HEIGHT), 1: ("md127", T1_HEIGHT)}
    data = _tank_lsblk()
    slices = []
    for tier in replacement_tiers:
        md_name, height = md_for[tier]
        pname = "sde%d" % (tier + 1)
        slices.append(
            _lsblk_node(
                pname, height, "part", pkname="sde",
                partlabel="oar:tank:t%02d" % tier, parttype=PART_GUID,
                fstype="linux_raid_member",
                children=[_lsblk_node(md_name, height, "raid5", pkname=pname)],
            )
        )
    data["blockdevices"].append(
        _lsblk_node(
            "sde", 20 * GiB, "disk", model="WDC WD201EFRX",
            serial="WD-4", vendor="ATA     ", rota=True, tran="sata",
            children=slices,
        )
    )
    return SystemState(
        devices=parse_lsblk(data), mdstat=parse_mdstat(mdstat_text)
    )


# -----------------------------------------------------------------------
# Small helpers.
# -----------------------------------------------------------------------

class FormatHelpersTestCase(unittest.TestCase):
    def test_dm_name_doubles_dashes(self):
        self.assertEqual(dm_name("tank", "data"), "tank-data")
        self.assertEqual(dm_name("my-pool", "data"), "my--pool-data")
        self.assertEqual(dm_name("a-b", "c-d"), "a--b-c--d")

    def test_format_binary(self):
        cases = [
            (0, "0 B"),
            (1023, "1023 B"),
            (1024, "1.00 KiB"),
            (1024 ** 3, "1.00 GiB"),
            (1536 * MiB, "1.50 GiB"),
            (20 * GiB, "20.00 GiB"),
            (3 * 1024 ** 4, "3.00 TiB"),
        ]
        for size, expected in cases:
            with self.subTest(size=size):
                self.assertEqual(format_binary(size), expected)


# -----------------------------------------------------------------------
# Parsers.
# -----------------------------------------------------------------------

class ParseMdstatTestCase(unittest.TestCase):
    def test_arrays_discovered_headers_ignored(self):
        arrays = parse_mdstat(MDSTAT_MIXED)
        # Personalities / unused lines never become arrays.
        self.assertEqual(sorted(arrays), ["md126", "md127"])

    def test_active_array_level_and_slots(self):
        md126 = parse_mdstat(MDSTAT_MIXED)["md126"]
        self.assertTrue(md126["active"])
        self.assertEqual(md126["level"], "raid5")
        # [3/2] on the blocks line, not [4KB] from the bitmap line.
        self.assertEqual(md126["slots"], 3)
        self.assertEqual(md126["up"], 2)

    def test_member_flags(self):
        members = parse_mdstat(MDSTAT_MIXED)["md126"]["members"]
        self.assertEqual(
            sorted(members), ["sdb1", "sdc1", "sdd1", "sde1"]
        )
        self.assertEqual(
            members["sdb1"],
            {"faulty": False, "spare": False, "replacement": False},
        )
        self.assertTrue(members["sdc1"]["faulty"])
        self.assertTrue(members["sdd1"]["spare"])
        self.assertTrue(members["sde1"]["replacement"])
        self.assertFalse(members["sdc1"]["spare"])

    def test_inactive_array(self):
        md127 = parse_mdstat(MDSTAT_MIXED)["md127"]
        self.assertFalse(md127["active"])
        self.assertIsNone(md127["level"])
        self.assertIsNone(md127["slots"])
        self.assertTrue(md127["members"]["sdf1"]["spare"])


class ParseSyncCompletedTestCase(unittest.TestCase):
    def test_ratio_table(self):
        cases = [
            ("1234 / 5678", 1234 / 5678),
            ("44040192 / 104857600", 0.42),
            ("none", 0.0),
            ("delayed", 0.0),
            ("", 0.0),
            ("garbage / junk", 0.0),
            ("100 / 0", 0.0),  # never divides by zero
            ("9999 / 100", 1.0),  # clamped
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertAlmostEqual(parse_sync_completed(text), expected)


class ParseLsblkTestCase(unittest.TestCase):
    def test_flattens_nested_tree(self):
        devs = parse_lsblk(LSBLK_TEXT)
        self.assertEqual(
            [d.kname for d in devs], ["sdb", "sdb1", "md126", "dm-0"]
        )

    def test_pkname_inherited_from_parent(self):
        devs = {d.kname: d for d in parse_lsblk(LSBLK_TEXT)}
        # sdb1 carries no "pkname" key in the JSON: parent kname wins.
        self.assertEqual(devs["sdb1"].pkname, "sdb")
        # An explicit pkname is kept as-is.
        self.assertEqual(devs["md126"].pkname, "sdb1")
        self.assertEqual(devs["sdb"].pkname, "")

    def test_type_coercion(self):
        devs = {d.kname: d for d in parse_lsblk(LSBLK_TEXT)}
        self.assertEqual(devs["sdb"].size, 10737418240)  # JSON number
        self.assertEqual(devs["sdb1"].size, 10736369664)  # JSON string
        self.assertIs(devs["sdb"].rota, True)  # "1"
        self.assertIs(devs["sdb"].ro, False)  # "0"
        self.assertIs(devs["md126"].rota, False)  # JSON false
        self.assertEqual(devs["sdb"].vendor, "ATA")  # padding stripped
        self.assertEqual(devs["sdb"].partlabel, "")  # null -> ""
        self.assertEqual(devs["sdb1"].partlabel, "oar:tank:t00")
        self.assertEqual(devs["dm-0"].fstype, "btrfs")

    def test_md_repeated_under_two_parents_deduped(self):
        def md(pk):
            return _lsblk_node("md0", GiB, "raid5", pkname=pk)

        data = {
            "blockdevices": [
                _lsblk_node("sdx", 2 * GiB, "disk", children=[
                    _lsblk_node("sdx1", GiB, "part", pkname="sdx",
                                partlabel="oar:p:t00",
                                children=[md("sdx1")]),
                ]),
                _lsblk_node("sdy", 2 * GiB, "disk", children=[
                    _lsblk_node("sdy1", GiB, "part", pkname="sdy",
                                partlabel="oar:p:t00",
                                children=[md("sdy1")]),
                ]),
            ]
        }
        devs = parse_lsblk(data)
        self.assertEqual(
            [d.kname for d in devs], ["sdx", "sdx1", "md0", "sdy", "sdy1"]
        )
        # First occurrence wins, so the md stays a child of sdx1 --
        # tier discovery relies on exactly one parent edge surviving.
        (md0,) = [d for d in devs if d.kname == "md0"]
        self.assertEqual(md0.pkname, "sdx1")


class ParseLvmReportTestCase(unittest.TestCase):
    def test_extracts_section_rows(self):
        rows = parse_lvm_report(VGS_JSON_TEXT, "vg")
        self.assertEqual(
            [r["vg_name"] for r in rows], ["tank", "ubuntu-vg"]
        )
        self.assertEqual(rows[0]["vg_size"], "31809601536")

    def test_missing_section_is_empty(self):
        self.assertEqual(parse_lvm_report(VGS_JSON_TEXT, "lv"), [])
        self.assertEqual(parse_lvm_report({}, "vg"), [])

    def test_multiple_reports_concatenated(self):
        data = {
            "report": [
                {"pv": [{"pv_name": "/dev/md126"}]},
                {"pv": [{"pv_name": "/dev/md127"}]},
            ]
        }
        self.assertEqual(
            [r["pv_name"] for r in parse_lvm_report(data, "pv")],
            ["/dev/md126", "/dev/md127"],
        )

    def test_vg_tags(self):
        self.assertEqual(
            vg_tags({"vg_tags": "omv-oar"}), ["omv-oar"]
        )
        self.assertEqual(
            vg_tags({"vg_tags": "omv-oar,oar.finalize"}),
            ["omv-oar", "oar.finalize"],
        )
        self.assertEqual(vg_tags({"vg_tags": ""}), [])
        self.assertEqual(vg_tags({}), [])


# -----------------------------------------------------------------------
# Pool discovery.
# -----------------------------------------------------------------------

class PoolNamesTestCase(unittest.TestCase):
    def test_only_tagged_vgs_sorted(self):
        state = SystemState(vgs=[
            {"vg_name": "tank", "vg_tags": "omv-oar"},
            {"vg_name": "ubuntu-vg", "vg_tags": ""},
            {"vg_name": "backup", "vg_tags": "backup-tag"},
            # Tag match is exact, not substring.
            {"vg_name": "impostor", "vg_tags": "omv-oar-old"},
            {"vg_name": "alpha", "vg_tags": "omv-oar,oar.finalize"},
        ])
        self.assertEqual(pool_names(state), ["alpha", "tank"])


class PoolTiersTestCase(unittest.TestCase):
    def test_tank_tiers_discovered(self):
        tiers = pool_tiers(tank_state(), "tank")
        self.assertEqual(sorted(tiers), [0, 1])
        self.assertEqual(
            [p.path for p in tiers[0].partitions],
            ["/dev/sdb1", "/dev/sdc1", "/dev/sdd1"],
        )
        self.assertEqual(
            [p.path for p in tiers[1].partitions],
            ["/dev/sdc2", "/dev/sdd2"],
        )
        # Each tier is attached to its assembled md array.
        self.assertEqual(tiers[0].md.kname, "md126")
        self.assertEqual(tiers[1].md.path, "/dev/md127")

    def test_partitions_sorted_regardless_of_device_order(self):
        state = tank_state()
        state.devices.reverse()
        tiers = pool_tiers(state, "tank")
        self.assertEqual(
            [p.path for p in tiers[0].partitions],
            ["/dev/sdb1", "/dev/sdc1", "/dev/sdd1"],
        )

    def test_foreign_and_malformed_partlabels_ignored(self):
        data = _tank_lsblk()
        data["blockdevices"] += [
            _lsblk_node("sde", 20 * GiB, "disk", children=[
                _lsblk_node("sde1", T0_HEIGHT, "part", pkname="sde",
                            partlabel="oar:other:t00", parttype=PART_GUID),
            ]),
            _lsblk_node("sdf", 20 * GiB, "disk", children=[
                _lsblk_node("sdf1", T0_HEIGHT, "part", pkname="sdf",
                            partlabel="oar:tank:tier0"),  # not t<NN>
            ]),
        ]
        state = SystemState(devices=parse_lsblk(data))
        tiers = pool_tiers(state, "tank")
        self.assertEqual(sorted(tiers), [0, 1])
        for tier in tiers.values():
            for part in tier.partitions:
                self.assertNotIn(part.pkname, ("sde", "sdf"))
        # The foreign slice belongs to its own pool.
        other = pool_tiers(state, "other")
        self.assertEqual(
            [p.path for p in other[0].partitions], ["/dev/sde1"]
        )
        self.assertEqual(
            [d.path for d in pool_disks(state, "tank")],
            ["/dev/sdb", "/dev/sdc", "/dev/sdd"],
        )


class PoolDisksTestCase(unittest.TestCase):
    def test_whole_disks_sorted(self):
        state = tank_state()
        state.devices.reverse()
        disks = pool_disks(state, "tank")
        self.assertEqual(
            [(d.path, d.size, d.type) for d in disks],
            [
                ("/dev/sdb", 10 * GiB, "disk"),
                ("/dev/sdc", 20 * GiB, "disk"),
                ("/dev/sdd", 20 * GiB, "disk"),
            ],
        )


class PoolLayoutTestCase(unittest.TestCase):
    def test_reconstructs_live_layout(self):
        lay = pool_layout(tank_state(), "tank")
        self.assertEqual(
            [(t.index, len(t.members)) for t in lay.tiers],
            [(0, 3), (1, 2)],
        )
        # RAID5 net capacity, and no wasted space for 10/20/20.
        self.assertEqual(lay.usable_capacity, 2 * T0_HEIGHT + T1_HEIGHT)
        self.assertEqual(lay.unallocatable_bytes, 0)


# -----------------------------------------------------------------------
# pool_status.
# -----------------------------------------------------------------------

class PoolStatusShapeTestCase(unittest.TestCase):
    def test_online_status(self):
        status = pool_status(tank_state(), "tank")
        self.assertEqual(
            sorted(status),
            [
                "activity", "allocated", "devicefile", "disks", "free",
                "fstype", "mountpoint", "name", "pending_finalize", "size",
                "state", "tiers", "unallocatable",
            ],
        )
        self.assertEqual(status["name"], "tank")
        # /dev/mapper path is derived even though lsblk only knows the
        # LV as name 'tank-data' with path /dev/dm-0.
        self.assertEqual(status["devicefile"], "/dev/mapper/tank-data")
        self.assertEqual(status["fstype"], "btrfs")
        self.assertEqual(
            status["mountpoint"], "/srv/dev-disk-by-uuid-0a1b2c3d"
        )
        self.assertEqual(status["state"], "online")
        self.assertEqual(status["activity"], "")
        self.assertFalse(status["pending_finalize"])
        # Mounted: statvfs numbers beat the VG numbers.
        self.assertEqual(status["size"], ST_SIZE)
        self.assertEqual(status["free"], ST_FREE)
        self.assertEqual(status["allocated"], ST_SIZE - ST_FREE)
        self.assertEqual(status["unallocatable"], 0)
        self.assertEqual(status["disks"], [
            {"devicefile": "/dev/sdb", "size": 10 * GiB, "state": "online"},
            {"devicefile": "/dev/sdc", "size": 20 * GiB, "state": "online"},
            {"devicefile": "/dev/sdd", "size": 20 * GiB, "state": "online"},
        ])

    def test_online_tiers(self):
        t0, t1 = pool_status(tank_state(), "tank")["tiers"]
        self.assertEqual(
            sorted(t0),
            [
                "consistency_policy", "devicefile", "index", "level",
                "members", "name", "progress", "size", "state",
                "sync_action",
            ],
        )
        self.assertEqual(t0["index"], 0)
        self.assertEqual(t0["name"], "tank-t00")
        self.assertEqual(t0["devicefile"], "/dev/md126")
        self.assertEqual(t0["level"], "raid5")
        self.assertEqual(
            t0["members"], ["/dev/sdb1", "/dev/sdc1", "/dev/sdd1"]
        )
        self.assertEqual(t0["state"], "clean")
        self.assertEqual(t0["sync_action"], "idle")
        self.assertEqual(t0["progress"], 0.0)
        self.assertEqual(t1["index"], 1)
        self.assertEqual(t1["name"], "tank-t01")
        self.assertEqual(t1["devicefile"], "/dev/md127")
        self.assertEqual(t1["members"], ["/dev/sdc2", "/dev/sdd2"])

    def test_unmounted_falls_back_to_vg_numbers(self):
        state = tank_state()
        for dev in state.devices:
            if dev.kname == "dm-0":
                dev.mountpoint = ""
        status = pool_status(state, "tank")
        self.assertEqual(status["mountpoint"], "")
        self.assertEqual(status["size"], VG_SIZE)
        self.assertEqual(status["free"], VG_FREE)
        self.assertEqual(status["allocated"], LV_SIZE)


class PoolStateTestCase(unittest.TestCase):
    """State derivation contract: failed > degraded > rebuilding >
    expanding > checking > online."""

    def test_state_table(self):
        cases = [
            ("all_idle_clean", lambda s: None, "online"),
            (
                "recover_is_rebuilding",
                lambda s: _set_md(
                    s, "md126", sync_action="recover", degraded="1",
                    sync_completed="44040192 / 104857600",
                ),
                "rebuilding",
            ),
            (
                "resync_is_rebuilding",
                lambda s: _set_md(s, "md127", sync_action="resync"),
                "rebuilding",
            ),
            (
                "degraded_idle",
                lambda s: _set_md(s, "md126", degraded="1"),
                "degraded",
            ),
            (
                "degraded_outranks_reshape",
                lambda s: (
                    _set_md(s, "md126", degraded="1"),
                    _set_md(s, "md127", sync_action="reshape"),
                ),
                "degraded",
            ),
            (
                "reshape_is_expanding",
                lambda s: _set_md(
                    s, "md127", sync_action="reshape",
                    sync_completed="44040192 / 104857600",
                ),
                "expanding",
            ),
            (
                "finalize_tag_is_expanding",
                lambda s: s.vgs[0].update(
                    vg_tags="omv-oar,oar.finalize"
                ),
                "expanding",
            ),
            (
                "check_is_checking",
                lambda s: _set_md(
                    s, "md126", sync_action="check",
                    sync_completed="10485760 / 104857600",
                ),
                "checking",
            ),
            (
                "md_missing_is_failed",
                lambda s: _drop_devices(s, "md127"),
                "failed",
            ),
            (
                "inactive_md_is_failed",
                lambda s: s.mdstat["md127"].update(active=False),
                "failed",
            ),
            (
                "lv_missing_is_failed",
                lambda s: s.lvs.clear(),
                "failed",
            ),
            (
                "failed_outranks_rebuilding",
                lambda s: (
                    s.lvs.clear(),
                    _set_md(s, "md126", sync_action="recover",
                            degraded="1"),
                ),
                "failed",
            ),
        ]
        for name, mutate, expected in cases:
            with self.subTest(case=name):
                state = tank_state()
                mutate(state)
                self.assertEqual(
                    pool_status(state, "tank")["state"], expected
                )

    def test_finalize_tag_sets_pending_flag(self):
        state = tank_state()
        state.vgs[0]["vg_tags"] = "omv-oar,oar.finalize"
        status = pool_status(state, "tank")
        self.assertTrue(status["pending_finalize"])
        self.assertFalse(
            pool_status(tank_state(), "tank")["pending_finalize"]
        )


class PoolActivityTestCase(unittest.TestCase):
    def test_idle_everywhere_is_empty(self):
        self.assertEqual(pool_status(tank_state(), "tank")["activity"], "")

    def test_single_operation_with_percent(self):
        state = tank_state()
        _set_md(state, "md127", sync_action="reshape",
                sync_completed="44040192 / 104857600")
        status = pool_status(state, "tank")
        self.assertEqual(status["activity"], "reshape (42%)")
        self.assertEqual(status["tiers"][1]["progress"], 0.42)

    def test_least_complete_operation_wins(self):
        state = tank_state()
        _set_md(state, "md126", sync_action="resync",
                sync_completed="94371840 / 104857600")  # 90%
        _set_md(state, "md127", sync_action="reshape",
                sync_completed="44040192 / 104857600")  # 42%
        self.assertEqual(
            pool_status(state, "tank")["activity"], "reshape (42%)"
        )

    def test_frozen_excluded(self):
        state = tank_state()
        _set_md(state, "md126", sync_action="frozen")
        self.assertEqual(pool_status(state, "tank")["activity"], "")
        # ...even while another array reports real work.
        _set_md(state, "md127", sync_action="check",
                sync_completed="10485760 / 104857600")
        self.assertEqual(
            pool_status(state, "tank")["activity"], "check (10%)"
        )


class PoolStatusDisksTestCase(unittest.TestCase):
    def test_faulty_member_marks_disk_failed(self):
        state = tank_state()
        state.mdstat = parse_mdstat(TANK_MDSTAT_FAULTY)
        _set_md(state, "md126", degraded="1")
        status = pool_status(state, "tank")
        self.assertEqual(
            [(d["devicefile"], d["state"]) for d in status["disks"]],
            [
                ("/dev/sdb", "online"),
                ("/dev/sdc", "failed"),
                ("/dev/sdd", "online"),
            ],
        )
        # Faulty member still occupies its slot: no phantom "missing".
        self.assertEqual(len(status["disks"]), 3)
        self.assertEqual(status["state"], "degraded")

    def test_missing_disk_synthesized(self):
        state = tank_state()
        _drop_devices(state, "sdd", "sdd1", "sdd2")
        state.mdstat = parse_mdstat(TANK_MDSTAT_DISK_GONE)
        _set_md(state, "md126", degraded="1")
        _set_md(state, "md127", degraded="1")
        status = pool_status(state, "tank")
        self.assertEqual(status["disks"], [
            {"devicefile": "/dev/sdb", "size": 10 * GiB, "state": "online"},
            {"devicefile": "/dev/sdc", "size": 20 * GiB, "state": "online"},
            {"devicefile": "missing", "size": 0, "state": "missing"},
        ])
        self.assertEqual(status["state"], "degraded")


# -----------------------------------------------------------------------
# stale_slices: reaping the disk left behind by a hot-replace.
# -----------------------------------------------------------------------

class StaleSlicesTestCase(unittest.TestCase):
    """Contract: a labeled slice is reaped only once its tier array is
    fully redundant again (slots==up) AND the slice is either faulty or
    no longer a member. A still-degraded tier reaps nothing."""

    def test_faulty_old_member_reaped_when_array_full(self):
        # sde replaced sdb; md126 is [3/3] again with sdb1 lingering
        # faulty. Only sdb1 is stale; the live members are left alone.
        state = _stale_state(STALE_MDSTAT_HOT_FAULTY, replacement_tiers=[0])
        stale = stale_slices(state, "tank")
        self.assertEqual([p.path for p in stale], ["/dev/sdb1"])
        self.assertTrue(stale[0].partlabel.startswith("oar:tank:"))

    def test_orphaned_slice_reaped_when_array_full(self):
        # sdb1 carries the label but is not a member of md126 at all.
        state = _stale_state(STALE_MDSTAT_HOT_ORPHAN, replacement_tiers=[0])
        self.assertEqual(
            [p.path for p in stale_slices(state, "tank")], ["/dev/sdb1"]
        )

    def test_degraded_tier_reaps_nothing_despite_faulty_member(self):
        # md126 is [3/2]: sdc1 is faulty but the array is NOT redundant
        # again, so the failed-but-unreplaced slice must be left in place.
        state = tank_state()
        state.mdstat = parse_mdstat(TANK_MDSTAT_FAULTY)
        self.assertEqual(stale_slices(state, "tank"), [])

    def test_healthy_pool_reaps_nothing(self):
        # Every member present, none faulty, both arrays full.
        self.assertEqual(stale_slices(tank_state(), "tank"), [])

    def test_two_tier_disk_reaped_from_both_tiers(self):
        # sdc fed both t00 and t01; after replacement by sde both arrays
        # are full again, so both of sdc's labeled slices are reaped
        # (sdc1 faulty in t00, sdc2 orphaned from t01).
        state = _stale_state(STALE_MDSTAT_TWO_TIER, replacement_tiers=[0, 1])
        stale = stale_slices(state, "tank")
        self.assertEqual(
            sorted(p.path for p in stale), ["/dev/sdc1", "/dev/sdc2"]
        )

# -----------------------------------------------------------------------
# Candidate disks.
# -----------------------------------------------------------------------

class CandidatesTestCase(unittest.TestCase):
    @staticmethod
    def state():
        data = _tank_lsblk()
        data["blockdevices"] += [
            _lsblk_node("sde", 20 * GiB, "disk",
                        model="WDC WD40EFRX-68N", serial="WD-WCC7K1",
                        vendor="ATA     ", rota=True, tran="sata"),
            _lsblk_node("sdf", 20 * GiB, "disk",
                        fstype="linux_raid_member"),  # stale signature
            _lsblk_node("sdg", 20 * GiB, "disk",
                        mountpoint="/mnt/backup"),  # in use
            _lsblk_node("sdh", 20 * GiB, "disk", ro=True),
            _lsblk_node("loop0", 5 * GiB, "loop"),
        ]
        return SystemState(devices=parse_lsblk(data))

    def test_only_unused_whole_disks(self):
        # Everything of tank (partitioned disks, parts, mds, the LV),
        # the mounted/signed/read-only disks and loop devices are out.
        cands = candidates(self.state())
        self.assertEqual([c["devicefile"] for c in cands], ["/dev/sde"])
        self.assertEqual(cands[0]["size"], 20 * GiB)
        self.assertEqual(cands[0]["vendor"], "ATA")
        self.assertEqual(cands[0]["serialnumber"], "WD-WCC7K1")
        self.assertEqual(
            cands[0]["description"],
            "WDC WD40EFRX-68N [/dev/sde, 20.00 GiB]",
        )

    def test_allow_loop_includes_loop_devices(self):
        cands = candidates(self.state(), allow_loop=True)
        self.assertEqual(
            [c["devicefile"] for c in cands],
            ["/dev/loop0", "/dev/sde"],
        )
        # No model/vendor: kname is the description fallback.
        self.assertEqual(
            cands[0]["description"], "loop0 [/dev/loop0, 5.00 GiB]"
        )


if __name__ == "__main__":
    unittest.main()
