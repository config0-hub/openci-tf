# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hermetic tests for target onboarding helpers."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from src.domain.accounts.external_id import derive_external_id

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DERIVE = _REPO_ROOT / "scripts/derive_external_id.sh"
_BUCKET_FROM_ARN = _REPO_ROOT / "scripts/bucket_from_s3_arn.sh"
_ACCOUNT_SET_APPLY = _REPO_ROOT / "scripts/account_set_apply.sh"
_JUSTFILE = _REPO_ROOT / "justfile"
_TARGET_CONNECT_MODULE = _REPO_ROOT / "infra/modules/executor-readonly/main.tf"
_TARGET_CONNECT_ROOT = _REPO_ROOT / "infra/target-connect"

pytestmark = pytest.mark.skipif(
    not _DERIVE.is_file() or not _JUSTFILE.is_file(),
    reason="onboarding scripts and justfile are not copied into the docker test image",
)


def _run(
    script: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    import os

    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [str(script), *args],
        cwd=_REPO_ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("hub_account_id", "target_account_id"),
    [
        ("", "222222222222"),
        ("123", "222222222222"),
        ("abcdefghijkl", "222222222222"),
        ("111111111111", ""),
        ("111111111111", "123"),
        ("111111111111", "abcdefghijkl"),
        ("111111111111", "1234567890123"),
    ],
)
def test_derive_external_id_rejects_invalid_account_ids(
    hub_account_id: str, target_account_id: str
):
    result = _run(_DERIVE, hub_account_id, target_account_id)
    assert result.returncode != 0
    assert "12 decimal digits" in result.stderr or "Usage" in result.stderr


def test_derive_external_id_known_vector_and_script_parity():
    expected = "openci-tf-b3b391e562bd71b6"
    assert derive_external_id("111111111111", "222222222222") == expected
    result = _run(_DERIVE, "111111111111", "222222222222")
    assert result.returncode == 0
    assert result.stdout.strip() == expected
    assert re.fullmatch(r"openci-tf-[0-9a-f]{16}", result.stdout.strip())


def test_bucket_from_s3_arn_extracts_bucket_name():
    result = _run(_BUCKET_FROM_ARN, "arn:aws:s3:::openci-tf-state-222222222222")
    assert result.returncode == 0
    assert result.stdout.strip() == "openci-tf-state-222222222222"


def test_justfile_lists_onboarding_recipes():
    text = _JUSTFILE.read_text()
    assert "target-onboard" in text
    assert "register-target" in text


def test_onboarding_recipes_forward_positional_arguments_safely():
    text = _JUSTFILE.read_text()
    target_onboard = text.split("target-onboard", 1)[1].split("register-target", 1)[0]
    register_target = text.split("register-target", 1)[1].split("create-webhook", 1)[0]
    assert "{{hub_account_id}}" not in target_onboard
    assert "{{state_bucket}}" not in target_onboard
    assert "external_id" not in target_onboard
    assert "external-id" not in target_onboard
    assert "{{alias}}" not in register_target
    assert "{{target_account_id}}" not in register_target
    assert "external_id" not in register_target
    assert "external-id" not in register_target
    assert 'hub_account_id="${1' in target_onboard
    assert 'alias="${1' in register_target
    assert 'target_account_id="${2' in register_target


def test_register_target_publishes_alias_after_deploy():
    text = (_REPO_ROOT / "scripts/register_target.sh").read_text()
    append_pos = text.index("append_target_account_id.sh")
    deploy_pos = text.index("just deploy")
    register_pos = text.index("register_account.sh")
    assert append_pos < deploy_pos < register_pos
    assert "--external-id" not in text


def test_register_account_has_no_external_id_user_input():
    text = (_REPO_ROOT / "scripts/register_account.sh").read_text()
    assert "--external-id" not in text
    assert "derive_external_id.sh" in text


def test_register_account_writes_enable_apply_bool_default_false():
    text = (_REPO_ROOT / "scripts/register_account.sh").read_text()
    assert "--enable-apply" in text
    assert "Error: --enable-apply must be true or false" in text
    assert '"enable_apply": {"BOOL": enable_apply == "true"}' in text


def test_register_account_uses_shared_alias_validator():
    text = (_REPO_ROOT / "scripts/register_account.sh").read_text()
    assert 'validate_account_alias.sh" "$ALIAS"' in text


def test_account_set_apply_requires_existing_item_and_strict_bool():
    text = (_REPO_ROOT / "scripts/account_set_apply.sh").read_text()
    assert "attribute_exists(pk)" in text
    assert "update-item" in text
    assert "Error: --enable-apply must be true or false" in text
    assert "SET enable_apply = :val" in text
    assert "validate_account_alias.sh" in text
    assert "json.dumps" in text


def test_account_set_apply_rejects_invalid_alias_contract(tmp_path):
    aws = tmp_path / "aws"
    aws.write_text(
        """#!/usr/bin/env bash
case "$1" in
dynamodb)
  exit 0
  ;;
*)
  echo "unexpected aws call: $*" >&2
  exit 99
  ;;
esac
"""
    )
    aws.chmod(0o755)
    env = {"PATH": f"{tmp_path}:{__import__('os').environ['PATH']}"}
    empty = _run(_ACCOUNT_SET_APPLY, "--alias", "", "--enable-apply", "true", env=env)
    assert empty.returncode != 0
    for alias in ("   ", "a" * 129):
        result = _run(
            _ACCOUNT_SET_APPLY,
            "--alias",
            alias,
            "--enable-apply",
            "true",
            env=env,
        )
        assert result.returncode != 0
        assert "invalid alias" in result.stderr


def test_account_set_apply_key_json_encodes_malicious_alias_via_script(tmp_path):
    captured_key = tmp_path / "captured-key.json"
    aws = tmp_path / "aws"
    aws.write_text(
        f"""#!/usr/bin/env bash
case "$1" in
dynamodb)
  key=""
  while [ $# -gt 0 ]; do
    case "$1" in
    --key) key="$2"; shift 2 ;;
    *) shift ;;
    esac
  done
  printf '%s' "$key" >"{captured_key}"
  exit 0
  ;;
*)
  echo "unexpected aws call: $*" >&2
  exit 99
  ;;
esac
"""
    )
    aws.chmod(0o755)
    alias_payload = 'benign"},"pk":{"S":"repo"},"sk":{"S":"victim'
    result = _run(
        _ACCOUNT_SET_APPLY,
        "--alias",
        alias_payload,
        "--enable-apply",
        "true",
        env={"PATH": f"{tmp_path}:{__import__('os').environ['PATH']}"},
    )
    assert result.returncode == 0
    key = json.loads(captured_key.read_text())
    assert key == {"pk": {"S": "account"}, "sk": {"S": alias_payload}}


def test_account_set_apply_round_trips_register_account_style_alias(tmp_path):
    captured_key = tmp_path / "captured-key.json"
    aws = tmp_path / "aws"
    aws.write_text(
        f"""#!/usr/bin/env bash
case "$1" in
dynamodb)
  key=""
  while [ $# -gt 0 ]; do
    case "$1" in
    --key) key="$2"; shift 2 ;;
    *) shift ;;
    esac
  done
  printf '%s' "$key" >"{captured_key}"
  exit 0
  ;;
*)
  echo "unexpected aws call: $*" >&2
  exit 99
  ;;
esac
"""
    )
    aws.chmod(0o755)
    alias_payload = "prod/team"
    result = _run(
        _ACCOUNT_SET_APPLY,
        "--alias",
        alias_payload,
        "--enable-apply",
        "false",
        env={"PATH": f"{tmp_path}:{__import__('os').environ['PATH']}"},
    )
    assert result.returncode == 0
    key = json.loads(captured_key.read_text())
    assert key == {"pk": {"S": "account"}, "sk": {"S": alias_payload}}


def test_account_set_apply_register_account_style_injection_payload(tmp_path):
    captured_key = tmp_path / "captured-key.json"
    aws = tmp_path / "aws"
    aws.write_text(
        f"""#!/usr/bin/env bash
case "$1" in
dynamodb)
  key=""
  while [ $# -gt 0 ]; do
    case "$1" in
    --key) key="$2"; shift 2 ;;
    *) shift ;;
    esac
  done
  printf '%s' "$key" >"{captured_key}"
  exit 0
  ;;
*)
  echo "unexpected aws call: $*" >&2
  exit 99
  ;;
esac
"""
    )
    aws.chmod(0o755)
    alias_payload = (
        'qa"alias\\with{braces}\n,"account_id":{"S":"000000000000"},"sk":{"S":"pwn"}'
    )
    result = _run(
        _ACCOUNT_SET_APPLY,
        "--alias",
        alias_payload,
        "--enable-apply",
        "false",
        env={"PATH": f"{tmp_path}:{__import__('os').environ['PATH']}"},
    )
    assert result.returncode == 0
    key = json.loads(captured_key.read_text())
    assert key == {"pk": {"S": "account"}, "sk": {"S": alias_payload}}


def test_register_account_and_set_apply_share_alias_validator():
    register = (_REPO_ROOT / "scripts/register_account.sh").read_text()
    setter = (_REPO_ROOT / "scripts/account_set_apply.sh").read_text()
    assert 'validate_account_alias.sh" "$ALIAS"' in register
    assert 'validate_account_alias.sh" "$ALIAS"' in setter


def test_just_account_set_apply_forwards_positional_arguments_safely(tmp_path):
    marker = tmp_path / "openci-tf-just-injection"
    injection = f'x"; printf PWNED >"{marker}"; #'
    result = subprocess.run(
        ["just", "account-set-apply", injection, "true"],
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert not marker.exists(), result.stdout + result.stderr


def test_justfile_lists_account_set_apply_recipe():
    text = _JUSTFILE.read_text()
    assert "account-set-apply" in text
    assert "account_set_apply.sh" in text
    recipe = text.split("account-set-apply", 1)[1].split("target-onboard", 1)[0]
    assert "{{alias}}" not in recipe
    assert "{{value}}" not in recipe
    assert 'alias="${1' in recipe
    assert 'value="${2' in recipe


def test_account_set_apply_safe_alias_emits_single_literal_sk(tmp_path):
    captured_key = tmp_path / "captured-key.json"
    aws = tmp_path / "aws"
    aws.write_text(
        f"""#!/usr/bin/env bash
case "$1" in
dynamodb)
  key=""
  while [ $# -gt 0 ]; do
    case "$1" in
    --key) key="$2"; shift 2 ;;
    *) shift ;;
    esac
  done
  printf '%s' "$key" >"{captured_key}"
  exit 0
  ;;
*)
  echo "unexpected aws call: $*" >&2
  exit 99
  ;;
esac
"""
    )
    aws.chmod(0o755)
    alias_payload = "primary"
    result = _run(
        _ACCOUNT_SET_APPLY,
        "--alias",
        alias_payload,
        "--enable-apply",
        "true",
        env={"PATH": f"{tmp_path}:{__import__('os').environ['PATH']}"},
    )
    assert result.returncode == 0
    key = json.loads(captured_key.read_text())
    assert key == {"pk": {"S": "account"}, "sk": {"S": alias_payload}}


def test_target_onboard_verifies_bucket_before_mutation_without_lock_table():
    text = (_REPO_ROOT / "scripts/target_onboard.sh").read_text()
    identity_pos = text.index("aws sts get-caller-identity")
    bucket_pos = text.index("bucket_exists.sh")
    ssm_pos = text.index("ssm_config.sh set")
    connect_pos = text.index("just target-create-aws-readonly")
    assert identity_pos < bucket_pos < ssm_pos < connect_pos
    # Decision 27: no lock table exists or is probed anywhere.
    assert "dynamodb" not in text
    assert "tf-locks" not in text


def test_target_role_recipes_delegate_to_target_aws_role_script():
    text = _JUSTFILE.read_text()
    script = (_REPO_ROOT / "scripts/target_aws_role.sh").read_text()
    assert "target_aws_role.sh" in text
    assert "write_tfvars.sh" in script
    assert "generate_backend.sh" in script
    assert "refuses same-account" in script


def test_target_connect_terraform_derives_trust_external_id():
    module = _TARGET_CONNECT_MODULE.read_text()
    root_variables = (_TARGET_CONNECT_ROOT / "variables.tf").read_text()
    root_main = (_TARGET_CONNECT_ROOT / "main.tf").read_text()
    assert (
        'try(regex("^arn:aws:iam::([0-9]{12}):role/[^:/]+$", var.hub_lambda_exec_role_arn)[0], "")'
        in module
    )
    assert (
        'expected_hub_lambda_exec_role_arn = "arn:aws:iam::${local.hub_account_id}:role/${var.role_prefix}-hub-lambda-exec"'
        in module
    )
    assert (
        'external_id                       = "openci-tf-${substr(sha256("openci-tf:${local.hub_account_id}:${local.target_account_id}"), 0, 16)}"'
        in module
    )
    assert '"sts:ExternalId"   = local.external_id' in module
    assert 'AWS = "arn:aws:iam::${local.hub_account_id}:root"' in module
    assert '"aws:PrincipalArn" = local.hub_role_prefix_arns' in module
    assert (
        "var.hub_lambda_exec_role_arn == local.expected_hub_lambda_exec_role_arn"
        in module
    )
    assert "var.hub_lambda_exec_role_arn," not in module
    assert 'variable "external_id"' not in root_variables
    assert "external_id" not in root_main
