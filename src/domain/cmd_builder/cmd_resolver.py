# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Safe command resolver registry."""
from dataclasses import dataclass

from src.core.models import FolderConfig


@dataclass(frozen=True)
class ResolvedOrder:
    cmds: list[str]
    timeout: int
    execution_target: str
    verb: str
    extra_flags: tuple[str, ...] = ()
    normalize_drift: bool = False

def _safe(verb: str, config: FolderConfig, *, normalize_drift: bool = False) -> ResolvedOrder:
    return ResolvedOrder(["bash ./openci_tf_run.sh"], config.timeout, config.execution_target, verb, config.extra_flags, normalize_drift)

def resolve_plan(config: FolderConfig) -> ResolvedOrder: return _safe("plan", config)
def resolve_plan_destroy(config: FolderConfig) -> ResolvedOrder: return _safe("plan_destroy", config)
def resolve_drift(config: FolderConfig) -> ResolvedOrder: return _safe("drift", config, normalize_drift=True)
def resolve_report(config: FolderConfig) -> ResolvedOrder: return _safe("report", config)
def resolve_apply(config: FolderConfig) -> ResolvedOrder: return _safe("apply", config)
def resolve_destroy(config: FolderConfig) -> ResolvedOrder: return _safe("destroy", config)

RESOLVERS = {
    "plan": resolve_plan,
    "plan_destroy": resolve_plan_destroy,
    "drift": resolve_drift,
    "report": resolve_report,
    "apply": resolve_apply,
    "destroy": resolve_destroy,
}

def resolve_commands(action: str, folder_config: FolderConfig) -> ResolvedOrder:
    try:
        return RESOLVERS[action](folder_config)
    except KeyError as error:
        raise KeyError(f"No resolver for action: {action!r}") from error
