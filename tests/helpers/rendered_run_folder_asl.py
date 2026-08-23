# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load Terraform-rendered run-folder Step Functions ASL for behavioral tests."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STEP_FUNCTION_TF = _REPO_ROOT / "infra/deploy/modules/run_folder/step_function.tf"
_COMMITTED_FIXTURE = _REPO_ROOT / "tests/fixtures/rendered/run_folder_state_machine.json"
_FIXTURE_SOURCE_HASH = _REPO_ROOT / "tests/fixtures/rendered/run_folder_state_machine.source.sha256"


def _terraform_binary() -> str:
    for candidate in ("terraform", "tofu"):
        if shutil.which(candidate):
            return candidate
    raise FileNotFoundError("neither terraform nor tofu found on PATH")


def _extract_definition_hcl() -> str:
    source = _STEP_FUNCTION_TF.read_text(encoding="utf-8")
    start = source.index("definition = jsonencode({") + len("definition = jsonencode(")
    depth = 0
    for index in range(start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                inner = source[start + 1 : index]
                break
    else:
        raise ValueError("unable to locate run-folder jsonencode definition")
    inner = re.sub(
        r'aws_lambda_function\.this\[[^\]]+\]\.arn',
        '"arn:aws:lambda:us-east-1:123456789012:function:mock"',
        inner,
    )
    return inner


def render_run_folder_definition_via_terraform(
    lane: str = "read",
) -> dict[str, Any]:
    """Evaluate one lane-specialized jsonencode(...) block through Terraform."""
    if lane == "read":
        actions = '["plan", "plan_destroy", "drift", "report"]'
        mutation_lane = "false"
    elif lane == "apply":
        actions = '["apply"]'
        mutation_lane = "true"
    elif lane == "destroy":
        actions = '["destroy"]'
        mutation_lane = "true"
    else:
        raise ValueError(f"unsupported run-folder lane: {lane}")
    inner = _extract_definition_hcl()
    with tempfile.TemporaryDirectory() as temp_dir:
        main_tf = Path(temp_dir) / "main.tf"
        main_tf.write_text(
            f'variable "lane" {{\n  type    = string\n  default = "{lane}"\n}}\n'
            "locals {\n"
            f"  allowed_actions = {actions}\n"
            f"  mutation_lane = {mutation_lane}\n"
            f"  definition = {{\n{inner}\n  }}\n"
            "}\n",
            encoding="utf-8",
        )
        subprocess.run(
            [_terraform_binary(), "init", "-backend=false"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        proc = subprocess.run(
            [_terraform_binary(), "console"],
            cwd=temp_dir,
            input="jsonencode(local.definition)\n",
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "TF_IN_AUTOMATION": "1"},
        )
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    stdout = ansi_escape.sub("", proc.stdout).strip()
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if candidate.startswith(('"', "{", "[")):
            encoded = json.loads(candidate)
            break
    else:
        raise ValueError(f"terraform console did not return JSON: {stdout[:200]!r}")
    return json.loads(encoded)


def _current_source_hash() -> str:
    return hashlib.sha256(_extract_definition_hcl().encode("utf-8")).hexdigest()


def _write_fixture(rendered: dict[str, Any], source_hash: str) -> None:
    _COMMITTED_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    _COMMITTED_FIXTURE.write_text(
        json.dumps(rendered, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _FIXTURE_SOURCE_HASH.write_text(source_hash + "\n", encoding="utf-8")


@lru_cache(maxsize=3)
def load_rendered_run_folder_definition(lane: str = "read") -> dict[str, Any]:
    """Return Terraform-rendered ASL, never trusting a stale committed fixture.

    The fixture is only used when its recorded source hash matches the current
    step_function.tf definition; otherwise it is re-rendered via terraform and
    rewritten. A missing terraform binary with a stale fixture fails loudly.
    """
    source_hash = _current_source_hash()
    if lane == "read" and _COMMITTED_FIXTURE.is_file() and _FIXTURE_SOURCE_HASH.is_file():
        recorded = _FIXTURE_SOURCE_HASH.read_text(encoding="utf-8").strip()
        if recorded == source_hash:
            return json.loads(_COMMITTED_FIXTURE.read_text(encoding="utf-8"))
    rendered = render_run_folder_definition_via_terraform(lane)
    if lane == "read":
        _write_fixture(rendered, source_hash)
    return rendered


def rendered_pass_state(state_name: str) -> dict[str, Any]:
    definition = load_rendered_run_folder_definition()
    states = definition.get("States")
    if not isinstance(states, dict):
        raise TypeError("rendered run-folder definition is missing States")
    state = states.get(state_name)
    if not isinstance(state, dict):
        raise KeyError(f"rendered state not found: {state_name}")
    if state.get("Type") != "Pass":
        raise ValueError(f"state {state_name} is not a Pass state")
    parameters = state.get("Parameters")
    if not isinstance(parameters, dict):
        raise TypeError(f"state {state_name} is missing Parameters")
    return parameters
