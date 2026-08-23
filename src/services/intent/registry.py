"""Domain adapters for intent token persistence."""
from __future__ import annotations

from src.domain.intent.models import FolderPlanPin, IntentRecord
from src.platform.aws.intent_registry import (
    IntentRegistryError,
    get_intent_record,
    mark_intent_record_used,
    put_intent_record,
)


def _record_to_dict(record: IntentRecord) -> dict:
    payload = {
        "token": record.token,
        "trigger_id": record.trigger_id,
        "pr_number": record.pr_number,
        "action": record.action,
        "source_run_id": record.source_run_id,
        "folders": list(record.folders),
        "commit_hash": record.commit_hash,
        "folder_pins": [
            {
                "folder": pin.folder,
                "source_run_id": pin.source_run_id,
                "plan_sha256": pin.plan_sha256,
                "plan_artifact_name": pin.plan_artifact_name,
                "account_id": pin.account_id,
                "tf_runtime": pin.tf_runtime,
                "account_binding": pin.account_binding,
            }
            for pin in record.folder_pins
        ],
        "expires_at": record.expires_at,
        "used": record.used,
    }
    if record.pipeline is not None:
        payload["pipeline"] = record.pipeline
    if record.step_index is not None:
        payload["step_index"] = record.step_index
    if record.step_count is not None:
        payload["step_count"] = record.step_count
    if record.pipeline_sha256 is not None:
        payload["pipeline_sha256"] = record.pipeline_sha256
    return payload


def _dict_to_record(item: dict) -> IntentRecord:
    pins_raw = item.get("folder_pins") or []
    pins: list[FolderPlanPin] = []
    for pin in pins_raw:
        if not isinstance(pin, dict):
            raise IntentRegistryError("invalid folder_pins entry")
        pins.append(
            FolderPlanPin(
                folder=str(pin["folder"]),
                source_run_id=str(pin["source_run_id"]),
                plan_sha256=str(pin["plan_sha256"]),
                plan_artifact_name=str(pin["plan_artifact_name"]),
                account_id=str(pin["account_id"]),
                tf_runtime=str(pin["tf_runtime"]),
                account_binding=dict(pin["account_binding"]),
            )
        )
    return IntentRecord(
        token=str(item["token"]),
        trigger_id=str(item["trigger_id"]),
        pr_number=int(item["pr_number"]),
        action=str(item["action"]),
        source_run_id=str(item["source_run_id"]),
        folders=tuple(str(folder) for folder in item.get("folders") or []),
        commit_hash=str(item["commit_hash"]),
        folder_pins=tuple(pins),
        expires_at=int(item["expires_at"]),
        used=bool(item.get("used")),
        pipeline=str(item["pipeline"]) if item.get("pipeline") is not None else None,
        step_index=int(item["step_index"]) if item.get("step_index") is not None else None,
        step_count=int(item["step_count"]) if item.get("step_count") is not None else None,
        pipeline_sha256=str(item["pipeline_sha256"]) if item.get("pipeline_sha256") is not None else None,
    )


def put_intent(record: IntentRecord) -> None:
    put_intent_record(_record_to_dict(record))


def get_intent(token: str) -> IntentRecord | None:
    item = get_intent_record(token)
    if not item:
        return None
    return _dict_to_record(item)


def mark_intent_used(token: str, *, trigger_id: str, pr_number: int, now: int | None = None) -> IntentRecord:
    return _dict_to_record(mark_intent_record_used(token, trigger_id=trigger_id, pr_number=pr_number, now=now))
