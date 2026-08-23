"""Confirm apply/destroy intent tokens and start execution runs."""
from __future__ import annotations

from typing import Any

from src.domain.intent.gates import evaluate_confirm_gates
from src.domain.intent.models import IntentGateFailure
from src.platform.aws.intent_registry import IntentTokenConflictError
from src.services.intent.registry import get_intent, mark_intent_used


def confirm_intent(
    *,
    token: str,
    action: str,
    commit_hash: str,
    trigger_id: str,
    pr_number: int,
    repo_name: str,
) -> tuple[list[IntentGateFailure], dict[str, Any] | None]:
    record = get_intent(token)
    if record is None:
        return [IntentGateFailure("unknown confirmation token")], None
    if record.action != action:
        return [IntentGateFailure(f"token is for tf {record.action}, not tf {action}")], None
    failures = evaluate_confirm_gates(
        record=record,
        commit_hash=commit_hash,
        trigger_id=trigger_id,
        pr_number=pr_number,
        repo_name=repo_name,
    )
    if failures:
        return failures, None
    try:
        confirmed = mark_intent_used(token, trigger_id=trigger_id, pr_number=pr_number)
    except IntentTokenConflictError as error:
        return [IntentGateFailure(str(error))], None
    folder_pins = {
        pin.folder: {
            "source_run_id": pin.source_run_id,
            "plan_sha256": pin.plan_sha256,
            "plan_artifact_name": pin.plan_artifact_name,
            "account_id": pin.account_id,
            "tf_runtime": pin.tf_runtime,
            "account_binding": pin.account_binding,
        }
        for pin in confirmed.folder_pins
    }
    return [], {
        "action": confirmed.action,
        "folders": list(confirmed.folders),
        "commit_hash": confirmed.commit_hash,
        "source_plan_run_id": confirmed.source_run_id,
        "folder_pins": folder_pins,
        "intent_token": confirmed.token,
        "pipeline": confirmed.pipeline,
        "step_index": confirmed.step_index,
        "step_count": confirmed.step_count,
        "pipeline_sha256": confirmed.pipeline_sha256,
    }
