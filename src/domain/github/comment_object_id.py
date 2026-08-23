"""Readable managed PR comment object identity markers."""

from __future__ import annotations

import hashlib
import re

MANAGED_COMMENT_TYPES = frozenset(
    {"plan", "drift", "report", "report-all", "apply", "destroy"}
)
IMMUTABLE_TERMINAL_ACTIONS = frozenset({"apply", "destroy"})
_MARKER_PREFIX = "comment_object_id:"
_LEGACY_TAG_PREFIX = "openci-tf:::tag::"
_MARKER = re.compile(
    r"^comment_object_id:\s*"
    r"(?P<repo>[^:]+):::"
    r"pr-(?P<pr>\d+)::"
    r"(?P<type>plan|drift|report|report-all|apply|destroy):"
    r"(?P<folder>.+?)\s*$"
)


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


def legacy_opaque_tag(repo_name: str, pr_number: int, suffix: str = "") -> str:
    """Legacy MD5 search tag retained only for migration cleanup."""
    raw = f"{repo_name}{pr_number}{suffix}"
    md5 = hashlib.md5(raw.encode()).hexdigest()
    return f"{_LEGACY_TAG_PREFIX}{md5}"


def legacy_folder_suffix(folder: str) -> str:
    return f"folder-{folder}"


def legacy_summary_suffix() -> str:
    return "summary"
