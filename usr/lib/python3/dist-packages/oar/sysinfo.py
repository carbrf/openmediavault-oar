# -*- coding: utf-8 -*-
#
# This file is part of OpenMediaVault.
#
# @license   https://www.gnu.org/licenses/gpl.html GPL Version 3
# @author    OpenMediaVault Plugin Developers <plugins@openmediavault.org>
# @copyright Copyright (c) 2026 OpenMediaVault Plugin Developers
#
# OpenMediaVault is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# OpenMediaVault is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with OpenMediaVault. If not, see <https://www.gnu.org/licenses/>.
"""
System introspection for OAR pools.

All parsers accept injected raw text/dicts so they are unit-testable
without a live system; only :func:`collect` touches the machine
(lsblk, /proc/mdstat, /sys/block/*/md/*, vgs/pvs/lvs).
"""
from __future__ import annotations

import glob as _glob
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Union

from . import layout

LSBLK_COLUMNS = (
    "NAME,KNAME,PATH,SIZE,TYPE,PARTLABEL,PARTTYPE,PKNAME,MODEL,SERIAL,"
    "VENDOR,ROTA,RO,TRAN,MOUNTPOINT,FSTYPE"
)

#: partlabel "oar:<pool>:t<NN>"
PARTLABEL_RE = re.compile(r"^oar:([a-zA-Z][a-zA-Z0-9+_.-]{0,31}):t(\d+)$")

POOL_TAG = "omv-oar"
FINALIZE_TAG = "oar.finalize"

MD_ATTRS = (
    "level",
    "array_state",
    "degraded",
    "sync_action",
    "sync_completed",
    "consistency_policy",
    "raid_disks",
)


def _as_int(value: object, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.endswith("B"):  # lvm --units b keeps a 'B' suffix w/o --nosuffix
        text = text[:-1]
    try:
        return int(text)
    except ValueError:
        return default


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes")


def format_binary(size: int) -> str:
    """1073741824 -> '1.00 GiB' (OMV binaryUnit style)."""
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
    value = float(size)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return "%d %s" % (int(value), unit)
            return "%.2f %s" % (value, unit)
        value /= 1024.0
    return "%d B" % size


# -----------------------------------------------------------------------
# lsblk.
# -----------------------------------------------------------------------

@dataclass
class BlockDevice:
    name: str = ""
    kname: str = ""
    path: str = ""
    size: int = 0
    type: str = ""
    partlabel: str = ""
    parttype: str = ""
    pkname: str = ""
    model: str = ""
    serial: str = ""
    vendor: str = ""
    rota: bool = False
    ro: bool = False
    tran: str = ""
    mountpoint: str = ""
    fstype: str = ""


def parse_lsblk(data: Union[str, dict]) -> List[BlockDevice]:
    """Parse ``lsblk -J -b -o <LSBLK_COLUMNS>`` output (text or the
    already-decoded dict) into a flat device list."""
    if isinstance(data, str):
        data = json.loads(data)
    devices: List[BlockDevice] = []

    def _walk(node: dict, parent: Optional[str]) -> None:
        dev = BlockDevice(
            name=str(node.get("name") or ""),
            kname=str(node.get("kname") or ""),
            path=str(node.get("path") or ""),
            size=_as_int(node.get("size")),
            type=str(node.get("type") or ""),
            partlabel=str(node.get("partlabel") or ""),
            parttype=str(node.get("parttype") or ""),
            pkname=str(node.get("pkname") or parent or ""),
            model=str(node.get("model") or "").strip(),
            serial=str(node.get("serial") or "").strip(),
            vendor=str(node.get("vendor") or "").strip(),
            rota=_as_bool(node.get("rota")),
            ro=_as_bool(node.get("ro")),
            tran=str(node.get("tran") or ""),
            mountpoint=str(node.get("mountpoint") or ""),
            fstype=str(node.get("fstype") or ""),
        )
        # lsblk repeats md devices once per member partition; dedupe.
        for seen in devices:
            if seen.kname == dev.kname:
                break
        else:
            devices.append(dev)
        for child in node.get("children", []) or []:
            _walk(child, dev.kname)

    for node in (data or {}).get("blockdevices", []) or []:
        _walk(node, None)
    return devices


# -----------------------------------------------------------------------
# mdstat / md sysfs.
# -----------------------------------------------------------------------

_MDSTAT_DEV_RE = re.compile(r"(?P<dev>[^\s\[]+)\[(?P<role>\d+)\](?P<flags>(?:\([A-Z]\))*)")
_MDSTAT_SLOTS_RE = re.compile(r"\[(?P<slots>\d+)/(?P<up>\d+)\]")


def parse_mdstat(text: str) -> Dict[str, dict]:
    """Parse /proc/mdstat. Returns kname -> {active, level, members,
    slots, up}; members is devname -> {faulty, spare, replacement}."""
    arrays: Dict[str, dict] = {}
    current: Optional[dict] = None
    for line in text.splitlines():
        if line.startswith("Personalities") or line.startswith("unused"):
            current = None
            continue
        if line and not line[0].isspace():
            head, _, rest = line.partition(":")
            name = head.strip()
            if not name.startswith("md"):
                current = None
                continue
            fields = rest.split()
            active = bool(fields) and fields[0] != "inactive"
            level = None
            idx = 1
            if len(fields) > 1 and not _MDSTAT_DEV_RE.match(fields[1]):
                # may be the level or '(read-only)' etc.
                if fields[1].startswith("raid") or fields[1] in (
                    "linear",
                    "multipath",
                ):
                    level = fields[1]
                    idx = 2
            members: Dict[str, dict] = {}
            for m in _MDSTAT_DEV_RE.finditer(" ".join(fields[idx:])):
                flags = m.group("flags") or ""
                members[m.group("dev")] = {
                    "faulty": "(F)" in flags,
                    "spare": "(S)" in flags,
                    "replacement": "(R)" in flags,
                }
            current = {
                "active": active,
                "level": level,
                "members": members,
                "slots": None,
                "up": None,
            }
            arrays[name] = current
        elif current is not None:
            m = _MDSTAT_SLOTS_RE.search(line)
            if m:
                current["slots"] = int(m.group("slots"))
                current["up"] = int(m.group("up"))
    return arrays


def parse_sync_completed(text: str) -> float:
    """'1234 / 5678' -> 0.217...; 'none'/junk -> 0.0."""
    parts = str(text).split("/")
    if len(parts) != 2:
        return 0.0
    try:
        done = float(parts[0].strip())
        total = float(parts[1].strip())
    except ValueError:
        return 0.0
    if total <= 0:
        return 0.0
    return min(done / total, 1.0)


# -----------------------------------------------------------------------
# LVM json reports.
# -----------------------------------------------------------------------

def parse_lvm_report(data: Union[str, dict], section: str) -> List[dict]:
    """Parse ``vgs/pvs/lvs --reportformat json`` output; ``section`` is
    'vg', 'pv' or 'lv'. Returns the row dicts."""
    if isinstance(data, str):
        data = json.loads(data)
    rows: List[dict] = []
    for report in (data or {}).get("report", []) or []:
        rows.extend(report.get(section, []) or [])
    return rows


def vg_tags(row: dict) -> List[str]:
    return [t for t in str(row.get("vg_tags", "")).split(",") if t]


# -----------------------------------------------------------------------
# Aggregated system state.
# -----------------------------------------------------------------------

@dataclass
class SystemState:
    """Snapshot of everything the engine needs. Every field can be
    injected for tests."""
    devices: List[BlockDevice] = field(default_factory=list)
    mdstat: Dict[str, dict] = field(default_factory=dict)
    md_attrs: Dict[str, Dict[str, str]] = field(default_factory=dict)
    vgs: List[dict] = field(default_factory=list)
    pvs: List[dict] = field(default_factory=list)
    lvs: List[dict] = field(default_factory=list)
    mounts: Dict[str, dict] = field(default_factory=dict)  # mnt -> statvfs

    # -- lookups ---------------------------------------------------------

    def device_by_kname(self, kname: str) -> Optional[BlockDevice]:
        for dev in self.devices:
            if dev.kname == kname:
                return dev
        return None

    def device_by_path(self, path: str) -> Optional[BlockDevice]:
        for dev in self.devices:
            if dev.path == path or "/dev/%s" % dev.kname == path:
                return dev
        return None

    def children(self, kname: str) -> List[BlockDevice]:
        return [d for d in self.devices if d.pkname == kname]

    def statvfs(self, mountpoint: str) -> Optional[dict]:
        """Injected via ``mounts`` in tests; live otherwise."""
        if mountpoint in self.mounts:
            return self.mounts[mountpoint]
        try:
            st = os.statvfs(mountpoint)
        except OSError:
            return None
        return {
            "size": st.f_blocks * st.f_frsize,
            "free": st.f_bavail * st.f_frsize,
        }


def _run(argv: Sequence[str]) -> str:
    env = dict(os.environ, LC_ALL="C.UTF-8", LANG="C.UTF-8")
    return subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout


def collect() -> SystemState:
    """Gather live system state."""
    state = SystemState()
    state.devices = parse_lsblk(
        _run(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS])
    )
    try:
        with open("/proc/mdstat", "r", encoding="utf-8") as f:
            state.mdstat = parse_mdstat(f.read())
    except OSError:
        state.mdstat = {}
    for md_dir in sorted(_glob.glob("/sys/block/md*/md")):
        kname = md_dir.split("/")[3]
        attrs: Dict[str, str] = {}
        for attr in MD_ATTRS:
            try:
                with open(os.path.join(md_dir, attr), encoding="utf-8") as f:
                    attrs[attr] = f.read().strip()
            except OSError:
                continue
        state.md_attrs[kname] = attrs
    lvm_common = ["--reportformat", "json", "--units", "b", "--nosuffix"]
    try:
        state.vgs = parse_lvm_report(
            _run(["vgs"] + lvm_common + ["-o", "vg_name,vg_size,vg_free,vg_tags"]),
            "vg",
        )
        state.pvs = parse_lvm_report(
            _run(["pvs"] + lvm_common + ["-o", "pv_name,vg_name,pv_size"]),
            "pv",
        )
        state.lvs = parse_lvm_report(
            _run(["lvs"] + lvm_common + ["-o", "lv_name,vg_name,lv_size,lv_path"]),
            "lv",
        )
    except (OSError, subprocess.CalledProcessError):
        pass
    return state


# -----------------------------------------------------------------------
# Pool model.
# -----------------------------------------------------------------------

def dm_name(vg: str, lv: str) -> str:
    """device-mapper name: dashes in VG/LV names are doubled."""
    return "%s-%s" % (vg.replace("-", "--"), lv.replace("-", "--"))


@dataclass
class TierState:
    index: int
    partitions: List[BlockDevice] = field(default_factory=list)
    md: Optional[BlockDevice] = None  # the md block device, if assembled


def pool_names(state: SystemState) -> List[str]:
    return sorted(
        str(row.get("vg_name", ""))
        for row in state.vgs
        if POOL_TAG in vg_tags(row)
    )


def pool_tiers(state: SystemState, pool: str) -> Dict[int, TierState]:
    """Tier discovery: partlabels 'hr:<pool>:t<NN>' + their md children."""
    tiers: Dict[int, TierState] = {}
    for dev in state.devices:
        m = PARTLABEL_RE.match(dev.partlabel)
        if not m or m.group(1) != pool:
            continue
        index = int(m.group(2))
        tier = tiers.setdefault(index, TierState(index=index))
        tier.partitions.append(dev)
        for child in state.children(dev.kname):
            if child.type.startswith("raid") or child.kname.startswith("md"):
                tier.md = child
    for tier in tiers.values():
        tier.partitions.sort(key=lambda d: d.path)
    return tiers


def pool_disks(state: SystemState, pool: str) -> List[BlockDevice]:
    """Whole disks holding slices of ``pool``."""
    disks: Dict[str, BlockDevice] = {}
    for tier in pool_tiers(state, pool).values():
        for part in tier.partitions:
            parent = state.device_by_kname(part.pkname)
            if parent is not None:
                disks[parent.kname] = parent
    return sorted(disks.values(), key=lambda d: d.path)


def pool_layout(state: SystemState, pool: str) -> layout.Layout:
    """Reconstruct the pure layout of a live pool for planning."""
    tiers = pool_tiers(state, pool)
    disks = pool_disks(state, pool)
    spec = []
    for index, tier in sorted(tiers.items()):
        if not tier.partitions:
            continue
        height = tier.partitions[0].size
        members = []
        for part in tier.partitions:
            parent = state.device_by_kname(part.pkname)
            if parent is not None:
                members.append(parent.path)
        spec.append((index, height, members))
    return layout.reconstruct(
        [layout.Disk(d.path, d.size) for d in disks], spec
    )


def _tier_json(state: SystemState, pool: str, tier: TierState) -> dict:
    md = tier.md
    attrs = state.md_attrs.get(md.kname, {}) if md else {}
    mdstat = state.mdstat.get(md.kname, {}) if md else {}
    sync_action = attrs.get("sync_action", "idle")
    progress = parse_sync_completed(attrs.get("sync_completed", "none"))
    array_state = attrs.get("array_state", "inactive" if md is None else "")
    if md is None:
        array_state = "missing"
    elif mdstat and not mdstat.get("active", True):
        array_state = "inactive"
    return {
        "index": tier.index,
        "name": layout.tier_name(pool, tier.index),
        "devicefile": md.path or "/dev/%s" % md.kname if md else "",
        "level": attrs.get("level") or mdstat.get("level") or layout.RAID_LEVEL,
        "members": [p.path for p in tier.partitions],
        "size": md.size if md else 0,
        "state": array_state,
        "sync_action": sync_action,
        "progress": round(progress, 4),
        "consistency_policy": attrs.get("consistency_policy", ""),
        "degraded": _as_int(attrs.get("degraded")),
    }


def _pool_state(
    tiers: List[dict], pending_finalize: bool, lv_missing: bool
) -> str:
    """Contract: failed > degraded > rebuilding > expanding > checking
    > online."""
    if lv_missing or any(
        t["state"] in ("missing", "inactive", "clear", "suspended")
        for t in tiers
    ):
        return "failed"
    if any(t["degraded"] > 0 and t["sync_action"] not in ("recover", "resync")
           for t in tiers):
        return "degraded"
    if any(t["sync_action"] in ("recover", "resync") for t in tiers):
        return "rebuilding"
    if pending_finalize or any(
        t["sync_action"] == "reshape" for t in tiers
    ):
        return "expanding"
    if any(t["sync_action"] == "check" for t in tiers):
        return "checking"
    return "online"


def _pool_activity(tiers: List[dict]) -> str:
    """Human-readable summary of the busiest running sync operation,
    e.g. 'reshape (42%)'. Empty string when every tier is idle."""
    active = [
        (t["sync_action"], t["progress"])
        for t in tiers
        if t["sync_action"] not in ("idle", "frozen")
    ]
    if not active:
        return ""
    action, progress = min(active, key=lambda a: a[1])
    return "%s (%d%%)" % (action, int(progress * 100))


def pool_status(state: SystemState, pool: str) -> dict:
    """One pool status JSON object (contract shape)."""
    vg_row = next(
        (r for r in state.vgs if str(r.get("vg_name")) == pool), None
    )
    tags = vg_tags(vg_row) if vg_row else []
    lv_row = next(
        (
            r
            for r in state.lvs
            if str(r.get("vg_name")) == pool
            and str(r.get("lv_name")) == "data"
        ),
        None,
    )
    tiers_state = pool_tiers(state, pool)
    tiers = [
        _tier_json(state, pool, t) for _, t in sorted(tiers_state.items())
    ]

    devicefile = "/dev/mapper/%s" % dm_name(pool, "data")
    lv_dev = state.device_by_path(devicefile)
    if lv_dev is None:
        lv_dev = next(
            (
                d
                for d in state.devices
                if d.type == "lvm" and d.name == dm_name(pool, "data")
            ),
            None,
        )
    fstype = lv_dev.fstype if lv_dev else ""
    mountpoint = lv_dev.mountpoint if lv_dev else ""

    size = _as_int(vg_row.get("vg_size")) if vg_row else 0
    free = _as_int(vg_row.get("vg_free")) if vg_row else 0
    allocated = _as_int(lv_row.get("lv_size")) if lv_row else 0
    if mountpoint:
        st = state.statvfs(mountpoint)
        if st:
            size = st["size"]
            free = st["free"]
            allocated = size - free

    # Disk states from mdstat member flags.
    faulty_parts = set()
    missing = 0
    for tier in tiers_state.values():
        if tier.md is None:
            continue
        entry = state.mdstat.get(tier.md.kname, {})
        for devname, flags in entry.get("members", {}).items():
            if flags.get("faulty"):
                faulty_parts.add(devname)
        slots = entry.get("slots")
        if slots:
            present = sum(
                1
                for devname, flags in entry.get("members", {}).items()
                if not flags.get("spare")
            )
            missing = max(missing, slots - present)
    disks = []
    for disk in pool_disks(state, pool):
        parts = [d.kname for d in state.children(disk.kname)]
        disk_state = (
            "failed"
            if any(p in faulty_parts for p in parts)
            else "online"
        )
        disks.append(
            {"devicefile": disk.path, "size": disk.size, "state": disk_state}
        )
    for _ in range(missing):
        disks.append({"devicefile": "missing", "size": 0, "state": "missing"})

    try:
        unallocatable = pool_layout(state, pool).unallocatable_bytes
    except layout.LayoutError:
        unallocatable = 0

    pending = FINALIZE_TAG in tags
    status = {
        "name": pool,
        "devicefile": devicefile,
        "fstype": fstype,
        "state": _pool_state(tiers, pending, lv_row is None),
        "size": size,
        "allocated": allocated,
        "free": free,
        "unallocatable": unallocatable,
        "pending_finalize": pending,
        "activity": _pool_activity(tiers),
        "disks": disks,
        "tiers": [
            {k: v for k, v in t.items() if k != "degraded"} for t in tiers
        ],
    }
    return status


def all_pool_status(state: SystemState) -> List[dict]:
    return [pool_status(state, name) for name in pool_names(state)]


# -----------------------------------------------------------------------
# Candidate disks.
# -----------------------------------------------------------------------

def candidates(state: SystemState, allow_loop: bool = False) -> List[dict]:
    """Unused whole disks suitable for a pool: exclude mounted,
    partitioned, signature-holding, read-only and loop (unless
    ``allow_loop``) devices."""
    out = []
    for dev in state.devices:
        if dev.type == "loop":
            if not allow_loop:
                continue
        elif dev.type != "disk":
            continue
        if dev.ro or dev.size <= 0:
            continue
        if dev.mountpoint or dev.fstype:
            continue
        if state.children(dev.kname):
            continue
        description = "%s [%s, %s]" % (
            dev.model or dev.vendor or dev.kname,
            dev.path,
            format_binary(dev.size),
        )
        out.append(
            {
                "devicefile": dev.path,
                "size": dev.size,
                "vendor": dev.vendor,
                "serialnumber": dev.serial,
                "description": description.strip(),
            }
        )
    return sorted(out, key=lambda c: c["devicefile"])
