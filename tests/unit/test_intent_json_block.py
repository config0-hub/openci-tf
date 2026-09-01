# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Machine-readable JSON block in intent confirmation comments."""

from __future__ import annotations

import json
import re

import pytest

from src.domain.formatters.intent import intent_json_block, intent_success_comment
from src.domain.intent.models import IntentRecord
from src.domain.intent.token import mint_intent_id

_FENCED_JSON = re.compile(r"```json\n(?P<payload>\{.*\})\n```", re.DOTALL)


def _record(**overrides) -> IntentRecord:
    fields = {
        "token": "abcd1234",
        "trigger_id": "trig-1",
        "pr_number": 7,
        "action": "apply",
        "source_run_id": "run-1",
        "folders": ("infra/vpc",),
        "commit_hash": "a" * 40,
        "folder_pins": (),
        "expires_at": 1700000600,
        "intent_id": "intent-0011223344556677",
    }
    fields.update(overrides)
    return IntentRecord(**fields)


def _parse_block(body: str) -> dict:
    match = _FENCED_JSON.search(body)
    assert match is not None, f"no fenced JSON block in comment:\n{body}"
    return json.loads(match.group("payload"))


def test_pipeline_intent_comment_json_block_round_trips_every_field() -> None:
    record = _record(pipeline="data/primary", step_index=2, step_count=3)
    body = intent_success_comment(record, plan_summaries=["- `infra/vpc`: pinned plan from execution `run-1`"])
    parsed = _parse_block(body)
    assert parsed == {
        "intent_id": record.intent_id,
        "confirm_token": record.token,
        "expires_at": record.expires_at,
        "pipeline": record.pipeline,
        "step": record.step_index,
    }
    # The human-readable text stays; the block is additive.
    assert "## tf apply intent created" in body
    assert f"tf apply confirm {record.token}" in body


def test_non_pipeline_intent_comment_json_block_has_null_pipeline_and_step() -> None:
    record = _record(action="destroy")
    body = intent_success_comment(record, plan_summaries=[])
    parsed = _parse_block(body)
    assert parsed["intent_id"] == record.intent_id
    assert parsed["confirm_token"] == record.token
    assert parsed["expires_at"] == record.expires_at
    assert parsed["pipeline"] is None
    assert parsed["step"] is None


def test_intent_json_block_requires_intent_id() -> None:
    record = _record(intent_id=None)
    with pytest.raises(ValueError, match="intent_id"):
        intent_json_block(record)


def test_mint_intent_id_is_distinct_and_well_formed() -> None:
    intent_id = mint_intent_id()
    assert re.fullmatch(r"intent-[0-9a-f]{16}", intent_id)
    assert mint_intent_id() != intent_id
