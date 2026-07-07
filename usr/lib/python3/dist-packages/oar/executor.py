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
    """Machine-readable output (dry-run plan) -> stdout."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _log(line: str) -> None:
    """Human progress -> stderr, so a ``--json`` command's stdout stays
    pure JSON for callers that parse it (the RPC ``scrub`` decoder, the
    e2e harness, ad-hoc scripts). The RPC merges stderr into stdout for
    its streaming progress dialog, so nothing is lost there."""
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def _run(argv: Sequence[str]) -> int:
    """Run one command with its stdout AND stderr streamed to our
    stderr, keeping our stdout clean for the final JSON result."""
    env = dict(os.environ, LC_ALL="C.UTF-8", LANG="C.UTF-8")
    sys.stdout.flush()
    sys.stderr.flush()
    proc = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=2,
        stderr=2,
        env=env,
        check=False,
    )
    return proc.returncode


def run_steps(steps: Iterable[Step], dry_run: bool = False) -> None:
    """Execute (or with ``dry_run`` just print) a list of plan steps.

    Raises :class:`ExecutorError` on the first fatal failure. A step
    with ``fallback_argv`` gets a second chance with the fallback
    command; a step with ``check`` False only logs its failure.

    Execution progress goes to stderr (see :func:`_log`); only the
    ``dry_run`` plan is written to stdout.
    """
    if dry_run:
        for line in format_plan(steps):
            _print(line)
        return
    for step in steps:
        _log(">>> %s" % shlex.join(step.argv))
        rc = _run(step.argv)
        if rc == 0:
            continue
        if step.fallback_argv is not None:
            _log(
                "WARNING: command failed (exit %d), trying fallback: %s"
                % (rc, shlex.join(step.fallback_argv))
            )
            rc = _run(step.fallback_argv)
            if rc == 0:
                continue
        if not step.check:
            _log("NOTICE: non-fatal command failed (exit %d)" % rc)
            continue
        raise ExecutorError(step, rc)
