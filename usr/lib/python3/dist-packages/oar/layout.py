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
"""
Pure tier layout planning for OAR pools. NO I/O in this module.

A pool is built from whole disks. Each disk is carved into GPT partition
"slices" which stack contiguously from START_OFFSET. Slices of equal
height across >= 2 disks form a "tier", backed by one mdadm RAID5 array
(2-member RAID5 is valid and can grow without a level change). Slices
that would have only a single member are never created; their space is
reported as "unallocatable".

All planning functions return :class:`Plan` objects carrying exact argv
lists (as :class:`Step`) plus the resulting :class:`Layout`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

MiB: int = 1024 * 1024

#: First slice starts here (classic 1 MiB alignment gap for GPT metadata).
START_OFFSET: int = 1 * MiB
#: Kept free at the disk end so a "same size" replacement drive that is
#: a few sectors smaller still fits.
END_RESERVE: int = 128 * MiB
#: All slice heights are multiples of this.
ALIGN: int = 64 * MiB

#: Logical sector size assumed when rendering sgdisk sector arguments.
SECTOR_SIZE: int = 512

#: GPT partition type GUID for OAR slices.
PART_TYPE_GUID: str = "A19D880F-05FC-4D3B-A006-743F0F84911E"

#: Pool name = VG name. No colon: partlabel parsing relies on it.
POOL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+_.-]{0,31}$")

RAID_LEVEL: str = "raid5"


class LayoutError(ValueError):
    """Raised for invalid layout requests (validation errors)."""


# -----------------------------------------------------------------------
# Data model.
# -----------------------------------------------------------------------

@dataclass(frozen=True)
class Disk:
    """A whole disk owned by the pool."""
    name: str  # device path, e.g. /dev/sdb
    size: int  # bytes

    @property
    def usable(self) -> int:
        return usable(self.size)


@dataclass(frozen=True)
class Partition:
    """A slice on a disk. ``start``/``end`` are absolute byte offsets on
    the disk (end exclusive). ``tier`` is the owning tier index, or -1
    for an unallocatable (never created) span."""
    disk: str
    start: int
    end: int
    tier: int

    @property
    def size(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Tier:
    """One RAID5 array spanning equal-height slices on >= 2 disks."""
    index: int
    level: str
    members: Tuple[Partition, ...]

    @property
    def height(self) -> int:
        return self.members[0].size

    @property
    def capacity(self) -> int:
        """Net capacity contributed by this tier (single parity)."""
        return (len(self.members) - 1) * self.height


@dataclass(frozen=True)
class Layout:
    """Complete slice layout of a pool."""
    disks: Tuple[Disk, ...]
    tiers: Tuple[Tier, ...]
    unallocatable: Tuple[Partition, ...]

    @property
    def usable_capacity(self) -> int:
        return sum(t.capacity for t in self.tiers)

    @property
    def unallocatable_bytes(self) -> int:
        return sum(p.size for p in self.unallocatable)

    def disk(self, name: str) -> Disk:
        for d in self.disks:
            if d.name == name:
                return d
        raise LayoutError("unknown disk: %s" % name)

    def allocated_top(self, disk: str) -> int:
        """Sum of slice heights on ``disk`` (offset of first free byte
        in the disk's usable space)."""
        return sum(
            p.size for t in self.tiers for p in t.members if p.disk == disk
        )

    def partition_count(self, disk: str) -> int:
        return sum(
            1 for t in self.tiers for p in t.members if p.disk == disk
        )


@dataclass(frozen=True)
class Step:
    """One executable command of a plan.

    ``fallback_argv`` is tried when ``argv`` fails (e.g. mdadm --create
    with --consistency-policy=ppl falls back to bitmap). ``check`` False
    makes a failure non-fatal.
    """
    argv: Tuple[str, ...]
    description: str = ""
    check: bool = True
    fallback_argv: Optional[Tuple[str, ...]] = None


@dataclass(frozen=True)
class Plan:
    steps: Tuple[Step, ...]
    layout: Layout


# -----------------------------------------------------------------------
# Core math.
# -----------------------------------------------------------------------

def usable(size: int) -> int:
    """Usable bytes of a disk of ``size`` bytes: the aligned space
    between START_OFFSET and the end reserve."""
    avail = size - START_OFFSET - END_RESERVE
    if avail <= 0:
        return 0
    return (avail // ALIGN) * ALIGN


def validate_pool_name(name: str) -> None:
    if not POOL_NAME_RE.match(name):
        raise LayoutError(
            "invalid pool name %r (expected %s)" % (name, POOL_NAME_RE.pattern)
        )


def partition_device(disk: str, number: int) -> str:
    """Partition device path: /dev/sdb -> /dev/sdb1, /dev/nvme0n1 ->
    /dev/nvme0n1p1, /dev/loop0 -> /dev/loop0p1."""
    sep = "p" if disk and disk[-1].isdigit() else ""
    return "%s%s%d" % (disk, sep, number)


def tier_name(pool: str, index: int) -> str:
    return "%s-t%02d" % (pool, index)


def md_device(pool: str, index: int) -> str:
    return "/dev/md/%s" % tier_name(pool, index)


def partlabel(pool: str, index: int) -> str:
    """GPT partition label. NOTE: uses ``@``, not ``:``, as the field
    separator. ``sgdisk --change-name=<num>:<name>`` splits its
    argument on EVERY colon, not just the first (partnum-separating)
    one -- ``--change-name=1:oar:pool:t00`` silently truncates the
    on-disk label to just ``oar``. Verified with GPT fdisk 1.0.9
    (Debian 12/bookworm). ``@`` (and every other character tested)
    survives sgdisk and round-trips correctly through blkid/lsblk,
    including live relabeling of an in-use RAID member partition."""
    return "oar@%s@t%02d" % (pool, index)


def _tier_spans(heights: Dict[str, int]) -> List[Tuple[int, List[str]]]:
    """Boundary algorithm over per-disk free heights.

    Sorted unique positive heights b1 < ... < bk define spans of height
    b(i) - b(i-1); span i is carried by every disk whose free height is
    >= b(i). Returns [(span_height, member_disk_names)] with members
    sorted by name. Positions are per-disk (each disk stacks its slices
    from its own cursor), so only heights matter.
    """
    boundaries = sorted({h for h in heights.values() if h > 0})
    spans: List[Tuple[int, List[str]]] = []
    prev = 0
    for b in boundaries:
        members = sorted(d for d, h in heights.items() if h >= b)
        spans.append((b - prev, members))
        prev = b
    return spans


def _build_layout(
    disks: Sequence[Disk],
    existing: Optional[Layout],
    heights: Dict[str, int],
    first_index: int,
) -> Tuple[Layout, List[Tier]]:
    """Extend ``existing`` (or nothing) with tiers computed from the
    per-disk free ``heights``. Returns (new_layout, new_tiers)."""
    disk_map = {d.name: d for d in disks}
    cursors: Dict[str, int] = {
        d.name: START_OFFSET
        + (existing.allocated_top(d.name) if existing else 0)
        for d in disks
    }
    tiers: List[Tier] = list(existing.tiers) if existing else []
    new_tiers: List[Tier] = []
    index = first_index
    for height, members in _tier_spans(heights):
        parts = []
        tier_idx = index if len(members) >= 2 else -1
        for name in members:
            start = cursors[name]
            parts.append(Partition(name, start, start + height, tier_idx))
        if len(members) >= 2:
            tier = Tier(index=index, level=RAID_LEVEL, members=tuple(parts))
            tiers.append(tier)
            new_tiers.append(tier)
            for name in members:
                cursors[name] += height
            index += 1
        # Single-member spans stay virtual: no partition is created and
        # the cursor does not advance, so a later grow can still use the
        # space.
    unallocatable = tuple(
        Partition(
            d.name,
            cursors[d.name],
            START_OFFSET + d.usable,
            -1,
        )
        for d in sorted(disk_map.values(), key=lambda d: d.name)
        if START_OFFSET + d.usable > cursors[d.name]
    )
    layout = Layout(
        disks=tuple(sorted(disks, key=lambda d: d.name)),
        tiers=tuple(tiers),
        unallocatable=unallocatable,
    )
    return layout, new_tiers


def compute_tiers(disks: Sequence[Disk]) -> Layout:
    """CREATE layout: run the boundary algorithm over the full usable
    height of every disk."""
    _check_disks(disks)
    heights = {d.name: d.usable for d in disks}
    layout, _ = _build_layout(disks, None, heights, first_index=0)
    return layout


def preview(disks: Sequence[Disk]) -> dict:
    """JSON-shaped capacity preview for a prospective pool."""
    layout = compute_tiers(disks)
    return {
        "usable": layout.usable_capacity,
        "unallocatable": layout.unallocatable_bytes,
        "tiers": [
            {
                "index": t.index,
                "height": t.height,
                "members": len(t.members),
                "level": t.level,
            }
            for t in layout.tiers
        ],
    }


def _check_disks(disks: Sequence[Disk]) -> None:
    if not disks:
        raise LayoutError("no disks given")
    names = [d.name for d in disks]
    if len(set(names)) != len(names):
        raise LayoutError("duplicate disk names")
    for d in disks:
        if d.usable <= 0:
            raise LayoutError(
                "disk %s is too small (%d bytes)" % (d.name, d.size)
            )


# -----------------------------------------------------------------------
# Step rendering helpers.
# -----------------------------------------------------------------------

def _wipe_steps(disk: str) -> List[Step]:
    return [
        Step(("sgdisk", "--zap-all", disk), "wipe partition table on %s" % disk),
        Step(("wipefs", "-a", disk), "wipe signatures on %s" % disk),
    ]


def _sgdisk_step(pool: str, disk: str, number: int, part: Partition) -> Step:
    first = part.start // SECTOR_SIZE
    last = part.end // SECTOR_SIZE - 1
    label = partlabel(pool, part.tier)
    return Step(
        (
            "sgdisk",
            "--new=%d:%d:%d" % (number, first, last),
            "--change-name=%d:%s" % (number, label),
            "--typecode=%d:%s" % (number, PART_TYPE_GUID),
            disk,
        ),
        "create slice %s on %s" % (label, disk),
    )


def _mdadm_create_step(pool: str, tier: Tier, devices: Sequence[str]) -> Step:
    name = tier_name(pool, tier.index)
    argv = (
        "mdadm",
        "--create",
        md_device(pool, tier.index),
        "--run",
        "--force",
        "--level=5",
        "--metadata=1.2",
        "--name=%s" % name,
        "--consistency-policy=ppl",
        "--raid-devices=%d" % len(devices),
    ) + tuple(devices)
    fallback = tuple(
        a.replace("--consistency-policy=ppl", "--consistency-policy=bitmap")
        for a in argv
    )
    return Step(
        argv,
        "create RAID5 array %s" % name,
        fallback_argv=fallback,
    )


def _partition_numbers(
    layout_before: Optional[Layout], parts_by_disk: Dict[str, List[Partition]]
) -> Dict[Tuple[str, int], int]:
    """Assign partition numbers for the new partitions of each disk,
    continuing after the disk's existing partition count. Key:
    (disk, tier index)."""
    numbers: Dict[Tuple[str, int], int] = {}
    for disk, parts in parts_by_disk.items():
        base = layout_before.partition_count(disk) if layout_before else 0
        for i, part in enumerate(sorted(parts, key=lambda p: p.start)):
            numbers[(disk, part.tier)] = base + i + 1
    return numbers


def _member_devices(
    tier: Tier, numbers: Dict[Tuple[str, int], int]
) -> List[str]:
    return [
        partition_device(p.disk, numbers[(p.disk, p.tier)])
        for p in tier.members
    ]


# -----------------------------------------------------------------------
# Plans.
# -----------------------------------------------------------------------

def plan_create(pool: str, disks: Sequence[Disk], fs: str = "btrfs") -> Plan:
    """Full pipeline: wipe, partition, mdadm --create per tier, pvcreate,
    vgcreate, lvcreate, mkfs."""
    validate_pool_name(pool)
    if fs not in ("btrfs", "ext4", "xfs", "jfs"):
        raise LayoutError("unsupported filesystem: %s" % fs)
    if len(disks) < 2:
        raise LayoutError("at least two disks are required")
    layout = compute_tiers(disks)
    if not layout.tiers:
        raise LayoutError("disks are too dissimilar: no RAID tier possible")

    parts_by_disk: Dict[str, List[Partition]] = {}
    for tier in layout.tiers:
        for p in tier.members:
            parts_by_disk.setdefault(p.disk, []).append(p)
    numbers = _partition_numbers(None, parts_by_disk)

    steps: List[Step] = []
    ordered_disks = sorted(d.name for d in disks)
    for disk in ordered_disks:
        steps.extend(_wipe_steps(disk))
    for disk in ordered_disks:
        for part in sorted(parts_by_disk.get(disk, []), key=lambda p: p.start):
            steps.append(
                _sgdisk_step(pool, disk, numbers[(disk, part.tier)], part)
            )
    steps.append(Step(("udevadm", "settle"), "settle device nodes"))

    md_devs: List[str] = []
    for tier in layout.tiers:
        devices = _member_devices(tier, numbers)
        steps.append(_mdadm_create_step(pool, tier, devices))
        md_devs.append(md_device(pool, tier.index))
    for md in md_devs:
        steps.append(Step(("pvcreate", md), "create PV on %s" % md))
    steps.append(
        Step(
            ("vgcreate", "--addtag", "omv-oar", pool) + tuple(md_devs),
            "create VG %s" % pool,
        )
    )
    steps.append(
        Step(
            ("lvcreate", "-n", "data", "-l", "100%FREE", pool),
            "create data LV",
        )
    )
    lv_dev = "/dev/%s/data" % pool
    mkfs_cmd = {
        "btrfs": ("mkfs.btrfs", "-L", pool, "-d", "single", "-m", "dup", lv_dev),
        "ext4": ("mkfs.ext4", "-L", pool, lv_dev),
        "xfs": ("mkfs.xfs", "-f", "-L", pool, lv_dev),
        "jfs": ("mkfs.jfs", "-q", "-L", pool, lv_dev),
    }[fs]
    steps.append(Step(mkfs_cmd, "create %s filesystem" % fs))
    return Plan(steps=tuple(steps), layout=layout)


def plan_grow(pool: str, layout: Layout, new_disks: Sequence[Disk]) -> Plan:
    """GROW: existing arrays/partitions are immutable. Each new disk
    joins every existing tier (in index order) whose full span still
    fits in its remaining usable space. The free-space pool (new disks'
    remainders + existing disks' unallocated tops) then forms NEW tiers
    where >= 2 disks overlap; the rest stays unallocatable.

    PPL dance: each joined array is switched to bitmap consistency
    before --add/--grow (the kernel refuses reshape while PPL is
    active); ``finalize`` restores PPL afterwards.
    """
    validate_pool_name(pool)
    if not new_disks:
        raise LayoutError("no disks given")
    existing_names = {d.name for d in layout.disks}
    for d in new_disks:
        if d.name in existing_names:
            raise LayoutError("disk %s is already a pool member" % d.name)
    _check_disks(new_disks)

    # 1. Join existing tiers (greedy, tier index order).
    joined: Dict[int, List[Disk]] = {}  # tier index -> new member disks
    for d in sorted(new_disks, key=lambda d: d.name):
        cursor = 0
        for tier in layout.tiers:
            if cursor + tier.height <= d.usable:
                joined.setdefault(tier.index, []).append(d)
                cursor += tier.height

    # Layout with the joined slices merged in (still the same tiers).
    tiers_joined: List[Tier] = []
    join_parts: Dict[str, List[Partition]] = {}  # new-disk slices
    for tier in layout.tiers:
        members = list(tier.members)
        for d in joined.get(tier.index, []):
            start = START_OFFSET + sum(
                p.size for p in join_parts.get(d.name, [])
            )
            part = Partition(d.name, start, start + tier.height, tier.index)
            join_parts.setdefault(d.name, []).append(part)
            members.append(part)
        tiers_joined.append(
            Tier(tier.index, tier.level, tuple(members))
        )
    all_disks = list(layout.disks) + list(new_disks)
    interim = Layout(
        disks=tuple(sorted(all_disks, key=lambda d: d.name)),
        tiers=tuple(tiers_joined),
        unallocatable=(),
    )

    # 2. New tiers from the free-space pool.
    free_heights = {
        d.name: d.usable - interim.allocated_top(d.name) for d in all_disks
    }
    first_index = max((t.index for t in layout.tiers), default=-1) + 1
    result, new_tiers = _build_layout(
        all_disks, interim, free_heights, first_index
    )

    contributes = set(join_parts)
    for tier in new_tiers:
        contributes.update(p.disk for p in tier.members)
    if not contributes & {d.name for d in new_disks}:
        raise LayoutError(
            "none of the given disks can contribute space to pool %s" % pool
        )

    # Disks that neither join a tier nor carry a new tier stay out of
    # the pool entirely.
    idle = {d.name for d in new_disks} - contributes
    if idle:
        keep = [d for d in all_disks if d.name not in idle]
        result = Layout(
            disks=tuple(sorted(keep, key=lambda d: d.name)),
            tiers=result.tiers,
            unallocatable=tuple(
                p for p in result.unallocatable if p.disk not in idle
            ),
        )

    # 3. Render steps.
    new_parts: Dict[str, List[Partition]] = {}
    for d, parts in join_parts.items():
        new_parts.setdefault(d, []).extend(parts)
    for tier in new_tiers:
        for p in tier.members:
            new_parts.setdefault(p.disk, []).append(p)
    numbers = _partition_numbers(layout, new_parts)

    steps: List[Step] = []
    fresh = sorted(d.name for d in new_disks if d.name in contributes)
    for disk in fresh:
        steps.extend(_wipe_steps(disk))
    for disk in sorted(new_parts):
        for part in sorted(new_parts[disk], key=lambda p: p.start):
            steps.append(
                _sgdisk_step(pool, disk, numbers[(disk, part.tier)], part)
            )
    steps.append(Step(("udevadm", "settle"), "settle device nodes"))

    for tier in layout.tiers:
        added = joined.get(tier.index, [])
        if not added:
            continue
        md = md_device(pool, tier.index)
        devices = [
            partition_device(d.name, numbers[(d.name, tier.index)])
            for d in added
        ]
        steps.append(
            Step(
                ("mdadm", "--grow", md, "--consistency-policy=bitmap"),
                "switch %s to bitmap before reshape (PPL blocks reshape)"
                % md,
            )
        )
        steps.append(
            Step(("mdadm", "--add", md) + tuple(devices), "add members to %s" % md)
        )
        steps.append(
            Step(
                (
                    "mdadm",
                    "--grow",
                    md,
                    "--raid-devices=%d" % (len(tier.members) + len(added)),
                ),
                "grow %s" % md,
            )
        )

    new_pvs: List[str] = []
    for tier in new_tiers:
        devices = _member_devices(tier, numbers)
        steps.append(_mdadm_create_step(pool, tier, devices))
        new_pvs.append(md_device(pool, tier.index))
    for md in new_pvs:
        steps.append(Step(("pvcreate", md), "create PV on %s" % md))
    if new_pvs:
        steps.append(
            Step(("vgextend", pool) + tuple(new_pvs), "extend VG %s" % pool)
        )
    steps.append(
        Step(
            ("vgchange", "--addtag", "oar.finalize", pool),
            "mark pool pending finalize (reboot-safe)",
        )
    )
    steps.append(
        Step(
            (
                "systemctl",
                "start",
                "omv-oar-finalize@%s.service" % pool,
                "--no-block",
            ),
            "kick off background finalize",
            check=False,
        )
    )
    return Plan(steps=tuple(steps), layout=result)


def plan_replace(
    pool: str,
    layout: Layout,
    old_disk: str,
    new_disk: Disk,
    old_alive: bool = True,
) -> Plan:
    """REPLACE: mirror the old disk's tier slices onto the new disk,
    then per tier hot-replace (``mdadm --replace --with``, redundancy
    kept) when the old disk is alive, else ``--add`` to the degraded
    array. A larger new disk's remainder is handled like grow free
    space."""
    validate_pool_name(pool)
    layout.disk(old_disk)  # raises LayoutError if unknown
    if new_disk.name in {d.name for d in layout.disks}:
        raise LayoutError("disk %s is already a pool member" % new_disk.name)
    old_top = layout.allocated_top(old_disk)
    if old_top <= 0:
        raise LayoutError("disk %s holds no tier slices" % old_disk)
    if new_disk.usable < old_top:
        raise LayoutError(
            "replacement disk %s is too small: %d usable bytes < %d "
            "partitioned bytes on %s"
            % (new_disk.name, new_disk.usable, old_top, old_disk)
        )

    # Old disk's slices, bottom-up, mirrored onto the new disk.
    old_parts = sorted(
        (p for t in layout.tiers for p in t.members if p.disk == old_disk),
        key=lambda p: p.start,
    )
    mirror: List[Partition] = []
    cursor = START_OFFSET
    for p in old_parts:
        mirror.append(Partition(new_disk.name, cursor, cursor + p.size, p.tier))
        cursor += p.size

    steps: List[Step] = list(_wipe_steps(new_disk.name))
    numbers: Dict[Tuple[str, int], int] = {}
    for i, part in enumerate(mirror):
        numbers[(new_disk.name, part.tier)] = i + 1
        steps.append(
            _sgdisk_step(pool, new_disk.name, i + 1, part)
        )
    steps.append(Step(("udevadm", "settle"), "settle device nodes"))

    old_numbers: Dict[int, int] = {
        p.tier: i + 1 for i, p in enumerate(old_parts)
    }
    for part in mirror:
        md = md_device(pool, part.tier)
        new_dev = partition_device(
            new_disk.name, numbers[(new_disk.name, part.tier)]
        )
        if old_alive:
            old_dev = partition_device(old_disk, old_numbers[part.tier])
            steps.append(
                Step(("mdadm", "--add", md, new_dev), "add spare to %s" % md)
            )
            steps.append(
                Step(
                    ("mdadm", md, "--replace", old_dev, "--with", new_dev),
                    "hot-replace %s with %s" % (old_dev, new_dev),
                )
            )
        else:
            steps.append(
                Step(
                    ("mdadm", "--add", md, new_dev),
                    "add %s to degraded %s" % (new_dev, md),
                )
            )

    # Build the resulting layout: old disk's slices swapped for the
    # mirrored ones.
    disks = [d for d in layout.disks if d.name != old_disk] + [new_disk]
    swapped: List[Tier] = []
    for tier in layout.tiers:
        members = tuple(
            next(m for m in mirror if m.tier == tier.index)
            if p.disk == old_disk
            else p
            for p in tier.members
        )
        swapped.append(Tier(tier.index, tier.level, members))
    interim = Layout(
        disks=tuple(sorted(disks, key=lambda d: d.name)),
        tiers=tuple(swapped),
        unallocatable=(),
    )

    # Remainder of a larger new disk + existing unallocated tops:
    # same free-span handling as grow.
    free_heights = {
        d.name: d.usable - interim.allocated_top(d.name) for d in disks
    }
    first_index = max((t.index for t in layout.tiers), default=-1) + 1
    result, new_tiers = _build_layout(disks, interim, free_heights, first_index)

    if new_tiers:
        extra_parts: Dict[str, List[Partition]] = {}
        for tier in new_tiers:
            for p in tier.members:
                extra_parts.setdefault(p.disk, []).append(p)
        extra_numbers = _partition_numbers(interim, extra_parts)
        numbers.update(extra_numbers)
        for disk in sorted(extra_parts):
            for part in sorted(extra_parts[disk], key=lambda p: p.start):
                steps.append(
                    _sgdisk_step(
                        pool, disk, extra_numbers[(disk, part.tier)], part
                    )
                )
        steps.append(Step(("udevadm", "settle"), "settle device nodes"))
        new_pvs = []
        for tier in new_tiers:
            devices = _member_devices(tier, extra_numbers)
            steps.append(_mdadm_create_step(pool, tier, devices))
            new_pvs.append(md_device(pool, tier.index))
        for md in new_pvs:
            steps.append(Step(("pvcreate", md), "create PV on %s" % md))
        steps.append(
            Step(("vgextend", pool) + tuple(new_pvs), "extend VG %s" % pool)
        )
    steps.append(
        Step(
            ("vgchange", "--addtag", "oar.finalize", pool),
            "mark pool pending finalize (reap the replaced disk, restore "
            "redundancy and grow if the new disk is larger)",
        )
    )
    steps.append(
        Step(
            (
                "systemctl",
                "start",
                "omv-oar-finalize@%s.service" % pool,
                "--no-block",
            ),
            "kick off background finalize",
            check=False,
        )
    )
    return Plan(steps=tuple(steps), layout=result)


def reconstruct(
    disks: Sequence[Disk],
    tiers: Sequence[Tuple[int, int, Sequence[str]]],
) -> Layout:
    """Rebuild a :class:`Layout` from introspected state.

    ``tiers`` is a sequence of (index, height_bytes, member_disk_names).
    Slices stack per disk in ascending tier index order (the invariant
    maintained by create/grow/replace)."""
    disk_map = {d.name: d for d in disks}
    cursors = {d.name: START_OFFSET for d in disks}
    out: List[Tier] = []
    for index, height, members in sorted(tiers, key=lambda t: t[0]):
        parts = []
        for name in sorted(members):
            if name not in disk_map:
                raise LayoutError("tier %d references unknown disk %s"
                                  % (index, name))
            start = cursors[name]
            parts.append(Partition(name, start, start + height, index))
            cursors[name] += height
        out.append(Tier(index=index, level=RAID_LEVEL, members=tuple(parts)))
    unallocatable = tuple(
        Partition(d.name, cursors[d.name], START_OFFSET + d.usable, -1)
        for d in sorted(disks, key=lambda d: d.name)
        if START_OFFSET + d.usable > cursors[d.name]
    )
    return Layout(
        disks=tuple(sorted(disks, key=lambda d: d.name)),
        tiers=tuple(out),
        unallocatable=unallocatable,
    )
