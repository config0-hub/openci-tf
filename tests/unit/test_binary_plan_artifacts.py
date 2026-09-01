# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path
from urllib.parse import unquote

import pytest

from src.domain.cmd_builder.script_generator import ScriptParams, render
from src.domain.engine.artifact_paths import (
    build_plan_artifact_keys,
    expected_plan_artifact_uris,
    folder_artifact_prefix,
)
from src.domain.engine.plan_artifacts import validate_plan_artifact_metadata
from src.domain.formatters.artifacts import folder_comment
from src.platform.aws import s3 as s3_platform
from src.services.render import artifact_access as render_artifact_access
from src.services.render import handler as render_handler

_FULL_SHA = "a" * 40
_ACCOUNT = "123456789012"
_RUN_ID = "trigger-1-42-dabc123456"


def test_plan_and_report_scripts_upload_binary_plan_sidecars_and_drift_does_not() -> None:
    for verb in ("plan", "report"):
        script = render(ScriptParams(verb=verb, execution_target="lambda"))
        assert 'plan_file="$plan_dir/plan.tfplan"' in script
        assert 'tofu plan -out="$plan_file" -no-color' in script
        assert "PLAN_BINARY_PUT_URL" in script
        assert "PLAN_SHA256_PUT_URL" in script
        assert "PLAN_METADATA_PUT_URL" in script
        assert "upload_plan_binary_artifact" in script
        assert "-H 'If-None-Match: *'" not in script
        assert "init.out" in script
        assert "tf/plan.out" in script
    drift = render(ScriptParams(verb="drift", execution_target="lambda"))
    assert "PLAN_BINARY_PUT_URL" not in drift
    assert "plan.tfplan" not in drift
    assert " -out=" not in drift


def test_plan_out_path_cannot_be_overridden_by_extra_flags() -> None:
    with pytest.raises(ValueError, match="managed plan -out"):
        render(ScriptParams(verb="plan", execution_target="lambda", extra_flags=("-out=evil",)))


def test_artifact_path_builder_rejects_traversal_and_uses_run_scoped_prefix() -> None:
    with pytest.raises(ValueError, match="\\.\\."):
        folder_artifact_prefix(repo_name="org/repo", run_id=_RUN_ID, folder_path="../infra/app")
    keys = build_plan_artifact_keys(repo_name="org/repo", run_id=_RUN_ID, folder_path="infra/app")
    assert keys.plan == f"openci-tf/org/repo/{_RUN_ID}/infra/app/tf/plan.tfplan"
    assert keys.checksum.endswith("tf/plan.tfplan.sha256")
    assert keys.metadata.endswith("tf/plan-metadata.json")
    other_run = build_plan_artifact_keys(repo_name="org/repo", run_id="other-run", folder_path="infra/app")
    assert keys.plan != other_run.plan


def test_presign_put_allows_retry_overwrite() -> None:
    captured = {}

    class Client:
        def generate_presigned_url(self, method, *, Params, ExpiresIn):
            captured.update({"method": method, "Params": Params, "ExpiresIn": ExpiresIn})
            return "https://signed"

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(s3_platform, "_presign_client", lambda: Client())
    try:
        assert s3_platform.presign_put("tmp", "openci-tf/org/repo/run/tf/plan.tfplan", 900) == "https://signed"
        assert captured["Params"]["Key"] == "openci-tf/org/repo/run/tf/plan.tfplan"
        assert "IfNoneMatch" not in captured["Params"]
    finally:
        monkeypatch.undo()


def test_plan_artifact_metadata_validation_binds_all_run_dimensions() -> None:
    expected = expected_plan_artifact_uris(
        bucket="tmp", repo_name="org/repo", run_id=_RUN_ID, folder_path="infra/app"
    )
    metadata = {
        "repo": "org/repo",
        "run_id": _RUN_ID,
        "pinned_sha": _FULL_SHA,
        "account_id": _ACCOUNT,
        "folder": "infra/app",
        "action": "plan",
        "opentofu_runtime": "tofu:1.10.6",
        "created_at": "2026-08-10T00:00:00Z",
        "expires_at": "2026-08-11T00:00:00Z",
        "expires_after_days": 1,
        "plan_s3_uri": expected.plan,
        "sha256_s3_uri": expected.checksum,
        "metadata_s3_uri": expected.metadata,
        "sha256": "b" * 64,
    }
    assert validate_plan_artifact_metadata(
        metadata=metadata,
        bucket="tmp",
        repo_name="org/repo",
        run_id=_RUN_ID,
        commit_hash=_FULL_SHA,
        account_id=_ACCOUNT,
        folder="infra/app",
        action="plan",
    ) is metadata


def test_renderer_requires_exact_expected_metadata_pointer_before_loading(monkeypatch) -> None:
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    expected = expected_plan_artifact_uris(
        bucket="tmp", repo_name="org/repo", run_id=_RUN_ID, folder_path="infra/app"
    )
    metadata = {
        "repo": "org/repo",
        "run_id": _RUN_ID,
        "pinned_sha": _FULL_SHA,
        "account_id": _ACCOUNT,
        "folder": "infra/app",
        "action": "plan",
        "opentofu_runtime": "tofu:1.10.6",
        "created_at": "2026-08-10T00:00:00Z",
        "expires_at": "2026-08-11T00:00:00Z",
        "expires_after_days": 1,
        "plan_s3_uri": expected.plan,
        "sha256_s3_uri": expected.checksum,
        "metadata_s3_uri": expected.metadata,
        "sha256": "b" * 64,
    }
    loads = []
    monkeypatch.setattr(render_artifact_access, "get_bounded_json", lambda bucket, key, limit: loads.append((bucket, key, limit)) or metadata)
    outcome = {
        "succeeded": True,
        "account_id": _ACCOUNT,
        "folder": "infra/app",
        "execution_id": "run.abc.0",
        "attempt": 0,
        "pointers": {"plan_metadata": expected.metadata},
    }
    assert render_handler._plan_artifact_metadata(outcome, "plan", {"repo_name": "org/repo", "commit_hash": _FULL_SHA}, _RUN_ID) == metadata
    assert loads == [("tmp", expected.metadata.removeprefix("s3://tmp/"), 4096)]


def test_renderer_metadata_required_only_for_successful_plan_and_report(monkeypatch) -> None:
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    success = {"succeeded": True, "account_id": _ACCOUNT, "folder": "infra/app", "execution_id": "run.abc.0", "attempt": 0, "pointers": {}}
    for action in ("plan", "report"):
        with pytest.raises(ValueError):
            render_handler._plan_artifact_metadata(success, action, {"repo_name": "org/repo", "commit_hash": _FULL_SHA}, _RUN_ID)
    for action, outcome in (
        ("drift", success),
        ("plan", {**success, "succeeded": False}),
        ("report", {**success, "status": "infrastructure_error"}),
    ):
        assert render_handler._plan_artifact_metadata(outcome, action, {"repo_name": "org/repo", "commit_hash": _FULL_SHA}, _RUN_ID) is None


def test_tmp_bucket_has_openci_tf_lifecycle_rule() -> None:
    source = Path("infra/foundation/s3.tf").read_text()
    assert 'openci_tf_prefix    = "openci-tf/"' in source
    assert 'id     = "openci-tf-artifact-retention"' in source
    assert 'filter { prefix = local.openci_tf_prefix }' in source


def test_comment_shows_run_id_and_plan_pointers_only() -> None:
    artifacts = {
        "init.out": "init",
        "validate.out": "ok",
        "tf/plan.out": "Plan: 0 to add, 0 to change, 0 to destroy",
        "tf/plan.tfplan": "",
    }
    rendered = folder_comment(
        "infra/app",
        {"succeeded": True, "account_id": _ACCOUNT},
        artifacts,
        run_id=_RUN_ID,
        repo_name="org/repo",
        existing_names=frozenset(artifacts),
        tmp_bucket="tmp-bucket",
        region="us-east-1",
        hub_account_id="999999999999",
        identity_center_start_url="https://d-9567aa6b98.awsapps.com/start",
        identity_center_role_name="AWSAdministratorAccess",
    )
    assert "> <summary>Artifacts</summary>" in rendered
    assert "[plan.tfplan]" in rendered
    assert _RUN_ID in rendered
    assert (
        f"openci-tf/org/repo/{_RUN_ID}/infra/app/tf/plan.tfplan" in unquote(rendered)
    )
    assert "SHA-256" not in rendered
    assert "Expires" not in rendered
    assert "Plan Artifact" not in rendered
