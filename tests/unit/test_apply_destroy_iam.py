"""IAM policy rendering for split executor roles."""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_readonly_module_has_read_only_attachment_only():
    source = (_REPO_ROOT / "infra/modules/executor-readonly/main.tf").read_text(encoding="utf-8")
    assert "executor_readonly_read_only" in source
    assert 'policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"' in source
    assert 'policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"' not in source
    assert "executor_readonly_permissions_boundary" in source
    assert "permissions_boundary" in source
    assert "DenyStateBucketNonBackendPrimitives" in source
    assert "DenyListBucketOutsideTargetsPrefix" in source


def test_poweruser_module_has_power_user_attachment_only():
    source = (_REPO_ROOT / "infra/modules/executor-poweruser/main.tf").read_text(encoding="utf-8")
    assert "executor_poweruser_power_user" in source
    assert 'policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"' in source
    assert 'policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"' not in source
    assert "DenyIamAndCloudFormationUnconditionally" in source
    assert "DenyStateBucketNonBackendPrimitives" in source
    assert "DenyLockTableNonBackendPrimitives" in source
    assert "permissions_boundary" not in source
    assert "executor_poweruser_permissions_boundary" not in source
    assert "DenyProtectedHubResources" in source
