# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from src.core.models import FolderConfig
from src.domain.cmd_builder.cmd_resolver import resolve_commands
from src.domain.cmd_builder.script_generator import ScriptParams, render
from src.domain.engine.payload import EnginePayload


def _engine_payload_path() -> Path:
    for candidate in (Path("engine_payload.py"), Path("docker/engine_ref/payload.py")):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("engine payload contract file is missing")


@pytest.fixture(scope="module")
def simple_payload_type():
    spec = importlib.util.spec_from_file_location(
        "engine_payload", _engine_payload_path()
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.SimplePayload


@pytest.mark.parametrize("verb", ["plan", "drift", "report"])
def test_real_engine_simple_payload_validates_each_safe_verb(simple_payload_type, verb):
    resolved = resolve_commands(verb, FolderConfig(account_alias="target"))
    script = render(
        ScriptParams(
            verb=resolved.verb,
            execution_target=resolved.execution_target,
            normalize_drift=resolved.normalize_drift,
            extra_flags=resolved.extra_flags,
        )
    )
    assert script.startswith("#!/usr/bin/env bash")
    payload = EnginePayload(
        trigger_id=f"run-{verb}",
        s3_package_uri="s3://package/run.zip",
        sops_type="kms",
        sops_path=None,
        commands_b64=base64.b64encode(json.dumps(resolved.cmds).encode()).decode(),
        done_endpoint="s3://done/run/done",
        execution_target=resolved.execution_target,
        timeout_seconds=900,
    )
    payload.validate()
    simple_payload_type.from_dict(payload.__dict__).validate()
    assert json.loads(base64.b64decode(payload.commands_b64)) == resolved.cmds
