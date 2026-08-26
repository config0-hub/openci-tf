# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for install/uninstall phase timing helper."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PHASE_TIMING = _REPO_ROOT / "scripts/phase_timing.sh"
_JUSTFILE = _REPO_ROOT / "justfile"

_DONE_RE = re.compile(r"^<< success done in \d+m \d+s$")
_ANY_DONE_RE = re.compile(r"^<< .+ done in \d+m \d+s$")
_FAILED_RE = re.compile(r"^<< failure FAILED after \d+m \d+s$")
_TOTAL_DONE_RE = re.compile(r"^<< install total in \d+m \d+s$")
_TOTAL_FAILED_RE = re.compile(r"^<< install total FAILED after \d+m \d+s$")


def _run_helper(body: str, *, errexit: bool = True) -> subprocess.CompletedProcess[str]:
    errexit_line = "set -euo pipefail" if errexit else "set -uo pipefail"
    script = f"""#!/usr/bin/env bash
{errexit_line}
# shellcheck source=scripts/phase_timing.sh
source "{_PHASE_TIMING}"
{body}
"""
    return subprocess.run(
        ["bash", "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_phase_timing_run_success_prints_done_line_with_duration():
    completed = _run_helper('phase_timing_run success true')
    assert completed.returncode == 0
    lines = completed.stderr.splitlines()
    assert lines[0] == ">> success start"
    assert _DONE_RE.match(lines[1])


def test_phase_timing_run_failure_preserves_exit_code_and_prints_failed():
    completed = _run_helper('phase_timing_run failure bash -c "exit 42"')
    assert completed.returncode == 42
    lines = completed.stderr.splitlines()
    assert lines[0] == ">> failure start"
    assert _FAILED_RE.match(lines[1])


def test_phase_timing_run_multi_arg_quoted_command():
    completed = _run_helper(
        'phase_timing_run multi-arg bash -c "echo one two; exit 0"'
    )
    assert completed.returncode == 0
    lines = completed.stderr.splitlines()
    assert lines[0] == ">> multi-arg start"
    assert _ANY_DONE_RE.match(lines[1])
    assert completed.stdout.strip() == "one two"


def test_phase_timing_run_child_stderr_interleaves_between_timing_lines():
    completed = _run_helper(
        'phase_timing_run interleave bash -c "echo child-stderr >&2"'
    )
    assert completed.returncode == 0
    lines = completed.stderr.splitlines()
    assert lines[0] == ">> interleave start"
    assert lines[1] == "child-stderr"
    assert lines[2].startswith("<< interleave done in ")


def test_phase_timing_run_returns_failure_without_caller_set_e():
    completed = _run_helper(
        """
rc=0
phase_timing_run no-errexit bash -c "exit 7" || rc=$?
exit "$rc"
""",
        errexit=False,
    )
    assert completed.returncode == 7
    lines = completed.stderr.splitlines()
    assert lines[0] == ">> no-errexit start"
    assert lines[1].startswith("<< no-errexit FAILED after ")


def test_phase_timing_total_end_prints_total_on_success():
    completed = _run_helper(
        """
phase_timing_total_begin
phase_timing_run ok true
phase_timing_total_end install 0
"""
    )
    assert completed.returncode == 0
    assert _TOTAL_DONE_RE.match(completed.stderr.splitlines()[-1])


def test_phase_timing_total_end_prints_failed_total_and_preserves_exit_code():
    completed = _run_helper(
        """
phase_timing_total_begin
journey_rc=0
phase_timing_run fail bash -c "exit 42" || journey_rc=$?
phase_timing_total_end install "$journey_rc"
exit "$journey_rc"
"""
    )
    assert completed.returncode == 42
    lines = completed.stderr.splitlines()
    assert lines[0] == ">> fail start"
    assert lines[1].startswith("<< fail FAILED after ")
    assert _TOTAL_FAILED_RE.match(lines[2])


def test_justfile_foundation_destroy_has_exactly_one_terraform_destroy():
    section = _JUSTFILE.read_text().split("foundation-destroy:", 1)[1].split(
        "engine:", 1
    )[0]
    destroy_lines = [
        line
        for line in section.splitlines()
        if "terraform -chdir=infra/foundation destroy" in line
    ]
    assert len(destroy_lines) == 1
