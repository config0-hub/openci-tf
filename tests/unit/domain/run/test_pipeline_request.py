import pytest

from src.domain.run.request import (
    NotificationTarget,
    RunRequestValidationError,
    build_run_request,
    parse_run_request,
    run_request_folder_flags,
)

_FULL_SHA = "a" * 40


@pytest.mark.parametrize("folder_mode", [None, "pipeline", "PIPELINE"])
def test_parse_run_request_accepts_api_pipeline_mode(folder_mode: str | None) -> None:
    body = {
        "trigger_id": "trigger-1",
        "commit_hash": _FULL_SHA,
        "action": "plan",
        "pipeline": "data/primary",
        "idempotency_key": "idem-key-12345678",
    }
    if folder_mode is not None:
        body["folder_mode"] = folder_mode

    request = parse_run_request(body)

    assert request.folder_mode == "pipeline"
    assert request.pipeline == "data/primary"
    assert request.folders == []
    assert run_request_folder_flags(request) == ([], False, False)
    assert request.to_dict()["pipeline"] == "data/primary"


def test_build_run_request_accepts_github_pipeline_mode() -> None:
    request = build_run_request(
        trigger_id="trigger-1",
        commit_hash=_FULL_SHA,
        action="drift",
        folder_mode="pipeline",
        folders=[],
        idempotency_key="delivery-12345678",
        notification_target=NotificationTarget("github_pr", 7),
        ingress_source="github",
        pipeline="data/primary",
    )

    assert request.pipeline == "data/primary"
    assert request.notification_target.type == "github_pr"


def test_build_run_request_rejects_pipeline_destroy() -> None:
    with pytest.raises(RunRequestValidationError, match="destroy pipeline is not supported"):
        build_run_request(
            trigger_id="trigger-1",
            commit_hash=_FULL_SHA,
            action="destroy",
            folder_mode="pipeline",
            folders=[],
            idempotency_key="delivery-12345678",
            notification_target=NotificationTarget("github_pr", 7),
            ingress_source="github",
            pipeline="data/primary",
        )


def test_build_run_request_accepts_apply_pipeline_step() -> None:
    request = build_run_request(
        trigger_id="trigger-1",
        commit_hash=_FULL_SHA,
        action="apply",
        folder_mode="pipeline",
        folders=[],
        idempotency_key="delivery-12345678",
        notification_target=NotificationTarget("github_pr", 7),
        ingress_source="github",
        pipeline="data/primary",
        pipeline_step=2,
    )

    assert request.pipeline == "data/primary"
    assert request.pipeline_step == 2
    assert request.to_dict()["pipeline_step"] == 2


@pytest.mark.parametrize(
    "body,match",
    [
        (
            {
                "trigger_id": "trigger-1",
                "commit_hash": _FULL_SHA,
                "action": "plan",
                "folder_mode": "pipeline",
                "idempotency_key": "idem-key-12345678",
            },
            "pipeline is required",
        ),
        (
            {
                "trigger_id": "trigger-1",
                "commit_hash": _FULL_SHA,
                "action": "plan",
                "folder_mode": "pipeline",
                "pipeline": "../escape",
                "idempotency_key": "idem-key-12345678",
            },
            "pipeline is invalid",
        ),
        (
            {
                "trigger_id": "trigger-1",
                "commit_hash": _FULL_SHA,
                "action": "plan",
                "folder_mode": "pipeline",
                "pipeline": "all",
                "idempotency_key": "idem-key-12345678",
            },
            "pipeline name 'all' is reserved",
        ),
        (
            {
                "trigger_id": "trigger-1",
                "commit_hash": _FULL_SHA,
                "action": "plan",
                "folder_mode": "pipeline",
                "pipeline": "data/primary",
                "folders": ["infra/vpc"],
                "idempotency_key": "idem-key-12345678",
            },
            "pipeline is mutually exclusive with folders",
        ),
        (
            {
                "trigger_id": "trigger-1",
                "commit_hash": _FULL_SHA,
                "action": "report",
                "folder_mode": "pipeline",
                "pipeline": "data/primary",
                "idempotency_key": "idem-key-12345678",
            },
            "report is not supported",
        ),
        (
            {
                "trigger_id": "trigger-1",
                "commit_hash": _FULL_SHA,
                "action": "destroy",
                "folder_mode": "pipeline",
                "pipeline": "data/primary",
                "idempotency_key": "idem-key-12345678",
            },
            "destroy pipeline is not supported",
        ),
        (
            {
                "trigger_id": "trigger-1",
                "commit_hash": _FULL_SHA,
                "action": "apply",
                "folder_mode": "pipeline",
                "pipeline": "data/primary",
                "pipeline_step": 0,
                "idempotency_key": "idem-key-12345678",
            },
            "pipeline_step must be an integer",
        ),
        (
            {
                "trigger_id": "trigger-1",
                "commit_hash": _FULL_SHA,
                "action": "plan",
                "folder_mode": "explicit",
                "folders": ["infra/vpc"],
                "pipeline": "data/primary",
                "idempotency_key": "idem-key-12345678",
            },
            "folder_mode must be omitted or pipeline",
        ),
    ],
)
def test_parse_run_request_rejects_invalid_pipeline_payloads(
    body: dict[str, object], match: str
) -> None:
    with pytest.raises(RunRequestValidationError, match=match):
        parse_run_request(body)
