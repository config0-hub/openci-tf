# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for pipeline_plan_focus artifact expectations in collect/manifest."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.domain.engine.manifest import (
    BucketSet,
    ManifestBinding,
    _artifact_names_for_action,
    _canonical_manifest_digest,
    build_failure_manifest,
    build_manifest,
    validate_manifest_schema,
)
from src.services.run_folder import collect
from src.services.run_folder.prepare_and_submit import _artifact_names as prepare_artifact_names
from tests.helpers.rendered_run_folder_asl import load_rendered_run_folder_definition
from tests.unit.manifest_fixtures import complete_plan_object_mocks


def _focused_plan_head_object(
    *,
    last_modified: datetime,
    include_tfsec: bool = False,
):
    plan_metadata, base_head, read_object_bytes = complete_plan_object_mocks(
        execution_id="1788119366426.7e34ddd6",
        repo_name="williaumwu/openci-test-gitops",
        run_id="1788119366426.7e34ddd6",
        commit_hash="1" * 40,
        account_id="123456789012",
        folder="terraform/primary/ap-northeast-1/04-cloudwatch-log-group",
        attempt=0,
        last_modified=last_modified,
    )

    def head_object(bucket: str, key: str):
        if not include_tfsec and any(
            segment in key for segment in ("tfsec", "infracost")
        ):
            return None
        return base_head(bucket, key)

    return plan_metadata, head_object, read_object_bytes


def test_regular_plan_still_requires_tfsec_and_infracost_artifacts():
    regular_prepare = prepare_artifact_names("plan", pipeline_plan_focus=False)
    regular_manifest = _artifact_names_for_action("plan", pipeline_plan_focus=False)
    assert "tfsec.json" in regular_prepare
    assert "infracost.json" in regular_prepare
    assert "tfsec.json" in regular_manifest
    assert "infracost.json" in regular_manifest
    assert "tfsec.json" not in _artifact_names_for_action("plan", pipeline_plan_focus=True)


def test_focused_plan_omits_tfsec_and_infracost_artifacts():
    assert prepare_artifact_names("plan", pipeline_plan_focus=True) == (
        "init.out",
        "validate.out",
        "tf/plan.out",
        "drift.json",
    )
    focused = _artifact_names_for_action("plan", pipeline_plan_focus=True)
    assert focused == ("init.out", "validate.out", "tf/plan.out")
    assert "tfsec.json" not in focused
    assert "infracost.json" not in focused


@pytest.mark.parametrize(
    ("action", "pipeline_plan_focus"),
    [
        ("plan", True),
        ("plan_destroy", True),
    ],
)
def test_focused_pipeline_preview_actions_match_prepare_and_submit(action, pipeline_plan_focus):
    if action == "plan_destroy":
        assert _artifact_names_for_action(action, pipeline_plan_focus=pipeline_plan_focus) == (
            "init.out",
            "validate.out",
            "destroy.plan.out",
        )
    else:
        assert _artifact_names_for_action(action, pipeline_plan_focus=pipeline_plan_focus) == (
            "init.out",
            "validate.out",
            "tf/plan.out",
        )


@pytest.mark.parametrize("action", ["plan", "plan_destroy"])
def test_focused_plan_first_checkpoint_uses_same_artifact_expectations(action):
    assert _artifact_names_for_action(action, pipeline_plan_focus=True) == _artifact_names_for_action(
        action, pipeline_plan_focus=True
    )
    if action == "plan":
        assert "tfsec.json" not in _artifact_names_for_action(action, pipeline_plan_focus=True)


def test_focused_plan_without_tfsec_reproduces_live_collect_failure_before_focus_flag():
    last_modified = datetime(2026, 8, 30, 18, 40, tzinfo=timezone.utc)
    plan_metadata, head_object, read_object_bytes = _focused_plan_head_object(
        last_modified=last_modified,
        include_tfsec=False,
    )
    with pytest.raises(ValueError, match="expected artifact missing: tfsec.json"):
        build_manifest(
            execution_id="1788119366426.7e34ddd6",
            buckets=BucketSet(
                tmp_bucket="tmp",
                done_bucket="done",
                package_bucket="pkg",
                done_uri="s3://done/1788119366426.7e34ddd6/done",
                package_uri="s3://pkg/1788119366426.7e34ddd6.zip",
            ),
            binding=ManifestBinding(
                run_id="1788119366426.7e34ddd6",
                repo_name="williaumwu/openci-test-gitops",
                commit_hash="1" * 40,
                account_id="123456789012",
                folder="terraform/primary/ap-northeast-1/04-cloudwatch-log-group",
                attempt=0,
            ),
            action="plan",
            head_object=head_object,
            read_object_bytes=read_object_bytes,
            plan_metadata=plan_metadata,
            plan_dimensions={
                "repo_name": "williaumwu/openci-test-gitops",
                "commit_hash": "1" * 40,
                "account_id": "123456789012",
                "folder": "terraform/primary/ap-northeast-1/04-cloudwatch-log-group",
                "run_id": "1788119366426.7e34ddd6",
            },
            generated_at_source=last_modified,
            pipeline_plan_focus=False,
        )


def test_focused_plan_builds_valid_manifest_without_tfsec_or_infracost():
    last_modified = datetime(2026, 8, 30, 18, 40, tzinfo=timezone.utc)
    plan_metadata, head_object, read_object_bytes = _focused_plan_head_object(
        last_modified=last_modified,
        include_tfsec=False,
    )
    manifest = build_manifest(
        execution_id="1788119366426.7e34ddd6",
        buckets=BucketSet(
            tmp_bucket="tmp",
            done_bucket="done",
            package_bucket="pkg",
            done_uri="s3://done/1788119366426.7e34ddd6/done",
            package_uri="s3://pkg/1788119366426.7e34ddd6.zip",
        ),
        binding=ManifestBinding(
            run_id="1788119366426.7e34ddd6",
            repo_name="williaumwu/openci-test-gitops",
            commit_hash="1" * 40,
            account_id="123456789012",
            folder="terraform/primary/ap-northeast-1/04-cloudwatch-log-group",
            attempt=0,
        ),
        action="plan",
        head_object=head_object,
        read_object_bytes=read_object_bytes,
        plan_metadata=plan_metadata,
        plan_dimensions={
            "repo_name": "williaumwu/openci-test-gitops",
            "commit_hash": "1" * 40,
            "account_id": "123456789012",
            "folder": "terraform/primary/ap-northeast-1/04-cloudwatch-log-group",
            "run_id": "1788119366426.7e34ddd6",
        },
        generated_at_source=last_modified,
        pipeline_plan_focus=True,
    )
    validate_manifest_schema(manifest, execution_id="1788119366426.7e34ddd6")
    entry_names = {entry["name"] for entry in manifest["entries"]}
    assert manifest["pipeline_plan_focus"] is True
    assert "tfsec.json" not in entry_names
    assert "infracost.json" not in entry_names
    assert {"init.out", "validate.out", "tf/plan.out", "plan.tfplan"}.issubset(entry_names)


def test_focused_collect_handler_passes_pipeline_plan_focus(monkeypatch):
    captured: dict = {}
    last_modified = datetime(2026, 8, 30, 18, 40, tzinfo=timezone.utc)
    plan_metadata, head_object, read_object_bytes = _focused_plan_head_object(
        last_modified=last_modified,
        include_tfsec=False,
    )

    def fake_build_manifest(**kwargs):
        captured.update(kwargs)
        return build_manifest(**kwargs)

    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "pkg")
    monkeypatch.setattr(collect, "build_manifest", fake_build_manifest)
    monkeypatch.setattr(collect, "head_object", head_object)
    monkeypatch.setattr(collect, "get_object_bytes", read_object_bytes)
    monkeypatch.setattr(collect, "get_bounded_json", lambda *_args, **_kwargs: plan_metadata)
    monkeypatch.setattr(collect, "put_json_create_only", lambda *_args, **_kwargs: "v1")
    collect.handler(
        {
            "exec_id": "1788119366426.7e34ddd6",
            "attempt": 0,
            "succeeded": True,
            "credential_expired": False,
            "steps": [],
            "error": None,
            "pointers": {"done": "s3://done/1788119366426.7e34ddd6/done"},
            "action": "plan",
            "repo_name": "williaumwu/openci-test-gitops",
            "commit_hash": "1" * 40,
            "account_id": "123456789012",
            "folder": "terraform/primary/ap-northeast-1/04-cloudwatch-log-group",
            "run_id": "1788119366426.7e34ddd6",
            "submitted_at": 1_700_000_000.0,
            "plan_metadata_uri": plan_metadata["metadata_s3_uri"],
            "pipeline_plan_focus": True,
            "step_index": 1,
        },
        object(),
    )
    assert captured["pipeline_plan_focus"] is True


def test_focused_plan_destroy_preview_keeps_destroy_plan_artifacts_only():
    focused_destroy = _artifact_names_for_action("plan_destroy", pipeline_plan_focus=True)
    assert focused_destroy == ("init.out", "validate.out", "destroy.plan.out")
    assert prepare_artifact_names("plan_destroy", pipeline_plan_focus=True) == focused_destroy
    assert "tfsec.json" not in focused_destroy


def test_focused_plan_failure_manifest_allows_plan_only_partial_entries():
    manifest = build_failure_manifest(
        execution_id="run.infra.0",
        tmp_bucket="tmp",
        done_bucket="done",
        package_bucket="pkg",
        action="plan",
        failure_reason="terraform plan failed",
        run_id="run",
        repo_name="org/repo",
        commit_hash="a" * 40,
        account_id="123456789012",
        folder="infra/a",
        attempt=0,
        generated_at_source=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    manifest["pipeline_plan_focus"] = True
    manifest["entries"] = [
        {
            "name": "init.out",
            "s3_uri": "s3://tmp/openci-tf/org/repo/run/infra/a/init.out",
            "content_type": "text/plain",
            "size": 1,
            "checksum": "a" * 64,
            "expires_at": "2099-01-01T00:00:00Z",
        },
        {
            "name": "validate.out",
            "s3_uri": "s3://tmp/openci-tf/org/repo/run/infra/a/validate.out",
            "content_type": "text/plain",
            "size": 1,
            "checksum": "a" * 64,
            "expires_at": "2099-01-01T00:00:00Z",
        },
        {
            "name": "tf/plan.out",
            "s3_uri": "s3://tmp/openci-tf/org/repo/run/infra/a/tf/plan.out",
            "content_type": "text/plain",
            "size": 1,
            "checksum": "a" * 64,
            "expires_at": "2099-01-01T00:00:00Z",
        },
    ]
    manifest["manifest_sha256"] = _canonical_manifest_digest(manifest)
    validate_manifest_schema(manifest, execution_id="run.infra.0")


def test_collect_asl_passes_pipeline_plan_focus():
    collect_parameters = load_rendered_run_folder_definition("read")["States"]["Collect"]["Parameters"]
    assert collect_parameters["pipeline_plan_focus.$"] == "$.pipeline_plan_focus"
    mutation_collect = load_rendered_run_folder_definition("apply")["States"]["CollectMutation"]["Parameters"]
    assert mutation_collect["pipeline_plan_focus.$"] == "$.pipeline_plan_focus"


def test_focused_plan_failure_manifest_rejects_tfsec_entries():
    manifest = build_failure_manifest(
        execution_id="run.infra.0",
        tmp_bucket="tmp",
        done_bucket="done",
        package_bucket="pkg",
        action="plan",
        failure_reason="terraform plan failed",
        run_id="run",
        repo_name="org/repo",
        commit_hash="a" * 40,
        account_id="123456789012",
        folder="infra/a",
        attempt=0,
        generated_at_source=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    manifest["pipeline_plan_focus"] = True
    manifest["entries"] = [
        {
            "name": "init.out",
            "s3_uri": "s3://tmp/openci-tf/org/repo/run/infra/a/init.out",
            "content_type": "text/plain",
            "size": 1,
            "checksum": "a" * 64,
            "expires_at": "2099-01-01T00:00:00Z",
        },
        {
            "name": "tfsec.json",
            "s3_uri": "s3://tmp/openci-tf/org/repo/run/infra/a/tfsec.json",
            "content_type": "application/json",
            "size": 2,
            "checksum": "a" * 64,
            "expires_at": "2099-01-01T00:00:00Z",
        },
    ]
    manifest["manifest_sha256"] = _canonical_manifest_digest(manifest)
    with pytest.raises(ValueError, match="unexpected entries"):
        validate_manifest_schema(manifest, execution_id="run.infra.0")
