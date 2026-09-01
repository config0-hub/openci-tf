# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Intent token models for two-step apply/destroy."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FolderPlanPin:
    folder: str
    source_run_id: str
    plan_sha256: str
    plan_artifact_name: str
    account_id: str
    tf_runtime: str
    account_binding: dict[str, object]


@dataclass(frozen=True)
class IntentRecord:
    token: str
    trigger_id: str
    pr_number: int
    action: str
    source_run_id: str
    folders: tuple[str, ...]
    commit_hash: str
    folder_pins: tuple[FolderPlanPin, ...]
    expires_at: int
    used: bool = False
    pipeline: str | None = None
    step_index: int | None = None
    step_count: int | None = None
    pipeline_sha256: str | None = None
    requested_comment_id: int | None = None
    requested_comment_body: str | None = None
    intent_comment_id: int | None = None
    intent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "token": self.token,
            "trigger_id": self.trigger_id,
            "pr_number": self.pr_number,
            "action": self.action,
            "source_run_id": self.source_run_id,
            "folders": list(self.folders),
            "commit_hash": self.commit_hash,
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
                for pin in self.folder_pins
            ],
            "expires_at": self.expires_at,
            "used": self.used,
        }
        if self.pipeline is not None:
            payload["pipeline"] = self.pipeline
        if self.step_index is not None:
            payload["step_index"] = self.step_index
        if self.step_count is not None:
            payload["step_count"] = self.step_count
        if self.pipeline_sha256 is not None:
            payload["pipeline_sha256"] = self.pipeline_sha256
        if self.requested_comment_id is not None:
            payload["requested_comment_id"] = self.requested_comment_id
        if self.requested_comment_body is not None:
            payload["requested_comment_body"] = self.requested_comment_body
        if self.intent_comment_id is not None:
            payload["intent_comment_id"] = self.intent_comment_id
        if self.intent_id is not None:
            payload["intent_id"] = self.intent_id
        return payload


@dataclass
class IntentGateFailure:
    message: str
    folder: str | None = None


def intent_record_matches_current_request(
    record: IntentRecord | Mapping[str, object],
    *,
    trigger_id: str,
    pr_number: int,
    action: str,
) -> bool:
    """Return whether an intent record belongs to the current confirmation request."""
    if isinstance(record, IntentRecord):
        return (
            record.trigger_id == trigger_id
            and record.pr_number == pr_number
            and record.action == action
        )
    return (
        record.get("trigger_id") == trigger_id
        and record.get("pr_number") == pr_number
        and record.get("action") == action
    )


@dataclass
class IntentGateResult:
    ok: bool
    failures: list[IntentGateFailure] = field(default_factory=list)
    record: IntentRecord | None = None
