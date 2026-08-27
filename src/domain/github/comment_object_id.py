# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Readable managed PR comment object identity markers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

MANAGED_COMMENT_TYPES = frozenset(
    {"plan", "drift", "report", "report-all", "apply", "destroy"}
)
IMMUTABLE_TERMINAL_ACTIONS = frozenset({"apply", "destroy"})
_MARKER_PREFIX = "comment_object_id:"
_LEGACY_TAG_PREFIX = "openci-tf:::tag::"
COMMANDS_RUN_AUDIT_HEADING = "## openci-tf commands"
_TRANSIENT_HELP_MARKER = re.compile(
    r"^<!-- openci-tf:transient-help delivery:(?P<delivery_id>[^\s>]+) -->$"
)
_COMMANDS_RUN_MARKER = re.compile(
    r"^comment_object_id:\s*"
    r"(?P<repo>[^:]+):::"
    r"pr-(?P<pr>\d+)::"
    r"commands-run\s*$"
)
_INTENT_HEADING = re.compile(r"^## tf (?P<action>apply|destroy) intent created$")
_INTENT_CONFIRM_LINE = re.compile(
    r"^To proceed within 10 min: `tf (?P<action>apply|destroy) confirm (?P<token>\S+)`$"
)
_TERMINAL_MUTATION_HEADING = re.compile(r"^## (?P<action>Apply|Destroy) (succeeded|failed)\b")
_CODE_FENCE_START = re.compile(r"^\s*(?P<fence>`{3,}).*$")
_STATUS_COMMENT_PREFIX = "#openci-tf:::status_comment\t"
_MARKER = re.compile(
    r"^comment_object_id:\s*"
    r"(?P<repo>[^:]+):::"
    r"pr-(?P<pr>\d+)::"
    r"(?P<type>plan|drift|report|report-all|apply|destroy):"
    r"(?P<folder>.+?)\s*$"
)


@dataclass(frozen=True)
class CommentBodyClassification:
    """Exact structural classification for bot-authored PR comments."""

    kind: str
    comment_type: str | None = None
    folder: str | None = None
    delivery_id: str | None = None


def comment_type_for_action(action: str, *, report_all: bool = False) -> str:
    if report_all:
        return "report-all"
    if action == "plan_destroy":
        return "destroy"
    if action not in MANAGED_COMMENT_TYPES:
        raise ValueError(f"unsupported managed comment action: {action}")
    return action


def folder_value_for_comment(comment_type: str, folder: str) -> str:
    if comment_type == "report-all":
        return "all"
    if not folder:
        raise ValueError("folder is required for managed comment identity")
    return folder


def should_emit_comment_object_marker(action: str, *, terminal: bool) -> bool:
    """Return whether a rendered PR comment should carry replace identity.

    Terminal apply/destroy comments are audit history: they must not carry the
    logical marker that lets a later run delete and replace them. Pending
    mutation comments and all non-mutation actions remain marker-managed.
    """
    comment_type_for_action(action)
    return not (terminal and action in IMMUTABLE_TERMINAL_ACTIONS)


def format_comment_object_marker(
    repo_name: str, pr_number: int, comment_type: str, folder: str
) -> str:
    if comment_type not in MANAGED_COMMENT_TYPES:
        raise ValueError(f"unsupported managed comment type: {comment_type}")
    if pr_number < 1:
        raise ValueError("pr_number must be positive")
    folder_value = folder_value_for_comment(comment_type, folder)
    return (
        f"{_MARKER_PREFIX} {repo_name}:::pr-{pr_number}::{comment_type}:{folder_value}"
    )


def parse_comment_object_marker(line: str) -> dict[str, str] | None:
    match = _MARKER.match(line.strip())
    if match is None:
        return None
    comment_type = match.group("type")
    if comment_type not in MANAGED_COMMENT_TYPES:
        return None
    return {
        "repo_name": match.group("repo"),
        "pr_number": match.group("pr"),
        "comment_type": comment_type,
        "folder": match.group("folder"),
    }


def find_comment_object_marker(body: str) -> dict[str, str] | None:
    for line in body.splitlines():
        parsed = parse_comment_object_marker(line)
        if parsed is not None:
            return parsed
    return None


def _first_non_empty_line(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _last_non_empty_line(body: str) -> str | None:
    for line in reversed(body.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _non_empty_lines_outside_code_fences(body: str) -> list[str]:
    lines: list[str] = []
    open_fence: str | None = None
    for line in body.splitlines():
        stripped = line.strip()
        if open_fence is not None:
            if stripped.startswith(open_fence):
                open_fence = None
            continue
        fence_match = _CODE_FENCE_START.match(stripped)
        if fence_match is not None:
            open_fence = fence_match.group("fence")
            continue
        if stripped:
            lines.append(stripped)
    return lines


def parse_commands_run_marker(line: str) -> dict[str, str] | None:
    match = _COMMANDS_RUN_MARKER.match(line.strip())
    if match is None:
        return None
    return {"repo_name": match.group("repo"), "pr_number": match.group("pr")}


def find_commands_run_marker(body: str) -> dict[str, str] | None:
    if _first_non_empty_line(body) != COMMANDS_RUN_AUDIT_HEADING:
        return None
    trailing = _last_non_empty_line(body)
    if trailing is None:
        return None
    return parse_commands_run_marker(trailing)


def body_has_commands_run_audit_marker(body: str) -> bool:
    return find_commands_run_marker(body) is not None


def find_confirm_intent_marker(body: str) -> dict[str, str] | None:
    heading_action: str | None = None
    for line in _non_empty_lines_outside_code_fences(body):
        heading = _INTENT_HEADING.match(line)
        if heading is not None:
            heading_action = heading.group("action")
            continue
        if heading_action is None:
            continue
        confirm = _INTENT_CONFIRM_LINE.match(line)
        if confirm is not None and confirm.group("action") == heading_action:
            return {"action": heading_action, "token": confirm.group("token")}
    return None


def body_has_confirm_intent_marker(body: str, token: str) -> bool:
    marker = find_confirm_intent_marker(body)
    return marker is not None and marker["token"] == token.strip()


def body_is_confirm_intent_comment(body: str, token: str) -> bool:
    classification = classify_comment_body(body)
    return classification is not None and classification.kind == "intent" and body_has_confirm_intent_marker(body, token)


def body_has_terminal_mutation_heading(body: str) -> bool:
    return any(
        _TERMINAL_MUTATION_HEADING.match(line) is not None
        for line in _non_empty_lines_outside_code_fences(body)
    )


def find_trailing_comment_object_marker(body: str) -> dict[str, str] | None:
    trailing = _last_non_empty_line(body)
    if trailing is None:
        return None
    return parse_comment_object_marker(trailing)


def body_has_trailing_managed_marker(body: str, marker: str) -> bool:
    if body_has_commands_run_audit_marker(body):
        return False
    expected = parse_comment_object_marker(marker)
    if expected is None:
        raise ValueError(f"invalid managed comment marker: {marker!r}")
    return find_trailing_comment_object_marker(body) == expected


def body_has_trailing_hidden_marker(body: str, marker: str) -> bool:
    return not body_has_commands_run_audit_marker(body) and _last_non_empty_line(body) == marker


def body_has_legacy_opaque_tag(body: str, tag: str) -> bool:
    return body_has_trailing_hidden_marker(body, f"#{tag}")


def body_has_status_comment_marker_prefix(body: str, marker_prefix: str) -> bool:
    trailing = _last_non_empty_line(body)
    return (
        not body_has_commands_run_audit_marker(body)
        and trailing is not None
        and trailing.startswith(marker_prefix)
        and marker_prefix.startswith(_STATUS_COMMENT_PREFIX)
    )


def classify_comment_body(body: str) -> CommentBodyClassification | None:
    if body_has_commands_run_audit_marker(body):
        return CommentBodyClassification("audit")
    trailing = _last_non_empty_line(body)
    if trailing is None:
        return None
    transient_match = _TRANSIENT_HELP_MARKER.match(trailing)
    if transient_match is not None:
        return CommentBodyClassification(
            "transient-help", delivery_id=transient_match.group("delivery_id")
        )
    managed = parse_comment_object_marker(trailing)
    if managed is not None:
        return CommentBodyClassification(
            "managed",
            comment_type=managed["comment_type"],
            folder=managed["folder"],
        )
    intent = find_confirm_intent_marker(body)
    if intent is not None:
        return CommentBodyClassification("intent", comment_type=intent["action"])
    if body_has_terminal_mutation_heading(body):
        return CommentBodyClassification("terminal")
    return None


def legacy_opaque_tag(repo_name: str, pr_number: int, suffix: str = "") -> str:
    """Legacy MD5 search tag retained only for migration cleanup."""
    raw = f"{repo_name}{pr_number}{suffix}"
    md5 = hashlib.md5(raw.encode()).hexdigest()
    return f"{_LEGACY_TAG_PREFIX}{md5}"


def legacy_folder_suffix(folder: str) -> str:
    return f"folder-{folder}"


def legacy_summary_suffix() -> str:
    return "summary"
