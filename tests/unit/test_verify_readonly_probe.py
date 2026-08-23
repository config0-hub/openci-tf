"""verify.sh tri-state readonly and boundary lifecycle probe semantics."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VERIFY = _REPO_ROOT / "scripts/verify.sh"
_READONLY_ROLE = "openci-tf-executor-readonly"
_BOUNDARY_POLICY = "openci-tf-executor-readonly-permissions-boundary"
_BOUNDARY_ARN = "arn:aws:iam::123456789012:policy/openci-tf-executor-readonly-permissions-boundary"
_ACCOUNT_ID = "123456789012"
_NOSUCH_ROLE = (
    "An error occurred (NoSuchEntity) when calling the GetRole operation: Role not found"
)
_NOSUCH_POLICY = (
    "An error occurred (NoSuchEntity) when calling the GetPolicy operation: Policy not found"
)


def _run_verify(
    mode: str,
    fake_aws: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{fake_aws}:{env['PATH']}"
    env["AWS_REGION"] = "us-east-1"
    return subprocess.run(
        [_VERIFY, mode],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_fake_aws(
    tmp_path: Path,
    *,
    readonly_role_rc: int = 254,
    readonly_role_stderr: str = _NOSUCH_ROLE,
    readonly_boundary_rc: int = 254,
    readonly_boundary_stderr: str = _NOSUCH_POLICY,
    readonly_boundary_arn: str = _BOUNDARY_ARN,
) -> Path:
    script = tmp_path / "aws"
    script.write_text(
        f"""#!/usr/bin/env bash
set -eo pipefail
role=""
policy_arn=""
if [ $# -lt 2 ]; then
  echo "ResourceNotFoundException" >&2
  exit 254
fi
if [ "$1" = iam ] && [ "$2" = get-role ]; then
  shift 2
  query=""
  while [ $# -gt 0 ]; do
    if [ "$1" = --role-name ]; then role="$2"; shift 2; continue; fi
    if [ "$1" = --query ]; then query="$2"; shift 2; continue; fi
    if [ "$1" = --output ]; then shift 2; continue; fi
    shift
  done
  case "$role" in
    {_READONLY_ROLE!r})
      if [ {readonly_role_rc} -eq 0 ]; then
        if [ "$query" = "Role.PermissionsBoundary.PermissionsBoundaryArn" ]; then
          echo {readonly_boundary_arn!r}
        else
          echo '{{"Role":{{"PermissionsBoundary":{{"PermissionsBoundaryArn":{readonly_boundary_arn!r}}}}}}}'
        fi
      else
        printf '%s\\n' {readonly_role_stderr!r} >&2
      fi
      exit {readonly_role_rc}
      ;;
    openci-tf-executor-poweruser|openci-tf-executor-remote|openci-tf-hub-lambda-exec)
      echo "ResourceNotFoundException: role not found" >&2
      exit 254
      ;;
  esac
fi
if [ "$1" = iam ] && [ "$2" = get-policy ]; then
  shift 2
  while [ $# -gt 0 ]; do
    if [ "$1" = --policy-arn ]; then policy_arn="$2"; shift 2; continue; fi
    shift
  done
  case "$policy_arn" in
    {_BOUNDARY_ARN!r})
      if [ {readonly_boundary_rc} -eq 0 ]; then
        echo '{{"Policy":{{"Arn":{readonly_boundary_arn!r}}}}}'
      else
        printf '%s\\n' {readonly_boundary_stderr!r} >&2
      fi
      exit {readonly_boundary_rc}
      ;;
  esac
fi
if [ "$1" = sts ] && [ "$2" = get-caller-identity ]; then
  echo "{_ACCOUNT_ID}"
  exit 0
fi
echo "ResourceNotFoundException" >&2
exit 254
"""
    )
    script.chmod(0o755)
    return tmp_path


def test_verify_present_fails_when_readonly_missing_boundary(tmp_path: Path) -> None:
    fake = _write_fake_aws(
        tmp_path,
        readonly_role_rc=0,
        readonly_boundary_rc=254,
        readonly_boundary_stderr=_NOSUCH_POLICY,
    )
    result = _run_verify("present", fake)
    assert result.returncode != 0
    assert "boundary policy" in result.stdout.lower()


def test_verify_present_accepts_readonly_with_matching_boundary(tmp_path: Path) -> None:
    fake = _write_fake_aws(
        tmp_path,
        readonly_role_rc=0,
        readonly_boundary_rc=0,
    )
    result = _run_verify("present", fake)
    assert "present with matching boundary" in result.stdout


def test_verify_clean_fails_when_boundary_policy_only(tmp_path: Path) -> None:
    fake = _write_fake_aws(
        tmp_path,
        readonly_boundary_rc=0,
    )
    result = _run_verify("clean", fake)
    assert result.returncode != 0
    assert "boundary policy" in result.stdout.lower()
