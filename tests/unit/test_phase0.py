# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
import base64
from unittest.mock import Mock

import pytest

from src.core.errors import (
    BudgetUnmintableError,
    ConfigValidationError,
    LockHeldError,
    PayloadTooLargeError,
)
from src.core.models import FolderConfig
from src.domain.accounts.budget import compute_budget, compute_ttl
from src.domain.cmd_builder.cmd_resolver import RESOLVERS, resolve_commands
from src.domain.command.grammar import ParseError, parse_command
from src.domain.config.folder_config import parse_folder_config
from src.domain.engine.byte_budget import (
    MAX_SERIALIZED_PAYLOAD_BYTES,
    check_payload_size,
)
from src.domain.engine.execution_id import compose_execution_id
from src.domain.engine.payload import EnginePayload
from src.domain.locks.run_lock import acquire, release


@pytest.mark.parametrize("text,action,all_flag,affected_flag,folders", [
    ("tf report", "report", True, False, []),
    ("tf plan infra/vpc,infra/rds", "plan", False, False, ["infra/vpc", "infra/rds"]),
])
def test_safe_grammar_accepts(text, action, all_flag, affected_flag, folders):
    command = parse_command(text)
    assert command.action == action
    assert command.all_flag == all_flag
    assert command.affected_flag == affected_flag
    assert command.folders == folders

@pytest.mark.parametrize("text", [
    "plan openci-tf x",
    "tf unknown x",
    "tf plan",
    "tf plan all",
    "tf report all",
    "tf report infra/vpc",
    "tf drift infra/vpc",
    "tf plan ,",
    "tf plan x extra extra",
    "tf validate x",
])
def test_grammar_rejects_invalid(text):
    with pytest.raises(ParseError): parse_command(text)
@pytest.mark.parametrize("verb", ["apply", "destroy"])
def test_mutating_commands_parse_and_resolve(verb):
    assert parse_command(f"tf {verb} x").action == verb
    config = FolderConfig(account_alias="a", apply=True, destroy=True)
    assert resolve_commands(verb, config).verb == verb


def test_registry_includes_mutation_actions():
    assert set(RESOLVERS) == {"plan", "plan_destroy", "drift", "report", "apply", "destroy"}
def test_engine_payload_and_size_contract():
    payload = EnginePayload("id", "s3://bucket/package", "kms", None, base64.b64encode(b'[\"bash ./openci_tf_run.sh\"]').decode(), "s3://bucket/done", "lambda", 900)
    payload.validate(); check_payload_size(b"x" * MAX_SERIALIZED_PAYLOAD_BYTES)
    with pytest.raises(PayloadTooLargeError): check_payload_size(b"x" * (MAX_SERIALIZED_PAYLOAD_BYTES + 1))
    with pytest.raises(ValueError): EnginePayload("", "bad", "kms", "x", "!", "bad", "other", 900).validate()
def test_execution_id_and_ttl_boundaries():
    assert compose_execution_id("run", "infra/vpc", 0) != compose_execution_id("run", "infra/vpc", 1)
    assert compute_ttl(899, 3600) == 900 and compute_ttl(1800, 1800) == 1800
    with pytest.raises(BudgetUnmintableError): compute_ttl(1801, 1800)
    assert compute_budget(1, 2, 3, 4, 5, 6) == 21
@pytest.mark.parametrize("config", ["account_alias: x\ntf_runtime: bad:1\n", "account_alias: x\nexecution_target: evil\n", "account_alias: x\ntimeout: 2\n", "tf_runtime: tofu:1.8.0\n"])
def test_malicious_config_rejected(config):
    with pytest.raises(ConfigValidationError): parse_folder_config(config)
def test_folder_config_passes_flags_to_resolver():
    config = parse_folder_config("account_alias: target\nextra_flags:\n  - -lock=false\n")
    assert resolve_commands("plan", config).extra_flags == ("-lock=false",)

def test_lock_acquire_duplicate_and_release():
    table = Mock(); acquire(table, "o/r", "f", "exec", 10, 60)
    assert table.put_item.call_args.kwargs["ConditionExpression"] == ("attribute_not_exists(pk) OR expires_at < :now")
    from botocore.exceptions import ClientError
    table.put_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"); table.get_item.return_value = {"Item": {"holder_execution_id": "other"}}
    with pytest.raises(LockHeldError, match="other"): acquire(table, "o/r", "f", "exec", 10, 60)
    table.put_item.side_effect = None; release(table, "o/r", "f", "exec")
