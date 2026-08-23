# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ASL state-graph reachability checks over Terraform-rendered Step Functions definitions."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPENCI_TF_MODULE = _REPO_ROOT / "infra/deploy/modules/openci_tf"

_MOCK_LAMBDA = '"arn:aws:lambda:us-east-1:123456789012:function:mock"'
_MOCK_READ_SFN = '"arn:aws:states:us-east-1:123456789012:stateMachine:mock-read"'
_MOCK_APPLY_SFN = '"arn:aws:states:us-east-1:123456789012:stateMachine:mock-apply"'
_MOCK_DESTROY_SFN = '"arn:aws:states:us-east-1:123456789012:stateMachine:mock-destroy"'


def _substitute_tf_expressions(inner: str) -> str:
    inner = re.sub(r"local\.lambda_arns\[[^\]]+\]", _MOCK_LAMBDA, inner)
    inner = inner.replace(
        "var.run_folder_apply_state_machine_arn", _MOCK_APPLY_SFN
    )
    inner = inner.replace(
        "var.run_folder_destroy_state_machine_arn", _MOCK_DESTROY_SFN
    )
    inner = inner.replace("var.run_folder_state_machine_arn", _MOCK_READ_SFN)
    return inner


def _extract_balanced_block(source: str, open_index: int) -> str:
    depth = 0
    for index in range(open_index, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[open_index + 1 : index]
    raise ValueError(f"unbalanced braces at index {open_index}")


def _extract_jsonencode_inner(source: str, marker: str) -> str:
    idx = source.index(marker)
    brace = source.index("{", idx)
    inner = _extract_balanced_block(source, brace)
    return _substitute_tf_expressions(inner)


def _terraform_eval_locals(locals_hcl: str, expression: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        main_tf = Path(temp_dir) / "main.tf"
        main_tf.write_text(locals_hcl, encoding="utf-8")
        subprocess.run(
            ["tofu", "init", "-backend=false"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        proc = subprocess.run(
            ["tofu", "console"],
            cwd=temp_dir,
            input=f"jsonencode({expression})\n",
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
        raise ValueError(f"tofu console did not return JSON: {stdout[:200]!r}")
    return json.loads(encoded)


def render_read_outer_definition() -> dict[str, Any]:
    source = (_OPENCI_TF_MODULE / "step_function.tf").read_text(encoding="utf-8")
    inner = _extract_jsonencode_inner(source, "definition = jsonencode({")
    locals_hcl = f"locals {{\n  definition = {{\n{inner}\n  }}\n}}\n"
    return _terraform_eval_locals(locals_hcl, "local.definition")


def render_mutation_outer_definition(resource: str) -> dict[str, Any]:
    mutation_source = (
        _OPENCI_TF_MODULE / "step_function_mutation_outer.tf"
    ).read_text(encoding="utf-8")
    shared_inner = _extract_balanced_block(
        mutation_source,
        mutation_source.index("mutation_outer_shared_terminal_states = {")
        + len("mutation_outer_shared_terminal_states = "),
    )
    shared_inner = _substitute_tf_expressions(shared_inner)
    marker = f'resource "aws_sfn_state_machine" "{resource}"'
    resource_block = mutation_source.split(marker, 1)[1]
    definition_inner = _extract_jsonencode_inner(resource_block, "definition = jsonencode({")
    locals_hcl = (
        f"locals {{\n"
        f"  mutation_outer_shared_terminal_states = {{\n{shared_inner}\n  }}\n"
        f"  definition = {{\n{definition_inner}\n  }}\n"
        f"}}\n"
    )
    return _terraform_eval_locals(locals_hcl, "local.definition")


def collect_transition_targets(state: dict[str, Any]) -> set[str]:
    targets: set[str] = set()
    if "Next" in state:
        targets.add(state["Next"])
    if state.get("Type") == "Choice":
        for choice in state.get("Choices", []):
            if "Next" in choice:
                targets.add(choice["Next"])
        if "Default" in state:
            targets.add(state["Default"])
    for catch in state.get("Catch", []):
        if "Next" in catch:
            targets.add(catch["Next"])
    return targets


def _unreachable_in_states_map(states: dict[str, Any], start_at: str) -> list[str]:
    reachable: set[str] = set()

    def visit(name: str) -> None:
        if name in reachable or name not in states:
            return
        reachable.add(name)
        state = states[name]
        for target in collect_transition_targets(state):
            visit(target)
        if state.get("Type") == "Map" and "Iterator" in state:
            iterator = state["Iterator"]
            sub_unreachable = _unreachable_in_states_map(
                iterator.get("States", {}),
                iterator["StartAt"],
            )
            if sub_unreachable:
                raise ValueError(
                    f"iterator unreachable from {iterator['StartAt']}: {sub_unreachable}"
                )
        if state.get("Type") == "Parallel":
            for branch in state.get("Branches", []):
                sub_unreachable = _unreachable_in_states_map(
                    branch.get("States", {}),
                    branch["StartAt"],
                )
                if sub_unreachable:
                    raise ValueError(
                        f"parallel branch unreachable from {branch['StartAt']}: {sub_unreachable}"
                    )

    visit(start_at)
    return sorted(set(states.keys()) - reachable)


def unreachable_states(definition: dict[str, Any]) -> list[str]:
    states = definition.get("States")
    if not isinstance(states, dict):
        raise TypeError("ASL definition missing States map")
    start_at = definition.get("StartAt")
    if not isinstance(start_at, str):
        raise TypeError("ASL definition missing StartAt")
    return _unreachable_in_states_map(states, start_at)


def invalid_choice_states(definition: dict[str, Any]) -> list[str]:
    invalid: list[str] = []

    def check_map(prefix: str, states: dict[str, Any]) -> None:
        for name, state in states.items():
            qualified = f"{prefix}.{name}" if prefix else name
            if state.get("Type") == "Choice" and not state.get("Choices"):
                invalid.append(qualified)
            if state.get("Type") == "Map" and "Iterator" in state:
                check_map(qualified, state["Iterator"].get("States", {}))
            if state.get("Type") == "Parallel":
                for index, branch in enumerate(state.get("Branches", [])):
                    check_map(f"{qualified}.branch[{index}]", branch.get("States", {}))

    check_map("", definition["States"])
    return invalid


def dangling_transitions(definition: dict[str, Any]) -> list[tuple[str, str]]:
    dangling: list[tuple[str, str]] = []

    def check_map(prefix: str, states: dict[str, Any]) -> None:
        for name, state in states.items():
            qualified = f"{prefix}.{name}" if prefix else name
            for target in collect_transition_targets(state):
                if target not in states:
                    dangling.append((qualified, target))
            if state.get("Type") == "Map" and "Iterator" in state:
                iter_states = state["Iterator"].get("States", {})
                for iname, istate in iter_states.items():
                    for target in collect_transition_targets(istate):
                        if target not in iter_states:
                            dangling.append((f"{qualified}.{iname}", target))
            if state.get("Type") == "Parallel":
                for branch in state.get("Branches", []):
                    branch_states = branch.get("States", {})
                    for bname, bstate in branch_states.items():
                        for target in collect_transition_targets(bstate):
                            if target not in branch_states:
                                dangling.append((f"{qualified}.{bname}", target))

    check_map("", definition["States"])
    return dangling
