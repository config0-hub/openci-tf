# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Markdown truncation and balancing for bounded GitHub comment bodies."""

from __future__ import annotations

import re

_MAX_COMMENT_CHARS = 60_000
_DETAILS_PREFIX_RE = re.compile(
    r"(<details>\s*\n<summary>.*?</summary>\s*\n\n)", re.IGNORECASE | re.DOTALL
)
_DETAILS_CLOSE = "\n\n</details>"

_TRUNCATION_NOTE = "\n\n> Comment truncated for GitHub size limits. See S3 artifacts for full output.\n"
_COST_SECTION_MARKER = "### Cost Analysis"
_PLAN_SECTION_MARKER = "### Plan"
_ARTIFACT_SECTION_MARKER = "### Download and execution artifacts"
_CI_DETAILS_MARKER = "\n## CI Details"


def _count_code_fences(text: str) -> int:
    return text.count("```")


def _count_details_tags(text: str) -> tuple[int, int]:
    opens = len(re.findall(r"<details\b", text, flags=re.IGNORECASE))
    closes = len(re.findall(r"</details>", text, flags=re.IGNORECASE))
    return opens, closes


def _close_open_markdown(text: str) -> str:
    """Close any open code fence or details block left by a hard character cut."""
    closed = text.rstrip()
    if _count_code_fences(closed) % 2 == 1:
        closed += "\n```"
    opens, closes = _count_details_tags(closed)
    closed += "\n\n</details>" * max(0, opens - closes)
    return closed


def _trim_and_close_markdown(text: str, max_chars: int) -> str:
    """Trim text to max_chars while closing any orphaned markdown structures."""
    if max_chars <= 0:
        return ""
    trimmed = text[:max_chars].rstrip()
    closed = _close_open_markdown(trimmed)
    while len(closed) > max_chars and trimmed:
        trimmed = trimmed[:-1].rstrip()
        closed = _close_open_markdown(trimmed)
    return closed[:max_chars] if len(closed) > max_chars else closed


def _next_section_start(text: str) -> int | None:
    match = re.search(r"\n### [^#]|\n## CI Details", text)
    return match.start() if match else None


def _extract_section(body: str, marker: str) -> tuple[str, str, str] | None:
    """Return (before, section_including_marker, after) when marker is present."""
    idx = body.find(marker)
    if idx == -1:
        return None
    before = body[:idx]
    rest = body[idx:]
    next_start = _next_section_start(rest[len(marker) :])
    if next_start is None:
        return before, rest, ""
    split_at = len(marker) + next_start
    return before, rest[:split_at], rest[split_at:]


def _truncate_plan_section(body: str, max_chars: int) -> str:
    extracted = _extract_section(body, _PLAN_SECTION_MARKER)
    if extracted is None:
        return _trim_and_close_markdown(body, max_chars)
    before, plan_section, after = extracted
    budget = max_chars - len(before) - len(after)
    if budget <= len(_PLAN_SECTION_MARKER):
        return (before + after)[:max_chars].rstrip()
    if len(plan_section) <= budget:
        return before + plan_section + after
    plan_budget = budget - len(_TRUNCATION_NOTE)
    if plan_budget <= len(_PLAN_SECTION_MARKER):
        return (before + after)[:max_chars].rstrip()
    trimmed_plan = _trim_and_close_markdown(plan_section, plan_budget)
    return before + trimmed_plan + _TRUNCATION_NOTE + after


def _bound_comment_preserving_section(
    body: str, *, marker: str, max_chars: int, suffix: str
) -> str:
    """Keep a low-frequency evidence section when earlier plan output dominates."""
    extracted = _extract_section(body, marker)
    if extracted is None:
        return _truncate_head(body, max_chars=max_chars, suffix=suffix)
    before_section, section_body, after_section = extracted
    reserved = (
        len(_TRUNCATION_NOTE) + len(suffix) + len(section_body) + len(after_section)
    )
    if reserved >= max_chars:
        return _truncate_head(body, max_chars=max_chars, suffix=suffix)
    before_budget = max_chars - reserved
    trimmed_before = _truncate_plan_section(before_section, before_budget)
    truncation_note = (
        "" if _TRUNCATION_NOTE.strip() in trimmed_before else _TRUNCATION_NOTE
    )
    return trimmed_before + section_body + after_section + truncation_note + suffix


def _bound_comment_preserving_cost(body: str, *, max_chars: int, suffix: str) -> str:
    """Keep the itemized cost section even when earlier plan output dominates the body."""
    return _bound_comment_preserving_section(
        body, marker=_COST_SECTION_MARKER, max_chars=max_chars, suffix=suffix
    )


def _truncate_head(body: str, *, max_chars: int, suffix: str) -> str:
    reserved = len(_TRUNCATION_NOTE) + len(suffix)
    if reserved >= max_chars:
        return suffix[-max_chars:]
    keep = max_chars - reserved
    return _trim_and_close_markdown(body, keep) + _TRUNCATION_NOTE + suffix


def _bound_inner_comment(body: str, *, max_chars: int, suffix: str) -> str:
    if _ARTIFACT_SECTION_MARKER in body:
        return _bound_comment_preserving_section(
            body, marker=_ARTIFACT_SECTION_MARKER, max_chars=max_chars, suffix=suffix
        )
    if _COST_SECTION_MARKER in body:
        return _bound_comment_preserving_cost(body, max_chars=max_chars, suffix=suffix)
    return _truncate_head(body, max_chars=max_chars, suffix=suffix)


def bound_comment(
    body: str, *, max_chars: int = _MAX_COMMENT_CHARS, suffix: str = ""
) -> str:
    """Return a GitHub comment body within max_chars, preserving suffix (e.g. search tag)."""
    combined = f"{body}{suffix}"
    if len(combined) <= max_chars:
        return combined
    prefix_match = _DETAILS_PREFIX_RE.match(body)
    if prefix_match:
        prefix = prefix_match.group(1)
        rest = body[len(prefix) :]
        closing = _DETAILS_CLOSE if rest.endswith(_DETAILS_CLOSE) else ""
        inner = rest[: -len(closing)] if closing else rest
        reserved = len(prefix) + len(closing) + len(suffix)
        if reserved >= max_chars:
            return (prefix + closing + suffix)[:max_chars]
        bounded_inner = _bound_inner_comment(
            inner, max_chars=max_chars - reserved, suffix=""
        )
        return f"{prefix}{bounded_inner.rstrip()}{closing}{suffix}"
    return _bound_inner_comment(body, max_chars=max_chars, suffix=suffix)
