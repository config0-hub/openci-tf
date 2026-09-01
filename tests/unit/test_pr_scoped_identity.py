# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for PR-scoped identity, pointers, and outer execution IDs."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from src.domain.engine.artifact_paths import (
    execution_artifact_prefix,
    parse_execution_pointer,
    pr_pointer_key,
    report_all_pointer_key,
    serialize_execution_pointer,
)
from src.domain.engine.outer_execution_id import (
    compose_outer_run_id,
    is_legacy_uuid_run_id,
    parse_outer_run_epoch,
    validate_outer_run_id,
)
from src.domain.github.comment_object_id import (
    comment_type_for_action,
    format_comment_object_marker,
    legacy_opaque_tag,
    parse_comment_object_marker,
    should_emit_comment_object_marker,
)
from src.domain.engine.pointer_publish import publish_execution_pointer
from src.domain.engine.artifact_paths import pointer_type_for_action


def test_compose_outer_run_id_format_and_epoch_parse():
    run_id = compose_outer_run_id("org/repo", "plan", epoch_ms=1_700_000_000_123)
    assert run_id == "1700000000123." + run_id.split(".", 1)[1]
    assert len(run_id.split(".", 1)[1]) == 8
    assert parse_outer_run_epoch(run_id) == 1_700_000_000_123


def test_validate_outer_run_id_accepts_legacy_uuid():
    legacy = "a" * 32
    assert validate_outer_run_id(legacy) == legacy
    assert is_legacy_uuid_run_id(legacy)


def test_comment_object_marker_round_trip():
    marker = format_comment_object_marker("org/repo", 42, "plan", "infra/vpc")
    parsed = parse_comment_object_marker(marker)
    assert parsed == {
        "repo_name": "org/repo",
        "pr_number": "42",
        "comment_type": "plan",
        "folder": "infra/vpc",
    }


def test_report_all_marker_uses_all_folder():
    marker = format_comment_object_marker("org/repo", 1, "report-all", "ignored")
    assert marker.endswith("::report-all:all")


def test_plan_destroy_uses_destroy_comment_and_pointer_class():
    assert comment_type_for_action("plan_destroy") == "destroy"
    assert pointer_type_for_action("plan_destroy") == "destroy"


def test_terminal_marker_policy_keeps_destroy_plan_replaceable():
    assert should_emit_comment_object_marker("apply", terminal=False) is True
    assert should_emit_comment_object_marker("destroy", terminal=False) is True
    assert should_emit_comment_object_marker("apply", terminal=True) is False
    assert should_emit_comment_object_marker("destroy", terminal=True) is False
    assert should_emit_comment_object_marker("plan", terminal=True) is True
    assert should_emit_comment_object_marker("drift", terminal=True) is True
    assert should_emit_comment_object_marker("report", terminal=True) is True
    assert should_emit_comment_object_marker("plan_destroy", terminal=True) is True


def test_pointer_key_and_body_round_trip():
    key = pr_pointer_key(
        repo_name="org/repo", pr_number=9, folder_path="infra/vpc", pointer_type="plan"
    )
    assert key == "openci-tf/org/repo/pr-9/infra/vpc/plan.env"
    body = serialize_execution_pointer("1700000000000.deadbeef").decode()
    assert parse_execution_pointer(body) == "1700000000000.deadbeef"


def test_pointer_body_rejects_extra_lines():
    with pytest.raises(ValueError, match="exactly"):
        parse_execution_pointer("EXECUTION_ID=1700000000000.deadbeef\nEXTRA=1\n")


def test_execution_artifact_prefix_layout():
    prefix = execution_artifact_prefix(
        repo_name="org/repo",
        pr_number=3,
        execution_id="1700000000000.deadbeef",
        pointer_type="plan",
        folder_path="infra/vpc",
    )
    assert prefix.endswith("/plan/infra/vpc/")


def test_report_all_pointer_key():
    assert (
        report_all_pointer_key(repo_name="org/repo", pr_number=2)
        == "openci-tf/org/repo/pr-2/report-all.env"
    )


def test_pointer_publish_missing_writes_proposed():
    writes: list[tuple[str, bytes]] = []

    def put_text(*, bucket: str, key: str, body: bytes, if_match: str | None = None):
        assert bucket == "tmp"
        writes.append((key, body))

    result = publish_execution_pointer(
        bucket="tmp",
        key="openci-tf/org/repo/pr-1/infra/vpc/plan.env",
        execution_id="1700000000002.11111111",
        head_object=lambda *_: None,
        put_text=put_text,
        get_text=lambda *_: None,
    )
    assert result.updated is True
    assert writes[0][1] == serialize_execution_pointer("1700000000002.11111111")


def test_pointer_publish_skips_older_epoch():
    current = "1700000000005.aaaaaaaa"
    calls = {"puts": 0}

    def put_text(**_kwargs):
        calls["puts"] += 1

    result = publish_execution_pointer(
        bucket="tmp",
        key="k",
        execution_id="1700000000001.bbbbbbbb",
        head_object=lambda *_: {"etag": '"1"'},
        put_text=put_text,
        get_text=lambda *_: serialize_execution_pointer(current),
    )
    assert result.skipped_stale is True
    assert result.updated is False
    assert calls["puts"] == 0


def test_pointer_publish_idempotent_equal_id():
    execution_id = "1700000000005.aaaaaaaa"
    result = publish_execution_pointer(
        bucket="tmp",
        key="k",
        execution_id=execution_id,
        head_object=lambda *_: {"etag": '"1"'},
        put_text=Mock(),
        get_text=lambda *_: serialize_execution_pointer(execution_id),
    )
    assert result.updated is False
    assert result.skipped_stale is False


def test_legacy_opaque_tag_differs_by_type_suffix():
    plan_tag = legacy_opaque_tag("org/repo", 1, "folder-infra/a")
    report_tag = legacy_opaque_tag("org/repo", 1, "folder-infra/b")
    assert plan_tag != report_tag


def test_pr_plan_collect_to_apply_presign_uses_scoped_plan_key(monkeypatch):
    """PR plan collect -> scoped manifest -> plan.env -> lookup -> apply presign."""
    from src.domain.engine.artifact_paths import (
        build_folder_artifact_keys_for_run,
        expected_plan_artifact_uris,
        pr_pointer_key,
    )
    from src.domain.intent.plan_lookup import _plan_run_from_pointer
    from src.services.run_folder import collect, prepare_and_submit as prepare_handler
    from tests.unit.manifest_fixtures import complete_plan_object_mocks

    repo_name = "org/repo"
    run_id = "1700000000100.deadbeef"
    folder = "infra/a"
    exec_id = "run.abc.0"
    pr_number = 7
    keys = build_folder_artifact_keys_for_run(
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        pr_number=pr_number,
        pointer_type="plan",
    )
    published: list[tuple[str, str]] = []
    committed_manifest: dict = {}

    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(
        collect,
        "put_json_create_only",
        lambda _bucket, _key, manifest: committed_manifest.update(manifest) or "v1",
    )
    monkeypatch.setattr(
        collect,
        "publish_execution_pointer",
        lambda **kwargs: published.append((kwargs["key"], kwargs["execution_id"])),
    )
    monkeypatch.setattr(collect, "put_folder_attempt", lambda **_kwargs: None)
    monkeypatch.setattr(
        collect,
        "resolve_run_artifact_layout",
        lambda **_kwargs: __import__(
            "src.domain.engine.run_artifact_layout", fromlist=["RunArtifactLayout"]
        ).RunArtifactLayout(
            folder_keys=keys,
            pr_number=pr_number,
            pointer_type="plan",
        ),
    )
    _lm = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    plan_metadata, head_object, read_object_bytes = complete_plan_object_mocks(
        execution_id=exec_id,
        repo_name=repo_name,
        run_id=run_id,
        commit_hash="a" * 40,
        account_id="123456789012",
        folder=folder,
        attempt=0,
        last_modified=_lm,
        pr_number=pr_number,
        pointer_type="plan",
    )

    def head_without_drift(bucket: str, key: str):
        if key == keys.drift_json:
            return None
        return head_object(bucket, key)

    monkeypatch.setattr(collect, "head_object", head_without_drift)
    monkeypatch.setattr(collect, "get_object_bytes", read_object_bytes)
    monkeypatch.setattr(
        collect, "get_bounded_json", lambda *_args, **_kwargs: plan_metadata
    )
    expected = expected_plan_artifact_uris(
        bucket="tmp",
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        pr_number=pr_number,
        pointer_type="plan",
    )
    collected = collect.handler(
        {
            "exec_id": exec_id,
            "succeeded": True,
            "steps": [],
            "pointers": {
                "done": f"s3://done/{exec_id}/done",
                "plan_metadata": expected.metadata,
            },
            "action": "plan",
            "repo_name": repo_name,
            "commit_hash": "a" * 40,
            "folder": folder,
            "account_id": "123456789012",
            "run_id": run_id,
            "attempt": 0,
            "plan_metadata_uri": expected.metadata,
        },
        object(),
    )
    assert collected["succeeded"] is True
    assert published[0][0] == pr_pointer_key(
        repo_name=repo_name,
        pr_number=pr_number,
        folder_path=folder,
        pointer_type="plan",
    )

    presigned: list[str] = []

    def capture_presign_get(_bucket: str, key: str, _expiry: int) -> str:
        presigned.append(key)
        return f"get://{key}"

    monkeypatch.setattr(prepare_handler.s3, "presign_get", capture_presign_get)
    monkeypatch.setattr(
        "src.services.run_folder.secrets._pr_number_for_source_run",
        lambda _source_run_id: pr_number,
    )
    secrets = prepare_handler._pinned_plan_secrets(
        action="apply",
        bucket="tmp",
        repo_name=repo_name,
        source_run_id=run_id,
        folder=folder,
        plan_sha256=plan_metadata["sha256"],
        plan_artifact_name="plan.tfplan",
        expiry=3600,
    )
    assert presigned == [keys.plan_tfplan]
    assert secrets["PINNED_PLAN_GET_URL"] == f"get://{keys.plan_tfplan}"

    destroy_keys = build_folder_artifact_keys_for_run(
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        pr_number=pr_number,
        pointer_type="destroy",
    )
    presigned.clear()
    destroy_secrets = prepare_handler._pinned_plan_secrets(
        action="destroy",
        bucket="tmp",
        repo_name=repo_name,
        source_run_id=run_id,
        folder=folder,
        plan_sha256=plan_metadata["sha256"],
        plan_artifact_name="destroy.plan.tfplan",
        expiry=3600,
    )
    assert presigned == [destroy_keys.destroy_plan_tfplan]
    assert destroy_secrets["PINNED_PLAN_GET_URL"] == f"get://{destroy_keys.destroy_plan_tfplan}"

    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.get_bounded_json",
        lambda _bucket, key, _max_bytes: (
            committed_manifest if key.endswith("manifest.json") else None
        ),
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.head_object",
        head_without_drift,
    )

    def lookup_object_bytes(_bucket: str, key: str, max_bytes: int) -> bytes | None:
        if key.endswith("plan.env"):
            return serialize_execution_pointer(run_id)
        return read_object_bytes(_bucket, key, max_bytes)

    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.get_object_bytes",
        lookup_object_bytes,
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.get_folder_record",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "manifest_sha256": collected["manifest_sha256"],
        },
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.get_run",
        lambda *_args, **_kwargs: {"repo_name": repo_name},
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup._metadata_expired",
        lambda _expires_at: False,
    )
    pointer_match = _plan_run_from_pointer(
        repo_name=repo_name,
        pr_number=pr_number,
        folder=folder,
        mutation_action="apply",
        commit_hash="a" * 40,
        account_id="123456789012",
        expected_tf_runtime="tofu:1.10.6",
    )
    assert pointer_match is not None
    assert pointer_match["run_id"] == run_id
    assert pointer_match["plan_sha256"] == plan_metadata["sha256"]


def test_render_lists_scoped_artifacts_and_shows_plan_env(monkeypatch):
    from src.domain.engine.artifact_paths import (
        build_folder_artifact_keys_for_run,
        pr_pointer_key,
    )
    from src.domain.formatters.artifacts import folder_comment
    from src.services.render import handler as render_handler

    repo_name = "org/repo"
    run_id = "1700000000200.deadbeef"
    folder = "infra/a"
    pr_number = 3
    keys = build_folder_artifact_keys_for_run(
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        pr_number=pr_number,
        pointer_type="plan",
    )
    listed: list[str] = []

    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(
        render_handler,
        "list_text_prefix",
        lambda _bucket, prefix, *_args, **_kwargs: (
            listed.append(prefix)
            or {
                "init.out": "ok",
                "validate.out": "ok",
                "tf/plan.out": "Plan: 0 to add, 0 to change, 0 to destroy",
            }
        ),
    )
    prefix = render_handler._artifact_list_prefix(
        repo_name=repo_name,
        run_id=run_id,
        folder=folder,
        action="plan",
        pr_number=pr_number,
    )
    assert prefix == keys.prefix
    render_handler.list_text_prefix("tmp", prefix, 1, frozenset())
    assert listed == [keys.prefix]

    plan_env = pr_pointer_key(
        repo_name=repo_name,
        pr_number=pr_number,
        folder_path=folder,
        pointer_type="plan",
    )
    body = folder_comment(
        folder,
        {"succeeded": True, "account_id": "123456789012"},
        {
            "init.out": "init",
            "validate.out": "ok",
            "tf/plan.out": "Plan: 0 to add, 0 to change, 0 to destroy",
        },
        run_id=run_id,
        repo_name=repo_name,
        pr_number=pr_number,
        approved_plan_pointer_key=plan_env,
        tmp_bucket="tmp",
        region="us-east-1",
    )
    assert plan_env in body
    assert "latest/" not in body


def test_report_all_pointer_publishes_on_success_not_failure(monkeypatch):
    from src.services.render import handler as render_handler
    from src.services.render import artifact_access as render_artifact_access

    published: list[tuple[str, str]] = []

    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(
        render_artifact_access,
        "publish_execution_pointer",
        lambda **kwargs: published.append((kwargs["key"], kwargs["execution_id"])),
    )
    render_handler._publish_report_all_pointer(
        repo_name="org/repo",
        pr_number=5,
        run_id="1700000000300.deadbeef",
        terminal="succeeded",
    )
    assert published == [
        ("openci-tf/org/repo/pr-5/report-all.env", "1700000000300.deadbeef")
    ]
    published.clear()
    render_handler._publish_report_all_pointer(
        repo_name="org/repo",
        pr_number=5,
        run_id="1700000000300.deadbeef",
        terminal="failed",
    )
    assert published == []
