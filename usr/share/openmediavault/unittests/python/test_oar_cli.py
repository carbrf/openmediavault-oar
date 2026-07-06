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
"""Finalize hardening coverage: the write-hole (PPL) fallback that
``_finalize_pool_locked`` attaches to the consistency-policy step, and
the per-pool ``_finalize_pool`` flock that keeps concurrent finalizers
from racing. Both patch executor/sysinfo/os boundaries so no real
subprocess, device collection or /run/lock is touched."""
import contextlib
import fcntl
import io
import os
import shutil
import tempfile
import unittest

from oar import cli
from oar.sysinfo import BlockDevice, SystemState

GiB = 1024 ** 3

MD_PATH = "/dev/md/tank-t00"
MD_KNAME = "md127"


def _tank_state(consistency_policy):
    """A minimal one-tier 'tank' snapshot carrying the oar.finalize tag:
    disk sda -> partition sda1 (partlabel oar@tank@t00) -> md tank-t00.
    ``consistency_policy`` drives the != 'ppl' branch under test."""
    disk = BlockDevice(
        name="sda", kname="sda", path="/dev/sda", size=20 * GiB, type="disk"
    )
    part = BlockDevice(
        name="sda1", kname="sda1", path="/dev/sda1", size=20 * GiB - 1024 ** 2,
        type="part", pkname="sda", partlabel="oar@tank@t00",
        fstype="linux_raid_member",
    )
    md = BlockDevice(
        name="tank-t00", kname=MD_KNAME, path=MD_PATH, size=20 * GiB,
        type="raid5", pkname="sda1", fstype="LVM2_member",
    )
    return SystemState(
        devices=[disk, part, md],
        mdstat={
            MD_KNAME: {
                "active": True,
                "level": "raid5",
                "members": {
                    "sda1": {"faulty": False, "spare": False,
                             "replacement": False}
                },
                "slots": 1,
                "up": 1,
            }
        },
        md_attrs={MD_KNAME: {"consistency_policy": consistency_policy}},
        vgs=[{"vg_name": "tank", "vg_tags": "omv-oar,oar.finalize",
              "vg_size": "0", "vg_free": "0"}],
        pvs=[{"pv_name": MD_PATH, "vg_name": "tank"}],
        lvs=[],
    )


class FinalizePplFallbackTestCase(unittest.TestCase):
    """Contract: for a tier whose md consistency_policy != 'ppl',
    ``_finalize_pool_locked`` emits a bitmap-drop step followed by a
    PPL-restore step that carries the internal-bitmap fallback, so a
    failed PPL enable never leaves the array without write-hole
    protection. A tier already on 'ppl' emits neither step."""

    def setUp(self):
        self._orig_run_steps = cli.executor.run_steps
        self._orig_collect = cli.sysinfo.collect
        self.batches = []
        # Re-collect after the primary batch (reap + fs-grow) sees no
        # devices, so those follow-up sequences are no-ops.
        cli.sysinfo.collect = lambda: SystemState()
        cli.executor.run_steps = lambda steps, dry_run=False: (
            self.batches.append(list(steps))
        )

    def tearDown(self):
        cli.executor.run_steps = self._orig_run_steps
        cli.sysinfo.collect = self._orig_collect

    @property
    def steps(self):
        return [step for batch in self.batches for step in batch]

    def test_bitmap_policy_emits_ppl_restore_with_bitmap_fallback(self):
        cli._finalize_pool_locked(_tank_state("bitmap"), "tank")
        ppl = [s for s in self.steps if s.argv[-1] == "--consistency-policy=ppl"]
        self.assertEqual(len(ppl), 1)
        self.assertEqual(
            ppl[0].argv, ("mdadm", "--grow", MD_PATH, "--consistency-policy=ppl")
        )
        self.assertEqual(
            ppl[0].fallback_argv,
            ("mdadm", "--grow", MD_PATH, "--bitmap=internal"),
        )
        # The write-intent bitmap is dropped before PPL is re-enabled.
        self.assertTrue(
            any(s.argv == ("mdadm", "--grow", MD_PATH, "--bitmap=none")
                for s in self.steps)
        )

    def test_ppl_policy_emits_no_bitmap_or_ppl_steps(self):
        cli._finalize_pool_locked(_tank_state("ppl"), "tank")
        for step in self.steps:
            self.assertNotEqual(step.argv[-1], "--consistency-policy=ppl")
            self.assertNotIn("--bitmap=none", step.argv)
            self.assertNotIn("--bitmap=internal", step.argv)
            self.assertIsNone(step.fallback_argv)


class FinalizePoolLockTestCase(unittest.TestCase):
    """Contract: ``_finalize_pool`` takes a non-blocking exclusive flock
    per pool. A caller that cannot take a held lock skips the work; once
    the lock frees the work runs; and if the lock file cannot be opened
    at all the work runs unlocked rather than being silently dropped."""

    def setUp(self):
        self._orig_os_open = cli.os.open
        self._orig_makedirs = cli.os.makedirs
        self._orig_locked = cli._finalize_pool_locked
        self.tmpdir = tempfile.mkdtemp(prefix="oar-lock-test-")
        self.lock_path = os.path.join(self.tmpdir, "finalize.lock")
        with open(self.lock_path, "w"):
            pass
        self.calls = []
        cli._finalize_pool_locked = lambda state, pool: self.calls.append(pool)
        cli.os.makedirs = lambda *a, **k: None
        real_open = self._orig_os_open
        lock_path = self.lock_path
        cli.os.open = lambda path, *a, **k: real_open(
            lock_path, os.O_CREAT | os.O_RDWR, 0o600
        )

    def tearDown(self):
        cli.os.open = self._orig_os_open
        cli.os.makedirs = self._orig_makedirs
        cli._finalize_pool_locked = self._orig_locked
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _finalize(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._finalize_pool(SystemState(), "tank")
        return buf.getvalue()

    def test_contention_skips_then_release_runs(self):
        held = self._orig_os_open(self.lock_path, os.O_RDWR)
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = self._finalize()
            self.assertEqual(self.calls, [])
            self.assertIn("skipping", out)
            fcntl.flock(held, fcntl.LOCK_UN)
            self._finalize()
            self.assertEqual(self.calls, ["tank"])
        finally:
            os.close(held)

    def test_open_failure_runs_unlocked(self):
        def boom(*a, **k):
            raise OSError("/run/lock unavailable")

        cli.os.open = boom
        self._finalize()
        self.assertEqual(self.calls, ["tank"])


if __name__ == "__main__":
    unittest.main()
