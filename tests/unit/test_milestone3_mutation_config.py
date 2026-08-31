# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mutation config, grace wait, links, and plan show tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest  # type: ignore[import-not-found]

from src.core.errors import ConfigValidationError
from src.core.models import FolderConfig, MutationVerbConfig
from src.domain.cmd_builder.script_generator import ScriptParams, render
from src.domain.config.folder_config import parse_folder_config
from src.domain.formatters.console_urls import (
    codebuild_build_url,
    step_functions_execution_url,
)

MUTATION_OUTER = Path(
    "infra/deploy/modules/openci_tf/step_function_mutation_outer.tf"
).read_text()


def test_apply_destroy_allow_independent():
    config = parse_folder_config(
        "account_alias: target\napply:\n  allow: true\ndestroy:\n  allow: false\n"
    )
    assert config.apply.allow is True
    assert config.destroy.allow is False


def test_grace_seconds_defaults_when_block_present():
    config = parse_folder_config(
        "account_alias: target\napply:\n  allow: true\ndestroy:\n  allow: true\n"
    )
    assert config.apply.grace_seconds == 15
    assert config.destroy.grace_seconds == 60


@pytest.mark.parametrize(
    "yaml,message",
    [
        ("account_alias: t\napply:\n  allow: true\n  grace_seconds: true\n", "integer"),
        ("account_alias: t\napply:\n  allow: true\n  grace_seconds: 1.5\n", "integer"),
        (
            "account_alias: t\napply:\n  allow: true\n  grace_seconds: -1\n",
            "between 0 and",
        ),
        (
            "account_alias: t\napply:\n  allow: true\n  grace_seconds: 4000\n",
            "between 0 and",
        ),
        ("account_alias: t\napply:\n  grace_seconds: 10\n", "allow is required"),
    ],
)
def test_grace_seconds_validation(yaml, message):
    with pytest.raises(ConfigValidationError) as exc:
        parse_folder_config(yaml)
    assert message in str(exc.value)


def test_legacy_boolean_apply_destroy_rejected():
    with pytest.raises(ConfigValidationError, match="mapping with allow/grace_seconds"):
        parse_folder_config("account_alias: target\napply: true\n")


def test_apply_destroy_mapping_rejects_unknown_keys():
    with pytest.raises(ConfigValidationError, match="unknown keys"):
        parse_folder_config(
            "account_alias: target\napply:\n  allow: true\n  extra: true\n"
        )


def test_folder_config_from_dict_round_trip():
    raw = {
        "version": 1,
        "timeout": 300,
        "tf_runtime": "tofu:1.10.6",
        "account_alias": "target",
        "execution_target": "lambda",
        "extra_flags": (),
        "ssm_env_paths": (),
        "apply": {"allow": True, "grace_seconds": 20},
        "destroy": {"allow": False, "grace_seconds": 60},
    }
    config = FolderConfig(**raw)
    assert config.apply.grace_seconds == 20
    assert config.resolved_grace_seconds("apply") == 20


def test_mutation_outer_has_grace_wait_before_folder():
    assert "GraceWait" in MUTATION_OUTER
    assert 'SecondsPath = "$.grace_seconds"' in MUTATION_OUTER
    assert re.search(r'StartAt = "GraceWait"', MUTATION_OUTER)
    grace_idx = MUTATION_OUTER.index("GraceWait")
    run_idx = MUTATION_OUTER.index("SequentialRunFolder")
    assert grace_idx < run_idx


def test_mutation_artifact_uploader_sanitizes_hyphens_in_environment_names():
    script = render(ScriptParams(verb="apply", execution_target="codebuild"))
    assert "tr -c '[:alnum:]_' '_'" in script
    assert "tr './' '__'" not in script


def test_apply_script_runs_tofu_show_before_apply():
    script = render(ScriptParams(verb="apply", execution_target="codebuild"))
    show_pos = script.index("tofu show")
    apply_pos = script.index("tofu apply")
    assert show_pos < apply_pos
    assert "plan-show.out" in script
    assert 'if [ "$show_status" -ne 0 ]; then' in script
    assert 'exit "$show_status"' in script


def test_destroy_script_runs_tofu_show_before_apply():
    script = render(ScriptParams(verb="destroy", execution_target="codebuild"))
    assert "tofu show" in script
    assert script.index("tofu show") < script.index("tofu apply")


@pytest.mark.parametrize(
    "region,execution_arn",
    [
        (
            "us-east-1",
            "arn:aws:states:us-east-1:123456789012:execution:openci-tf-apply:exec-name",
        ),
        (
            "eu-west-1",
            "arn:aws:states:eu-west-1:210987654321:execution:openci-tf-destroy:weird/exec%20id",
        ),
    ],
)
def test_step_functions_execution_url_exact(region, execution_arn):
    url = step_functions_execution_url(execution_arn, region=region)
    assert url.startswith(f"https://console.aws.amazon.com/states/home?region={region}")
    assert "exec-name" in url or "weird" in url


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-1"])
def test_codebuild_build_url_exact(region):
    url = codebuild_build_url(
        "openci-tf-worker",
        "openci-tf-worker:11111111-2222-3333-4444-555555555555",
        region=region,
    )
    assert url.startswith(
        f"https://{region}.console.aws.amazon.com/codesuite/codebuild/"
    )
    assert "openci-tf-worker" in url
    assert "11111111-2222-3333-4444-555555555555" in url


def test_mutation_verb_config_rejects_bool_grace():
    with pytest.raises(ValueError, match="integer"):
        MutationVerbConfig(allow=True, grace_seconds=True)  # type: ignore[arg-type]
