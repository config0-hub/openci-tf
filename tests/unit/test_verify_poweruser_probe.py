# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""verify.sh tri-state optional poweruser and boundary lifecycle probe semantics."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VERIFY = _REPO_ROOT / "scripts/verify.sh"
_POWERUSER_ROLE = "openci-tf-executor-poweruser"
_BOUNDARY_POLICY = "openci-tf-executor-poweruser-permissions-boundary"
_BOUNDARY_ARN = "arn:aws:iam::123456789012:policy/openci-tf-executor-poweruser-permissions-boundary"
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
    *,
    remove_poweruser: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{fake_aws}:{env['PATH']}"
    env["AWS_REGION"] = "us-east-1"
    if remove_poweruser is not None:
        env["OPENCI_TF_REMOVE_POWERUSER"] = remove_poweruser
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
    poweruser_role_rc: int = 254,
    poweruser_role_stderr: str = _NOSUCH_ROLE,
    poweruser_boundary_rc: int = 254,
    poweruser_boundary_stderr: str = _NOSUCH_POLICY,
    poweruser_boundary_arn: str = _BOUNDARY_ARN,
    attached_boundary_arn: str = "None",
    legacy_remote_rc: int = 254,
    legacy_remote_stderr: str = _NOSUCH_ROLE,
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
    {_POWERUSER_ROLE!r})
      if [ {poweruser_role_rc} -eq 0 ]; then
        if [ "$query" = "Role.PermissionsBoundary.PermissionsBoundaryArn" ]; then
          echo {attached_boundary_arn!r}
        else
          echo '{{"Role":{{"PermissionsBoundary":{{"PermissionsBoundaryArn":{attached_boundary_arn!r}}}}}}}'
        fi
      else
        printf '%s\\n' {poweruser_role_stderr!r} >&2
      fi
      exit {poweruser_role_rc}
      ;;
    openci-tf-executor-remote)
      printf '%s\\n' {legacy_remote_stderr!r} >&2
      exit {legacy_remote_rc}
      ;;
    openci-tf-executor-readonly)
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
      if [ {poweruser_boundary_rc} -eq 0 ]; then
        echo '{{"Policy":{{"Arn":{poweruser_boundary_arn!r}}}}}'
      else
        printf '%s\\n' {poweruser_boundary_stderr!r} >&2
      fi
      exit {poweruser_boundary_rc}
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


def test_verify_clean_fails_when_poweruser_probe_is_access_denied(tmp_path: Path) -> None:
    fake = _write_fake_aws(
        tmp_path,
        poweruser_role_rc=254,
        poweruser_role_stderr="An error occurred (AccessDenied) when calling the GetRole operation",
    )
    result = _run_verify("clean", fake)
    assert result.returncode != 0
    assert "indeterminate" in result.stdout.lower() or "FAIL" in result.stdout


def test_verify_clean_fails_when_poweruser_present(tmp_path: Path) -> None:
    fake = _write_fake_aws(
        tmp_path,
        poweruser_role_rc=0,
        poweruser_boundary_rc=0,
    )
    result = _run_verify("clean", fake, remove_poweruser="yes")
    assert result.returncode != 0
    assert "poweruser" in result.stdout.lower()


def test_verify_clean_passes_when_poweruser_absent(tmp_path: Path) -> None:
    fake = _write_fake_aws(tmp_path)
    result = _run_verify("clean", fake)
    assert "FAIL optional poweruser" not in result.stdout


def test_verify_clean_fails_when_boundary_policy_only(tmp_path: Path) -> None:
    fake = _write_fake_aws(
        tmp_path,
        poweruser_boundary_rc=0,
    )
    result = _run_verify("clean", fake)
    assert result.returncode != 0
    assert "boundary policy" in result.stdout.lower()


def test_verify_clean_fails_when_poweruser_probe_is_generic_404(tmp_path: Path) -> None:
    fake = _write_fake_aws(
        tmp_path,
        poweruser_role_rc=1,
        poweruser_role_stderr="404 Not Found from intermediary endpoint",
    )
    result = _run_verify("clean", fake)
    assert result.returncode != 0
    assert "indeterminate" in result.stdout.lower()


def test_verify_present_fails_when_poweruser_probe_is_throttled(tmp_path: Path) -> None:
    fake = _write_fake_aws(
        tmp_path,
        poweruser_role_rc=254,
        poweruser_role_stderr="An error occurred (Throttling) when calling the GetRole operation",
    )
    result = _run_verify("present", fake)
    assert result.returncode != 0
    assert "indeterminate" in result.stdout.lower()


def test_verify_present_accepts_absent_poweruser(tmp_path: Path) -> None:
    fake = _write_fake_aws(tmp_path)
    result = _run_verify("present", fake)
    assert "FAIL optional poweruser" not in result.stdout
    assert "absent (optional)" in result.stdout


def test_verify_present_rejects_the_legacy_poweruser_boundary(tmp_path: Path) -> None:
    fake = _write_fake_aws(
        tmp_path,
        poweruser_role_rc=0,
        poweruser_boundary_rc=0,
        attached_boundary_arn=_BOUNDARY_ARN,
    )
    result = _run_verify("present", fake)
    assert result.returncode != 0
    assert "forbidden boundary" in result.stdout.lower()


def test_verify_present_accepts_poweruser_without_boundary(tmp_path: Path) -> None:
    fake = _write_fake_aws(
        tmp_path,
        poweruser_role_rc=0,
        poweruser_boundary_rc=254,
        poweruser_boundary_stderr=_NOSUCH_POLICY,
    )
    result = _run_verify("present", fake)
    assert "present without boundary" in result.stdout
    assert "FAIL optional poweruser" not in result.stdout


def test_verify_present_fails_when_any_boundary_is_attached(tmp_path: Path) -> None:
    fake = _write_fake_aws(
        tmp_path,
        poweruser_role_rc=0,
        poweruser_boundary_rc=254,
        attached_boundary_arn="arn:aws:iam::123456789012:policy/wrong-boundary",
    )
    result = _run_verify("present", fake)
    assert result.returncode != 0
    assert "forbidden permissions boundary" in result.stdout.lower()


def test_verify_present_fails_when_boundary_policy_orphaned(tmp_path: Path) -> None:
    fake = _write_fake_aws(
        tmp_path,
        poweruser_boundary_rc=0,
    )
    result = _run_verify("present", fake)
    assert result.returncode != 0
    assert "without role" in result.stdout.lower()


def test_verify_present_accepts_present_legacy_remote(tmp_path: Path) -> None:
    fake = _write_fake_aws(
        tmp_path,
        legacy_remote_rc=0,
    )
    result = _run_verify("present", fake)
    assert "legacy role openci-tf-executor-remote (present)" in result.stdout


def test_verify_present_fails_on_legacy_remote_access_denied(tmp_path: Path) -> None:
    fake = _write_fake_aws(
        tmp_path,
        legacy_remote_rc=254,
        legacy_remote_stderr="An error occurred (AccessDenied) when calling the GetRole operation",
    )
    result = _run_verify("present", fake)
    assert result.returncode != 0
    assert "indeterminate" in result.stdout.lower()
