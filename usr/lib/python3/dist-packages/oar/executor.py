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
Plan execution: run :class:`oar.layout.Step` lists via
``subprocess.run``, or print them without executing (``--dry-run``).
Command output is streamed unbuffered to stdout so the RPC layer can
tail it into a background output file.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from typing import Iterable, Sequence

from .layout import Step

__all__ = ["ExecutorError", "format_plan", "run_steps"]


class ExecutorError(RuntimeError):
    """A plan step failed."""

    def __init__(self, step: Step, returncode: int):
        self.step = step
        self.returncode = returncode
        super().__init__(
            "command failed with exit code %d: %s"
            % (returncode, shlex.join(step.argv))
        )


def format_plan(steps: Iterable[Step]) -> list:
    """Render steps as 'PLAN: <shell-quoted argv>' lines."""
    return ["PLAN: %s" % shlex.join(step.argv) for step in steps]


def _print(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _run(argv: Sequence[str]) -> int:
    """Run one command, child stdout/stderr inherited (streamed)."""
    env = dict(os.environ, LC_ALL="C.UTF-8", LANG="C.UTF-8")
    sys.stdout.flush()
    sys.stderr.flush()
    proc = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
        env=env,
        check=False,
    )
    return proc.returncode


def run_steps(steps: Iterable[Step], dry_run: bool = False) -> None:
    """Execute (or with ``dry_run`` just print) a list of plan steps.

    Raises :class:`ExecutorError` on the first fatal failure. A step
    with ``fallback_argv`` gets a second chance with the fallback
    command; a step with ``check`` False only logs its failure.
    """
    if dry_run:
        for line in format_plan(steps):
            _print(line)
        return
    for step in steps:
        _print(">>> %s" % shlex.join(step.argv))
        rc = _run(step.argv)
        if rc == 0:
            continue
        if step.fallback_argv is not None:
            _print(
                "WARNING: command failed (exit %d), trying fallback: %s"
                % (rc, shlex.join(step.fallback_argv))
            )
            rc = _run(step.fallback_argv)
            if rc == 0:
                continue
        if not step.check:
            _print("NOTICE: non-fatal command failed (exit %d)" % rc)
            continue
        raise ExecutorError(step, rc)
