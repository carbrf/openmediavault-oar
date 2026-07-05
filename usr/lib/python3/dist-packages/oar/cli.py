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
omv-oar command line interface.

Exit codes: 0 ok; 1 operational failure (message on stderr);
2 usage/validation error. With --json errors are also emitted as
``{"error": "..."}`` on stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Sequence

from . import executor, layout, sysinfo
from .layout import Step

PROG = "omv-oar"


def _emit_json(obj: object) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _fail(args: argparse.Namespace, message: str, code: int) -> int:
    if getattr(args, "json", False):
        _emit_json({"error": message})
    sys.stderr.write("%s: %s\n" % (PROG, message))
    return code


# -----------------------------------------------------------------------
# Shared helpers.
# -----------------------------------------------------------------------

def _resolve_disks(
    state: sysinfo.SystemState, paths: Sequence[str]
) -> List[layout.Disk]:
    """Turn device path arguments into layout Disks, validating that
    each is an unused whole disk (same rules as ``candidates``: no
    mountpoint, no partitions, no filesystem/raid/lvm signature)."""
    disks = []
    for path in paths:
        real = os.path.realpath(path) if os.path.exists(path) else path
        dev = state.device_by_path(real)
        if dev is None:
            raise layout.LayoutError("no such block device: %s" % path)
        if dev.type not in ("disk", "loop"):
            raise layout.LayoutError("%s is not a whole disk" % path)
        if dev.ro:
            raise layout.LayoutError("%s is read-only" % path)
        if dev.mountpoint:
            raise layout.LayoutError("%s is mounted" % path)
        if dev.fstype:
            raise layout.LayoutError(
                "%s carries a %s signature; wipe it first (wipefs -a)"
                % (path, dev.fstype)
            )
        children = state.children(dev.kname)
        if children:
            raise layout.LayoutError(
                "%s is partitioned (%s); wipe it first (sgdisk --zap-all)"
                % (path, ", ".join(c.path for c in children))
            )
        disks.append(layout.Disk(dev.path, dev.size))
    return disks


def _require_pool(state: sysinfo.SystemState, pool: str) -> None:
    if pool not in sysinfo.pool_names(state):
        raise EngineError("no such pool: %s" % pool)


class EngineError(RuntimeError):
    pass


def _print_status(pool: str) -> None:
    state = sysinfo.collect()
    _emit_json(sysinfo.pool_status(state, pool))


# -----------------------------------------------------------------------
# Query commands.
# -----------------------------------------------------------------------

def cmd_candidates(args: argparse.Namespace) -> int:
    state = sysinfo.collect()
    _emit_json(sysinfo.candidates(state, allow_loop=args.allow_loop))
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    state = sysinfo.collect()
    disks = _resolve_disks(state, args.devices)
    _emit_json(layout.preview(disks))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = sysinfo.collect()
    names = sysinfo.pool_names(state)
    if args.pool is not None:
        # Contract: status of an unknown pool is an empty list, not an
        # error. The RPC layer relies on this to probe name uniqueness
        # before create.
        names = [args.pool] if args.pool in names else []
    _emit_json([sysinfo.pool_status(state, name) for name in names])
    return 0


def cmd_detail(args: argparse.Namespace) -> int:
    state = sysinfo.collect()
    _require_pool(state, args.pool)
    status = sysinfo.pool_status(state, args.pool)
    lines: List[str] = []
    lines.append("Pool:          %s" % status["name"])
    lines.append("State:         %s" % status["state"])
    lines.append("Device:        %s" % status["devicefile"])
    lines.append(
        "Filesystem:    %s" % (status["fstype"] or "n/a")
    )
    lines.append("Size:          %s" % sysinfo.format_binary(status["size"]))
    lines.append(
        "Allocated:     %s" % sysinfo.format_binary(status["allocated"])
    )
    lines.append("Free:          %s" % sysinfo.format_binary(status["free"]))
    lines.append(
        "Unallocatable: %s" % sysinfo.format_binary(status["unallocatable"])
    )
    lines.append(
        "Pending grow:  %s" % ("yes" if status["pending_finalize"] else "no")
    )
    lines.append("")
    lines.append("Disks:")
    for disk in status["disks"]:
        lines.append(
            "  %-16s %10s  %s"
            % (
                disk["devicefile"],
                sysinfo.format_binary(disk["size"]),
                disk["state"],
            )
        )
    for tier in status["tiers"]:
        lines.append("")
        lines.append("Tier %02d (%s):" % (tier["index"], tier["name"]))
        lines.append("  Device:  %s" % tier["devicefile"])
        lines.append("  Level:   %s" % tier["level"])
        lines.append(
            "  State:   %s (sync: %s %.1f%%, policy: %s)"
            % (
                tier["state"],
                tier["sync_action"],
                tier["progress"] * 100.0,
                tier["consistency_policy"] or "n/a",
            )
        )
        lines.append("  Size:    %s" % sysinfo.format_binary(tier["size"]))
        lines.append("  Members: %s" % " ".join(tier["members"]))
        if tier["devicefile"]:
            try:
                detail = subprocess.run(
                    ["mdadm", "--detail", tier["devicefile"]],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout
                excerpt = [
                    "    %s" % ln.strip()
                    for ln in detail.splitlines()
                    if ":" in ln
                ][:20]
                if excerpt:
                    lines.append("  mdadm --detail:")
                    lines.extend(excerpt)
            except OSError:
                pass
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


# -----------------------------------------------------------------------
# Mutating commands.
# -----------------------------------------------------------------------

def cmd_create(args: argparse.Namespace) -> int:
    layout.validate_pool_name(args.pool)
    state = sysinfo.collect()
    if any(str(r.get("vg_name")) == args.pool for r in state.vgs):
        raise layout.LayoutError(
            "a volume group named %s already exists" % args.pool
        )
    disks = _resolve_disks(state, args.devices)
    plan = layout.plan_create(args.pool, disks, fs=args.fs)
    executor.run_steps(plan.steps, dry_run=args.dry_run)
    if args.dry_run:
        return 0
    _print_status(args.pool)
    return 0


def cmd_grow(args: argparse.Namespace) -> int:
    layout.validate_pool_name(args.pool)
    state = sysinfo.collect()
    _require_pool(state, args.pool)
    status = sysinfo.pool_status(state, args.pool)
    if status["pending_finalize"]:
        raise layout.LayoutError(
            "pool %s has a pending grow; run finalize first" % args.pool
        )
    if status["state"] not in ("online", "checking"):
        raise layout.LayoutError(
            "pool %s is %s; grow requires an online pool"
            % (args.pool, status["state"])
        )
    lay = sysinfo.pool_layout(state, args.pool)
    disks = _resolve_disks(state, args.devices)
    plan = layout.plan_grow(args.pool, lay, disks)
    executor.run_steps(plan.steps, dry_run=args.dry_run)
    if args.dry_run:
        return 0
    _print_status(args.pool)
    return 0


def cmd_replace(args: argparse.Namespace) -> int:
    layout.validate_pool_name(args.pool)
    state = sysinfo.collect()
    _require_pool(state, args.pool)
    old_path = (
        os.path.realpath(args.olddev)
        if os.path.exists(args.olddev)
        else args.olddev
    )
    lay, old_alive, pre_steps = _replace_context(state, args.pool, old_path)
    new_disk = _resolve_disks(state, [args.newdev])[0]
    plan = layout.plan_replace(
        args.pool, lay, old_path, new_disk, old_alive=old_alive
    )
    executor.run_steps(tuple(pre_steps) + plan.steps, dry_run=args.dry_run)
    if args.dry_run:
        return 0
    _print_status(args.pool)
    return 0


def _replace_context(
    state: sysinfo.SystemState, pool: str, old_path: str
):
    """Layout + liveness for a replace. A missing old disk is
    synthesized into the layout from the arrays' missing slots; a
    present-but-faulty old disk gets its members removed first."""
    lay = sysinfo.pool_layout(state, pool)
    tiers_state = sysinfo.pool_tiers(state, pool)
    in_layout = any(d.name == old_path for d in lay.disks)
    if in_layout:
        old_dev = state.device_by_path(old_path)
        faulty_parts = []
        for index, tier in sorted(tiers_state.items()):
            if tier.md is None:
                continue
            entry = state.mdstat.get(tier.md.kname, {})
            for part in tier.partitions:
                if part.pkname == (old_dev.kname if old_dev else ""):
                    flags = entry.get("members", {}).get(part.kname, {})
                    if flags.get("faulty"):
                        faulty_parts.append((index, part.path))
        old_alive = old_dev is not None and not faulty_parts
        pre_steps = [
            Step(
                (
                    "mdadm",
                    layout.md_device(pool, index),
                    "--remove",
                    part_path,
                ),
                "remove faulty member %s" % part_path,
                check=False,
            )
            for index, part_path in faulty_parts
        ]
        return lay, old_alive, pre_steps
    # Old disk is gone: attribute every missing array slot to it.
    spec = []
    top = 0
    for index, tier in sorted(tiers_state.items()):
        if not tier.partitions:
            continue
        height = tier.partitions[0].size
        members = []
        for part in tier.partitions:
            parent = state.device_by_kname(part.pkname)
            if parent is not None:
                members.append(parent.path)
        entry = state.mdstat.get(tier.md.kname, {}) if tier.md else {}
        slots = entry.get("slots") or len(members)
        if slots > len(members):
            members.append(old_path)
            top += height
        spec.append((index, height, members))
    if top <= 0:
        raise layout.LayoutError(
            "%s is not a member (dead or alive) of pool %s"
            % (old_path, pool)
        )
    disks = [
        layout.Disk(d.path, d.size) for d in sysinfo.pool_disks(state, pool)
    ]
    disks.append(
        layout.Disk(
            old_path, layout.START_OFFSET + layout.END_RESERVE + top
        )
    )
    return layout.reconstruct(disks, spec), False, []


def cmd_finalize(args: argparse.Namespace) -> int:
    state = sysinfo.collect()
    if args.all:
        pools = [
            name
            for name in sysinfo.pool_names(state)
            if sysinfo.FINALIZE_TAG
            in sysinfo.vg_tags(
                next(r for r in state.vgs if str(r.get("vg_name")) == name)
            )
        ]
    else:
        if args.pool is None:
            raise layout.LayoutError("either a pool name or --all is required")
        _require_pool(state, args.pool)
        pools = [args.pool]
    for pool in pools:
        _finalize_pool(state, pool)
    return 0


def _finalize_pool(state: sysinfo.SystemState, pool: str) -> None:
    """Idempotent completion of a grow: wait for reshapes, restore PPL,
    pvresize, lvextend, grow the filesystem, drop the oar.finalize tag."""
    vg_row = next(
        (r for r in state.vgs if str(r.get("vg_name")) == pool), None
    )
    if vg_row is None or sysinfo.FINALIZE_TAG not in sysinfo.vg_tags(vg_row):
        sys.stdout.write("%s: nothing pending for pool %s\n" % (PROG, pool))
        return
    tiers = sysinfo.pool_tiers(state, pool)
    steps: List[Step] = []
    for index, tier in sorted(tiers.items()):
        if tier.md is None:
            continue
        md = tier.md.path or "/dev/%s" % tier.md.kname
        # Returns non-zero when there was nothing to wait for.
        steps.append(
            Step(("mdadm", "--wait", md), "wait for reshape", check=False)
        )
    for index, tier in sorted(tiers.items()):
        if tier.md is None:
            continue
        md = tier.md.path or "/dev/%s" % tier.md.kname
        policy = state.md_attrs.get(tier.md.kname, {}).get(
            "consistency_policy", ""
        )
        if policy != "ppl":
            steps.append(
                Step(
                    ("mdadm", "--grow", md, "--bitmap=none"),
                    "drop write-intent bitmap",
                    check=False,
                )
            )
            # If PPL cannot be enabled the array keeps the bitmap
            # (same fallback as create).
            steps.append(
                Step(
                    ("mdadm", "--grow", md, "--consistency-policy=ppl"),
                    "restore PPL",
                    check=False,
                )
            )
    for row in state.pvs:
        if str(row.get("vg_name")) == pool:
            steps.append(
                Step(
                    ("pvresize", str(row.get("pv_name"))),
                    "resize PV",
                )
            )
    lv_dev_path = "/dev/%s/data" % pool
    steps.append(
        Step(
            ("lvextend", "-l", "+100%FREE", lv_dev_path),
            "extend data LV (no-op if already full size)",
            check=False,
        )
    )
    executor.run_steps(steps)

    # Grow the filesystem. Re-read state: the LV may just have grown.
    dm = sysinfo.dm_name(pool, "data")
    lv_dev = next(
        (d for d in sysinfo.collect().devices if d.name == dm), None
    )
    fstype = lv_dev.fstype if lv_dev else ""
    mountpoint = lv_dev.mountpoint if lv_dev else ""
    if fstype == "btrfs":
        if mountpoint:
            executor.run_steps(
                [
                    Step(
                        ("btrfs", "filesystem", "resize", "max", mountpoint),
                        "grow btrfs",
                    )
                ]
            )
        else:
            tmpdir = tempfile.mkdtemp(prefix="oar-finalize-")
            try:
                executor.run_steps(
                    [
                        Step(("mount", lv_dev_path, tmpdir), "mount for resize"),
                        Step(
                            ("btrfs", "filesystem", "resize", "max", tmpdir),
                            "grow btrfs",
                        ),
                        Step(("umount", tmpdir), "unmount"),
                    ]
                )
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
    elif fstype == "ext4":
        # resize2fs handles mounted (online) and unmounted growth.
        executor.run_steps(
            [Step(("resize2fs", lv_dev_path), "grow ext4")]
        )
    executor.run_steps(
        [
            Step(
                ("vgchange", "--deltag", sysinfo.FINALIZE_TAG, pool),
                "clear pending-finalize marker",
            )
        ]
    )


def cmd_scrub(args: argparse.Namespace) -> int:
    state = sysinfo.collect()
    _require_pool(state, args.pool)
    tiers = sysinfo.pool_tiers(state, args.pool)
    steps: List[Step] = []
    started: List[str] = []
    for index, tier in sorted(tiers.items()):
        if tier.md is None:
            continue
        steps.append(
            Step(
                (
                    "sh",
                    "-c",
                    "echo check > /sys/block/%s/md/sync_action"
                    % tier.md.kname,
                ),
                "start check on %s" % tier.md.kname,
                check=False,
            )
        )
        started.append(layout.tier_name(args.pool, index))
    dm = sysinfo.dm_name(args.pool, "data")
    lv_dev = next((d for d in state.devices if d.name == dm), None)
    btrfs_scrub = bool(
        lv_dev and lv_dev.fstype == "btrfs" and lv_dev.mountpoint
    )
    if btrfs_scrub:
        steps.append(
            Step(
                ("btrfs", "scrub", "start", lv_dev.mountpoint),
                "start btrfs scrub",
                check=False,
            )
        )
    executor.run_steps(steps, dry_run=args.dry_run)
    _emit_json(
        {"name": args.pool, "started": started, "btrfs_scrub": btrfs_scrub}
    )
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    state = sysinfo.collect()
    _require_pool(state, args.pool)
    pool = args.pool
    dm = sysinfo.dm_name(pool, "data")
    lv_dev = next((d for d in state.devices if d.name == dm), None)
    mountpoint = lv_dev.mountpoint if lv_dev else ""
    if mountpoint and not args.force:
        raise EngineError(
            "pool %s is mounted at %s; use --force" % (pool, mountpoint)
        )
    tiers = sysinfo.pool_tiers(state, pool)
    disks = sysinfo.pool_disks(state, pool)
    lv_row = next(
        (
            r
            for r in state.lvs
            if str(r.get("vg_name")) == pool and str(r.get("lv_name")) == "data"
        ),
        None,
    )
    steps: List[Step] = []
    if mountpoint:
        steps.append(Step(("umount", mountpoint), "unmount %s" % mountpoint))
    if lv_row is not None:
        steps.append(
            Step(("lvremove", "-f", "%s/data" % pool), "remove data LV")
        )
    steps.append(Step(("vgremove", "-f", pool), "remove VG"))
    for row in state.pvs:
        if str(row.get("vg_name")) == pool:
            steps.append(
                Step(("pvremove", "-y", str(row.get("pv_name"))), "remove PV")
            )
    for index, tier in sorted(tiers.items()):
        if tier.md is not None:
            md = tier.md.path or "/dev/%s" % tier.md.kname
            steps.append(Step(("mdadm", "--stop", md), "stop array"))
    for index, tier in sorted(tiers.items()):
        for part in tier.partitions:
            steps.append(
                Step(
                    ("mdadm", "--zero-superblock", part.path),
                    "zero superblock",
                    check=False,
                )
            )
    for disk in disks:
        steps.append(
            Step(("sgdisk", "--zap-all", disk.path), "zap partition table")
        )
        steps.append(Step(("wipefs", "-a", disk.path), "wipe signatures"))
    executor.run_steps(steps, dry_run=args.dry_run)
    if args.dry_run:
        return 0
    _emit_json({"name": pool, "deleted": True})
    return 0


# -----------------------------------------------------------------------
# Argument parsing.
# -----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Manage OAR OAR storage pools "
        "(GPT slices -> mdadm RAID5 tiers -> LVM -> btrfs/ext4).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("candidates", help="list unused whole disks")
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--allow-loop", action="store_true", help="include /dev/loop devices"
    )
    p.set_defaults(func=cmd_candidates)

    p = sub.add_parser("preview", help="preview capacity for a disk set")
    p.add_argument("--json", action="store_true")
    p.add_argument("devices", nargs="+", metavar="DEV")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("create", help="create a pool")
    p.add_argument("--json", action="store_true")
    p.add_argument("--fs", choices=("btrfs", "ext4"), default="btrfs")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("pool", metavar="POOL")
    p.add_argument("devices", nargs="+", metavar="DEV")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("status", help="pool status as JSON")
    p.add_argument("--json", action="store_true")
    p.add_argument("pool", nargs="?", metavar="POOL")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("detail", help="human-readable pool details")
    p.add_argument("pool", metavar="POOL")
    p.set_defaults(func=cmd_detail)

    p = sub.add_parser("grow", help="add disks to a pool")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("pool", metavar="POOL")
    p.add_argument("devices", nargs="+", metavar="DEV")
    p.set_defaults(func=cmd_grow)

    p = sub.add_parser(
        "finalize", help="complete pending grows (idempotent)"
    )
    p.add_argument("--all", action="store_true")
    p.add_argument("pool", nargs="?", metavar="POOL")
    p.set_defaults(func=cmd_finalize)

    p = sub.add_parser("replace", help="replace a member disk")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("pool", metavar="POOL")
    p.add_argument("olddev", metavar="OLDDEV")
    p.add_argument("newdev", metavar="NEWDEV")
    p.set_defaults(func=cmd_replace)

    p = sub.add_parser("scrub", help="start a data scrub")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("pool", metavar="POOL")
    p.set_defaults(func=cmd_scrub)

    p = sub.add_parser("delete", help="delete a pool and wipe its disks")
    p.add_argument("--json", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("pool", metavar="POOL")
    p.set_defaults(func=cmd_delete)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except layout.LayoutError as exc:
        return _fail(args, str(exc), 2)
    except EngineError as exc:
        return _fail(args, str(exc), 1)
    except executor.ExecutorError as exc:
        return _fail(args, str(exc), 1)
    except subprocess.CalledProcessError as exc:
        return _fail(
            args,
            "command failed with exit code %d: %s"
            % (exc.returncode, " ".join(map(str, exc.cmd or []))),
            1,
        )
    except OSError as exc:
        return _fail(args, str(exc), 1)


if __name__ == "__main__":
    sys.exit(main())
