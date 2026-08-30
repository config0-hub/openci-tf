# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure markdown rendering for bounded execution artifacts."""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from src.core.terminal_evidence import redact_and_bound_terminal_evidence
from src.domain.engine.artifact_paths import (
    FolderArtifactKeys,
    build_folder_artifact_keys,
    build_folder_artifact_keys_for_run,
    pointer_type_for_action,
)
from src.domain.engine.outer_execution_id import validate_outer_run_id
from src.domain.formatters.console_urls import s3_object_console_url
from src.domain.formatters.comment_bounds import (  # noqa: F401  (re-exported)
    _MAX_COMMENT_CHARS,
    bound_comment,
)
from src.domain.formatters.command_text import (
    normalized_command_context_line,
    redact_confirm_token,
    sanitize_command_line,
)
from src.domain.formatters.tfsec_findings import (
    _artifact_skipped,
    _strip_ansi,
    tfsec_summary_line,
)

_MAX_CONFIGURATION_ERROR_CHARS = 465
_ACCOUNT_ID = re.compile(r"^\d{12}$")
_COMMENT_OBJECT_ID_LINE = re.compile(r"\bcomment_object_id\s*:", re.IGNORECASE)
_HIDDEN_AUDIT_ROW_ID = re.compile(r"<!--\s*[dl]:[^\s>]+\s*-->")
_HIDDEN_DELIVERY_ID_LINE = re.compile(
    r"^\s*<!--\s*openci-tf:[^>]*\bdelivery:[^>]+-->\s*$"
)
_LEGACY_COMMENT_ID_LINE = re.compile(r"^\s*#openci-tf:::(?:tag::|status_comment\b).*$")
_IDENTITY_LINE_PLACEHOLDER = "[identity-line-removed]"


def _html_escape(text: str) -> str:
    return html.escape(text, quote=False)


def _short_hash(commit_hash: str) -> str:
    return commit_hash[:7] if commit_hash else "unknown"


def _is_comment_identity_line(line: str) -> bool:
    return (
        _COMMENT_OBJECT_ID_LINE.search(line) is not None
        or _HIDDEN_AUDIT_ROW_ID.search(line) is not None
        or _HIDDEN_DELIVERY_ID_LINE.match(line) is not None
        or _LEGACY_COMMENT_ID_LINE.match(line) is not None
    )


def _neutralize_comment_identity_lines(text: str) -> str:
    return "\n".join(
        _IDENTITY_LINE_PLACEHOLDER if _is_comment_identity_line(line) else line
        for line in text.splitlines()
    )


def _fenced_block(text: str, language: str = "") -> str:
    redacted = redact_confirm_token(text)
    neutralized = _neutralize_comment_identity_lines(redacted)
    fence = "```"
    while fence in neutralized:
        fence += "`"
    return f"{fence}{language}\n{neutralized}\n{fence}"


def _error_block(label: str, text: str) -> str:
    return f"{label}:\n\n{_fenced_block(text)}"


def _require_account_id(outcome: dict[str, Any], folder: str) -> str:
    account_id = outcome.get("account_id")
    if not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id):
        raise ValueError(f"missing or invalid account_id for folder {folder}")
    return account_id


def _account_cell(outcome: dict[str, Any], folder: str) -> str:
    return _require_account_id(outcome, folder)


def _running_label(action: str) -> str:
    return {
        "plan": "Planning",
        "drift": "Drift check running",
        "report": "Report running",
        "apply": "Apply running",
        "destroy": "Destroy running",
    }.get(action, f"{action.title()} running")


def _wrap_collapsed(summary_line: str, body: str) -> str:
    return f"<details>\n<summary>{_html_escape(summary_line)}</summary>\n\n{body.rstrip()}\n\n</details>"


def extract_plan_summary(plan_output: str) -> dict[str, str] | None:
    clean = _strip_ansi(plan_output)
    match = re.search(
        r"Plan:\s+(\d+)\s+to\s+add,\s+(\d+)\s+to\s+change,\s+(\d+)\s+to\s+destroy",
        clean,
    )
    if match:
        return {
            "add": match.group(1),
            "change": match.group(2),
            "destroy": match.group(3),
        }
    if "No changes." in clean or "No changes to infrastructure" in clean:
        return {"add": "0", "change": "0", "destroy": "0"}
    return None


def extract_infracost_monthly(text: str) -> str:
    if not text:
        return "N/A"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "N/A"
    if data.get("skipped"):
        return "not configured"
    monthly_cost = data.get("totalMonthlyCost")
    if monthly_cost is None:
        return "N/A"
    formatted = f"${float(monthly_cost):.2f}" if float(monthly_cost) > 0 else "$0"
    diff_cost = data.get("diffTotalMonthlyCost")
    if diff_cost and float(diff_cost) != 0:
        prefix = "+" if float(diff_cost) > 0 else ""
        formatted += f" ({prefix}${float(diff_cost):.2f})"
    return formatted


def _redact_confirm_token(text: str) -> str:
    return redact_confirm_token(text)


def _describe_command_line(
    action: str,
    *,
    folders: list[str] | None = None,
    all_flag: bool = False,
    affected_flag: bool = False,
    comment_body: str | None = None,
    pipeline: str | None = None,
    pipeline_step: int | None = None,
) -> str:
    if comment_body and comment_body.strip():
        return normalized_command_context_line(comment_body)
    verb = action
    if action == "plan_destroy":
        verb = "plan --destroy"
    if action == "report" and all_flag:
        return "tf report"
    parts = [f"tf {verb}"]
    if pipeline:
        parts.append("pipeline")
        parts.append(pipeline)
        if pipeline_step is not None and pipeline_step > 1:
            parts.append("step")
            parts.append(str(pipeline_step))
    elif affected_flag:
        parts.append("affected")
    elif folders:
        parts.extend(folders)
    return normalized_command_context_line(" ".join(parts))


def _triggering_comment_line(
    comment_id: int | None,
    *,
    comment_link: str | None = None,
    removed: bool = False,
    live_suffix: str | None = None,
) -> str | None:
    if comment_id is None:
        return None
    if removed or not comment_link:
        return (
            f"- triggering comment id: `{comment_id}` (removed after acknowledgement)"
        )
    suffix = f" ({live_suffix})" if live_suffix else ""
    return f"- triggering comment: [{comment_id}]({comment_link}){suffix}"


def prominent_command_header(
    *,
    action: str,
    folders: list[str] | None = None,
    all_flag: bool = False,
    affected_flag: bool = False,
    comment_body: str | None = None,
    pipeline: str | None = None,
    pipeline_step: int | None = None,
    requested_comment_body: str | None = None,
    commit_hash: str | None = None,
) -> str:
    """Prominent command and commit lines for terminal PR comments."""
    if action in {"apply", "destroy"}:
        source = requested_comment_body or comment_body
        command_line = (
            normalized_command_context_line(source)
            if source and source.strip()
            else f"tf {action}"
        )
        label = "Requested command"
    else:
        command_line = _describe_command_line(
            action,
            folders=folders,
            all_flag=all_flag,
            affected_flag=affected_flag,
            comment_body=comment_body,
            pipeline=pipeline,
            pipeline_step=pipeline_step,
        )
        label = "Command"
    lines = [
        "## openci-tf command",
        "",
        f"**{label}:** `{command_line}`",
    ]
    if commit_hash:
        lines.append(f"**Commit:** `{_short_hash(commit_hash)}`")
    return "\n".join(lines)


def command_context_block(
    *,
    action: str,
    folders: list[str] | None = None,
    all_flag: bool = False,
    affected_flag: bool = False,
    comment_body: str | None = None,
    comment_id: int | None = None,
    comment_link: str | None = None,
    run_id: str | None = None,
    commit_hash: str | None = None,
    comment_removed: bool = False,
    triggering_comment_live_suffix: str | None = None,
    pipeline: str | None = None,
    pipeline_step: int | None = None,
) -> str:
    """Concise command-scoped header for bot PR comments."""
    command_line = _describe_command_line(
        action,
        folders=folders,
        all_flag=all_flag,
        affected_flag=affected_flag,
        comment_body=comment_body,
        pipeline=pipeline,
        pipeline_step=pipeline_step,
    )
    lines = ["### openci-tf command", "", f"- command: `{command_line}`"]
    trigger_line = _triggering_comment_line(
        comment_id,
        comment_link=comment_link,
        removed=comment_removed,
        live_suffix=triggering_comment_live_suffix,
    )
    if trigger_line:
        lines.append(trigger_line)
    if run_id:
        lines.append(f"- run id: `{run_id}`")
    if commit_hash:
        lines.append(f"- commit: `{_short_hash(commit_hash)}`")
    return "\n".join(lines)


def _metadata_comment_line(
    comment_id: int | None,
    *,
    label: str,
    comment_link: str | None = None,
    comments_removed: bool = False,
) -> str | None:
    if comment_id is None:
        return None
    if comments_removed or not comment_link:
        return f"- {label} ID: `{comment_id}`"
    return f"- {label}: [{comment_id}]({comment_link})"


def metadata_section(
    *,
    action: str,
    folders: list[str] | None = None,
    all_flag: bool = False,
    affected_flag: bool = False,
    comment_body: str | None = None,
    comment_id: int | None = None,
    comment_link: str | None = None,
    run_id: str | None = None,
    commit_hash: str | None = None,
    comments_removed: bool = False,
    pipeline: str | None = None,
    pipeline_step: int | None = None,
    requested_comment_body: str | None = None,
    requested_comment_id: int | None = None,
    requested_comment_link: str | None = None,
    confirmation_comment_body: str | None = None,
    confirmation_comment_id: int | None = None,
    confirmation_comment_link: str | None = None,
    account_id: str | None = None,
    source_plan_run_id: str | None = None,
    include_account: bool = True,
    include_source_plan_run_id: bool = True,
) -> str:
    """Collapsed Metadata footer for terminal non-report PR comments."""
    if action == "report":
        return ""
    lines: list[str] = []
    if action in {"apply", "destroy"}:
        confirmation_line = (
            normalized_command_context_line(confirmation_comment_body)
            if confirmation_comment_body and confirmation_comment_body.strip()
            else f"tf {action} confirm <redacted>"
        )
        lines.append(f"- Confirmation command: `{confirmation_line}`")
        requested_line = _metadata_comment_line(
            requested_comment_id,
            label="Requested comment",
            comment_link=requested_comment_link,
            comments_removed=comments_removed,
        )
        if requested_line:
            lines.append(requested_line)
        confirmation_line_item = _metadata_comment_line(
            confirmation_comment_id,
            label="Confirmation comment",
            comment_link=confirmation_comment_link,
            comments_removed=comments_removed,
        )
        if confirmation_line_item:
            lines.append(confirmation_line_item)
    else:
        trigger_line = _metadata_comment_line(
            comment_id,
            label="Triggering comment",
            comment_link=comment_link,
            comments_removed=comments_removed,
        )
        if trigger_line:
            lines.append(trigger_line)
    if run_id:
        lines.append(f"- Run ID: `{run_id}`")
    if include_source_plan_run_id and source_plan_run_id:
        source_label = (
            "source destroy-plan run id"
            if action == "destroy"
            else "source plan run id"
        )
        lines.append(f"- {source_label}: `{source_plan_run_id}`")
    if include_account and account_id:
        lines.append(f"- account: `{account_id}`")
    if not lines:
        return ""
    return _wrap_collapsed("Metadata", "\n".join(lines))


def _action_heading(action: str) -> str:
    if action == "plan_destroy":
        return "## openci-tf plan --destroy"
    if action == "report":
        return "## openci-tf report"
    return f"## openci-tf {action}"


def _action_type_line(action: str) -> str:
    return {
        "plan": "**Type:** Plan",
        "plan_destroy": "**Type:** Destroy plan",
        "drift": "**Type:** Drift check",
        "report": "**Type:** Report",
        "apply": "**Type:** Apply",
        "destroy": "**Type:** Destroy",
    }[action]


def _mutation_summary_line(
    folder: str, action: str, *, succeeded: bool, account_id: str = ""
) -> str:
    verb = "Apply" if action == "apply" else "Destroy"
    outcome = f"{verb} succeeded ✅" if succeeded else f"{verb} failed ❌"
    account_part = f" · `{account_id}`" if account_id else ""
    return f"`{folder}`{account_part} · {outcome}"


def _folder_table_cell(
    folder: str,
    *,
    folder_urls: dict[str, str] | None = None,
) -> str:
    """Render one folder label for markdown tables with optional comment links."""
    label = sanitize_command_line(folder)
    if folder_urls and folder in folder_urls:
        return f"[`{label}`]({folder_urls[folder]})"
    return f"`{label}`"


def _mutation_status_icon(outcome: dict[str, Any]) -> str:
    status = str(outcome.get("status") or "")
    if (
        status in {"failed", "infrastructure_error"}
        or outcome.get("succeeded") is False
    ):
        return "❌"
    if status in {"skipped", "not_applicable"}:
        return "⏭️"
    if status == "in_progress":
        return "⏳"
    if status == "unknown":
        return "❔"
    if outcome.get("succeeded") is True or status == "succeeded":
        return "✅"
    return "❔"


def _mutation_status_bucket(outcome: dict[str, Any]) -> int:
    status = str(outcome.get("status") or "")
    if (
        status in {"failed", "infrastructure_error"}
        or outcome.get("succeeded") is False
    ):
        return 0
    if status in {"unknown", "in_progress"}:
        return 1
    if status in {"skipped", "not_applicable"}:
        return 2
    if outcome.get("succeeded") is True or status == "succeeded":
        return 3
    return 1


@dataclass(frozen=True)
class _MutationSummaryRow:
    folder: str
    outcome: dict[str, Any]

    @property
    def sort_key(self) -> tuple[int, str]:
        return (_mutation_status_bucket(self.outcome), self.folder)

    def result_cell(self, action: str) -> str:
        verb = "Apply" if action == "apply" else "Destroy"
        return f"{verb} {_mutation_status_icon(self.outcome)}"


def _mutation_summary_counts_line(rows: list[_MutationSummaryRow]) -> str:
    total = len(rows)
    succeeded = sum(1 for row in rows if _mutation_status_bucket(row.outcome) == 3)
    failed = sum(1 for row in rows if _mutation_status_bucket(row.outcome) == 0)
    skipped = sum(1 for row in rows if _mutation_status_bucket(row.outcome) == 2)
    other = sum(1 for row in rows if _mutation_status_bucket(row.outcome) == 1)
    parts = [
        f"**{total} folders**",
        f"**{succeeded} succeeded**",
        f"**{failed} failed**",
    ]
    if skipped:
        parts.append(f"**{skipped} skipped**")
    if other:
        parts.append(f"**{other} other**")
    return " · ".join(parts)


def _mutation_summary_row(
    row: _MutationSummaryRow,
    action: str,
    *,
    folder_urls: dict[str, str] | None = None,
) -> str:
    folder_cell = _folder_table_cell(row.folder, folder_urls=folder_urls)
    return f"| {folder_cell} | {row.result_cell(action)} |"


def _mutation_summary(
    outcomes: list[dict[str, Any]],
    *,
    action: str,
    folder_urls: dict[str, str] | None = None,
) -> str:
    rows = sorted(
        (
            _MutationSummaryRow(
                folder=str(outcome.get("folder", "unknown")),
                outcome=outcome,
            )
            for outcome in outcomes
        ),
        key=lambda row: row.sort_key,
    )
    lines = [
        _action_heading(action),
        "",
        _action_type_line(action),
        "",
        _mutation_summary_counts_line(rows),
        "",
        "| Folder | Result |",
        "|--------|--------|",
    ]
    lines.extend(
        _mutation_summary_row(row, action, folder_urls=folder_urls) for row in rows
    )
    return "\n".join(lines)


def invalid_command_rejection_comment(
    *,
    parse_error: str,
    comment_id: int | None = None,
    comment_link: str | None = None,
    comment_body: str | None = None,
) -> str:
    """Bot comment when a PR command fails grammar validation."""
    trigger = (
        f"[comment {comment_id}]({comment_link})"
        if comment_id is not None and comment_link
        else (f"comment `{comment_id}`" if comment_id is not None else "your comment")
    )
    detail = ""
    if comment_body and comment_body.strip():
        redacted = normalized_command_context_line(comment_body)
        detail = f"\n\nRejected command ({trigger}): `{redacted}`"
    return f"openci-tf rejected the command: {parse_error}.{detail}"


def closed_pr_rejection_comment(
    *,
    comment_id: int | None = None,
    comment_link: str | None = None,
    comment_body: str | None = None,
) -> str:
    """Bot comment when a command is ignored because the PR is closed or merged."""
    trigger = (
        f"[comment {comment_id}]({comment_link})"
        if comment_id is not None and comment_link
        else (f"comment `{comment_id}`" if comment_id is not None else "your comment")
    )
    detail = ""
    if comment_body and comment_body.strip():
        redacted = normalized_command_context_line(comment_body)
        detail = f"\n\nIgnored command ({trigger}): `{redacted}`"
    return (
        "openci-tf ignored the command because this pull request is closed or merged. "
        "Commands must be posted on an open pull request." + detail
    )


_BOUND_PLAN_CHARS = 8000
_REPORT_PLAN_CHARS = 32_000
_PLAN_TRUNCATION_NOTE = (
    "> Output truncated. See S3 artifacts for full plan output."
)


@dataclass(frozen=True)
class _BoundedPlanText:
    text: str
    truncated: bool


def _bound_plan_display_text(
    text: str, *, max_chars: int = _BOUND_PLAN_CHARS
) -> _BoundedPlanText:
    """Bound human-readable plan output before composing folder comments."""
    return _BoundedPlanText(text=text[:max_chars], truncated=len(text) > max_chars)


def _append_plan_truncation_note(body: str, *, truncated: bool) -> str:
    if not truncated:
        return body
    return f"{body}\n\n{_PLAN_TRUNCATION_NOTE}"


def _human_plan_output_key(action: str) -> str:
    if action == "plan_destroy":
        return "destroy.plan.out"
    return "tf/plan.out"


def _human_plan_text(action: str, artifacts: dict[str, str]) -> str:
    return artifacts.get(_human_plan_output_key(action), "")


_REPORT_DRIFT_LABELS = {
    "clean": "✅ CLEAN",
    "drift": "⚠️ DRIFT",
    "failed": "❌ FAILED",
    "unknown": "❔ UNKNOWN",
    "pending": "⏳ PENDING",
    "skipped": "⏭️ NOT RUN",
}
_REPORT_SECURITY_LABELS = {
    "clean": "✅ CLEAN",
    "critical": "🛑 CRITICAL",
    "high": "🛑 HIGH",
    "medium": "⚠️ MEDIUM",
    "low": "⚠️ LOW",
    "unknown": "❔ UNKNOWN",
    "pending": "⏳ PENDING",
    "skipped": "⏭️ NOT RUN",
}
_REPORT_SECURITY_SORT = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "clean": 4,
    "unknown": 5,
}


@dataclass(frozen=True)
class _ReportRow:
    folder: str
    account_id: str
    status: str
    succeeded: bool | None
    plan_counts: tuple[int, int, int] | None
    security: str
    finding_count: int | None
    cost: str

    @property
    def failed(self) -> bool:
        return (
            self.status in {"failed", "infrastructure_error"} or self.succeeded is False
        )

    @property
    def pending(self) -> bool:
        return self.status == "in_progress"

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"

    @property
    def unknown(self) -> bool:
        return (
            not self.failed
            and not self.pending
            and not self.skipped
            and (
                self.status == "unknown"
                or self.plan_counts is None
                or self.security == "unknown"
            )
        )

    @property
    def destroy_count(self) -> int:
        return self.plan_counts[2] if self.plan_counts is not None else 0

    @property
    def delta_count(self) -> int:
        return sum(self.plan_counts) if self.plan_counts is not None else 0

    @property
    def clean(self) -> bool:
        return (
            not self.failed
            and not self.pending
            and not self.skipped
            and self.status != "unknown"
            and self.plan_counts == (0, 0, 0)
            and self.security == "clean"
        )

    @property
    def bucket(self) -> int:
        if self.failed or self.unknown or self.skipped or self.pending:
            return 0
        if self.security in {"critical", "high"} or self.destroy_count > 0:
            return 1
        if self.security in {"medium", "low"} or self.delta_count > 0:
            return 2
        return 3

    @property
    def sort_key(self) -> tuple[int | str, ...]:
        if self.bucket == 0:
            sub = 0 if self.failed else 1 if self.unknown else 2 if self.skipped else 3
            return (self.bucket, sub, self.folder)
        if self.bucket == 1:
            high_security = (
                self.security if self.security in {"critical", "high"} else "clean"
            )
            return (
                self.bucket,
                _REPORT_SECURITY_SORT[high_security],
                -(self.finding_count or 0),
                -self.destroy_count,
                self.folder,
            )
        if self.bucket == 2:
            return (
                self.bucket,
                _REPORT_SECURITY_SORT[self.security],
                -(self.finding_count or 0),
                -self.delta_count,
                self.folder,
            )
        return (self.bucket, self.folder)

    @property
    def needs_attention(self) -> bool:
        return not self.clean

    def drift_cell(self) -> str:
        if self.failed:
            return _REPORT_DRIFT_LABELS["failed"]
        if self.pending:
            return _REPORT_DRIFT_LABELS["pending"]
        if self.skipped:
            return _REPORT_DRIFT_LABELS["skipped"]
        if self.status == "unknown" or self.plan_counts is None:
            return _REPORT_DRIFT_LABELS["unknown"]
        add, change, destroy = self.plan_counts
        if add == 0 and change == 0 and destroy == 0:
            return _REPORT_DRIFT_LABELS["clean"]
        return f"{_REPORT_DRIFT_LABELS['drift']} +{add} ~{change} -{destroy}"

    def security_cell(self) -> str:
        if self.failed:
            return "not run"
        if self.pending:
            return _REPORT_SECURITY_LABELS["pending"]
        if self.skipped:
            return _REPORT_SECURITY_LABELS["skipped"]
        if self.status == "unknown":
            return _REPORT_SECURITY_LABELS["unknown"]
        return _report_security_label(self.security, count=self.finding_count)

    def drift_icon(self) -> str:
        if self.failed:
            return "❌"
        if self.pending:
            return "⏳"
        if self.skipped:
            return "⏭️"
        if self.status == "unknown" or self.plan_counts is None:
            return "❔"
        add, change, destroy = self.plan_counts
        if add == 0 and change == 0 and destroy == 0:
            return "✅"
        return "⚠️"

    def security_icon(self) -> str:
        if self.failed:
            return "⏭️"
        if self.pending:
            return "⏳"
        if self.skipped:
            return "⏭️"
        if self.status == "unknown" or self.security == "unknown":
            return "❔"
        if self.security == "clean":
            return "✅"
        if self.security in {"critical", "high"}:
            return "🛑"
        if self.security in {"medium", "low"}:
            return "⚠️"
        return "❔"


def _plan_counts(plan_text: str) -> tuple[int, int, int] | None:
    summary = extract_plan_summary(plan_text)
    if summary is None:
        return None
    return int(summary["add"]), int(summary["change"]), int(summary["destroy"])


def _report_security_label(security: str, *, count: int | None = None) -> str:
    base = _REPORT_SECURITY_LABELS.get(security, _REPORT_SECURITY_LABELS["unknown"])
    if security in {"clean", "unknown", "pending", "skipped"} or count is None:
        return base
    noun = "finding" if count == 1 else "findings"
    return f"{base} · {count} {noun}"


def _report_row(
    outcome: dict[str, Any], artifacts: dict[str, str], *, action: str = "report"
) -> _ReportRow:
    folder = str(outcome.get("folder", "unknown"))
    status = str(
        outcome.get("status", "succeeded" if outcome.get("succeeded") else "unknown")
    )
    account_id = "-" if folder == "config" else _account_cell(outcome, folder)
    security, finding_count = tfsec_summary_line(artifacts.get("tfsec.json", ""))
    return _ReportRow(
        folder=folder,
        account_id=account_id,
        status=status,
        succeeded=outcome.get("succeeded"),
        plan_counts=_plan_counts(_human_plan_text(action, artifacts)),
        security=security,
        finding_count=finding_count,
        cost=_cost_cell(artifacts.get("infracost.json", "{}")),
    )


def _report_rows(
    outcomes: list[dict[str, Any]],
    artifacts_by_folder: dict[str, dict[str, str]],
    *,
    action: str = "report",
) -> list[_ReportRow]:
    return [
        _report_row(
            outcome,
            artifacts_by_folder.get(str(outcome.get("folder", "")), {}),
            action=action,
        )
        for outcome in outcomes
    ]


def _report_highest_alert(rows: list[_ReportRow]) -> str | None:
    attention = [row for row in rows if row.needs_attention]
    if not attention:
        return None
    row = min(attention, key=lambda item: item.sort_key)
    if row.failed:
        return f"> [!CAUTION]\n> **{row.folder}** - ❌ FAILED execution"
    if row.unknown:
        return f"> [!CAUTION]\n> **{row.folder}** - ❔ UNKNOWN evidence"
    if row.skipped:
        return f"> [!IMPORTANT]\n> **{row.folder}** - ⏭️ NOT RUN"
    if row.pending:
        return f"> [!IMPORTANT]\n> **{row.folder}** - ⏳ PENDING"
    if row.bucket == 1:
        return f"> [!WARNING]\n> **{row.folder}** - review critical/high security findings or planned destroys"
    return f"> [!IMPORTANT]\n> **{row.folder}** - review drift or security findings"


def _report_folder_summary_line(
    folder: str,
    outcome: dict[str, Any],
    artifacts: dict[str, str],
    *,
    action: str = "report",
) -> str:
    row = _report_row({**outcome, "folder": folder}, artifacts, action=action)
    return f"{folder} · Drift {row.drift_icon()} · Security {row.security_icon()}"


def _indent_blockquote(text: str) -> str:
    return "\n".join(">" if not line else f"> {line}" for line in text.splitlines())


def _report_child_collapsible(summary: str, body: str) -> str:
    return _indent_blockquote(_wrap_collapsed(summary, body))


def _init_status_icon(text: str) -> str:
    if not text:
        return "❔"
    return (
        "✅"
        if "successfully initialized" in _strip_ansi(text).lower()
        else "❌"
    )


def _validate_status_icon(text: str) -> str:
    if not text:
        return "❔"
    clean = _strip_ansi(text).lower()
    return (
        "✅"
        if "success!" in clean or "configuration is valid" in clean
        else "❌"
    )


def _setup_status_icon(init_text: str, validate_text: str) -> str:
    icons = {_init_status_icon(init_text), _validate_status_icon(validate_text)}
    if "❌" in icons:
        return "❌"
    if "❔" in icons:
        return "❔"
    return "✅"


def _plan_status_icon(plan_text: str) -> str:
    counts = _plan_counts(plan_text)
    if counts is None:
        return "❔"
    if counts == (0, 0, 0):
        return "✅"
    return "⚠️"


def _security_status_icon(tfsec_json: str) -> str:
    security, _ = tfsec_summary_line(tfsec_json)
    if security == "clean":
        return "✅"
    if security in {"critical", "high"}:
        return "🛑"
    if security in {"medium", "low"}:
        return "⚠️"
    return "❔"


def _init_status(text: str) -> str:
    if not text:
        return "Init unavailable"
    return (
        "Init succeeded"
        if "successfully initialized" in _strip_ansi(text).lower()
        else "Init failed"
    )


def _validate_status(text: str) -> str:
    if not text:
        return "Validate unavailable"
    clean = _strip_ansi(text).lower()
    return (
        "Validate succeeded"
        if "success!" in clean or "configuration is valid" in clean
        else "Validate failed"
    )


def _highlight_plan(text: str) -> str:
    return "\n".join(
        "+ " + line
        if line.strip().startswith("+ resource")
        else "- " + line
        if line.strip().startswith("- resource")
        else "! " + line
        if line.strip().startswith(("~ resource", "# resource"))
        else line
        for line in text.splitlines()
    )


def _report_plan_collapsible(text: str) -> str:
    clean = _neutralize_comment_identity_lines(_strip_ansi(text))
    bounded = _bound_plan_display_text(clean, max_chars=_REPORT_PLAN_CHARS)
    counts = _plan_counts(clean)
    parts: list[str] = []
    if counts is None:
        parts.append("Plan output was unavailable or unparseable.")
    else:
        add, change, destroy = counts
        parts.extend(
            [
                "**Plan summary:**",
                f"- **{add} to add**",
                f"- **{change} to change**",
                f"- **{destroy} to destroy**",
                "",
            ]
        )
    parts.append(
        _append_plan_truncation_note(
            _fenced_block(_highlight_plan(bounded.text), "diff"),
            truncated=bounded.truncated,
        )
    )
    return _report_child_collapsible(
        f"Plan {_plan_status_icon(text)}",
        "\n".join(parts),
    )


def _report_tfsec_collapsible(tfsec_json: str, tfsec_output: str) -> str:
    if tfsec_json and _artifact_skipped(tfsec_json, "not run"):
        return ""
    readable = _strip_ansi(tfsec_output) if tfsec_output else ""
    if not readable.strip():
        readable = "Security output unavailable."
    body = _fenced_block(readable)
    return _report_child_collapsible(
        f"Security {_security_status_icon(tfsec_json)}",
        body,
    )


def _report_infracost_collapsible(infracost_json: str, infracost_output: str) -> str:
    if infracost_json and _artifact_skipped(infracost_json, "not run"):
        return ""
    monthly = extract_infracost_monthly(infracost_json)
    summary = {
        "not configured": "Cost · not configured",
        "N/A": "Cost · unavailable",
    }.get(monthly, f"Cost · {monthly}/mo")
    body = (
        "Cost output unavailable."
        if not infracost_output.strip()
        else _fenced_block(_strip_ansi(infracost_output))
    )
    return _report_child_collapsible(summary, body)


def _report_setup_collapsible(init_text: str, validate_text: str) -> str:
    body = "\n".join(
        [
            f"terraform init {_init_status_icon(init_text)}",
            f"terraform validate {_validate_status_icon(validate_text)}",
        ]
    )
    return _report_child_collapsible(
        f"Setup {_setup_status_icon(init_text, validate_text)}",
        body,
    )


_REPORT_ARTIFACT_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Run", (("manifest.json", "manifest.json"),)),
    ("Setup", (("init.out", "init.out"), ("validate.out", "validate.out"))),
    (
        "Plan",
        (
            ("plan.out", "tf/plan.out"),
            ("plan.tfplan", "tf/plan.tfplan"),
            ("plan.tfplan.sha256", "tf/plan.tfplan.sha256"),
            ("plan-metadata.json", "tf/plan-metadata.json"),
        ),
    ),
    (
        "Security",
        (("tfsec.output", "tfsec.output"), ("tfsec.json", "tfsec.json")),
    ),
    (
        "Cost",
        (("infracost.output", "infracost.output"), ("infracost.json", "infracost.json")),
    ),
)


_REPORT_DESTROY_PLAN_ARTIFACT_MEMBERS: tuple[tuple[str, str], ...] = (
    ("destroy.plan.out", "destroy.plan.out"),
    ("destroy.plan.tfplan", "tf/destroy.plan.tfplan"),
    ("destroy.plan.tfplan.sha256", "tf/destroy.plan.tfplan.sha256"),
    ("destroy-plan-metadata.json", "tf/destroy-plan-metadata.json"),
)


def _report_artifact_groups(
    action: str,
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    plan_members = (
        _REPORT_DESTROY_PLAN_ARTIFACT_MEMBERS
        if action == "plan_destroy"
        else _REPORT_ARTIFACT_GROUPS[2][1]
    )
    return (
        _REPORT_ARTIFACT_GROUPS[0],
        _REPORT_ARTIFACT_GROUPS[1],
        ("Plan", plan_members),
        *_REPORT_ARTIFACT_GROUPS[3:],
    )


def _report_artifact_layout_keys(
    *,
    repo_name: str,
    run_id: str,
    folder: str,
    pr_number: int | None,
    action: str = "report",
) -> FolderArtifactKeys:
    scoped_pr: int | None = None
    pointer_type: str | None = None
    if isinstance(pr_number, int):
        try:
            validate_outer_run_id(run_id)
        except ValueError:
            pass
        else:
            scoped_pr = pr_number
            pointer_type = pointer_type_for_action(action)
    if scoped_pr is not None and pointer_type is not None:
        return build_folder_artifact_keys_for_run(
            repo_name=repo_name,
            run_id=run_id,
            folder_path=folder,
            pr_number=scoped_pr,
            pointer_type=pointer_type,
        )
    return build_folder_artifact_keys(
        repo_name=repo_name, run_id=run_id, folder_path=folder
    )


def _report_artifact_key(
    *,
    repo_name: str,
    run_id: str,
    folder: str,
    storage_name: str,
    pr_number: int | None = None,
    action: str = "report",
) -> str:
    keys = _report_artifact_layout_keys(
        repo_name=repo_name,
        run_id=run_id,
        folder=folder,
        pr_number=pr_number,
        action=action,
    )
    by_name = {
        "manifest.json": keys.manifest_json,
        "init.out": keys.init_out,
        "validate.out": keys.validate_out,
        "tf/plan.out": keys.plan_out,
        "tf/plan.tfplan": keys.plan_tfplan,
        "tf/plan.tfplan.sha256": keys.plan_sha256,
        "tf/plan-metadata.json": keys.plan_metadata,
        "destroy.plan.out": keys.destroy_plan_out,
        "tf/destroy.plan.tfplan": keys.destroy_plan_tfplan,
        "tf/destroy.plan.tfplan.sha256": keys.destroy_plan_sha256,
        "tf/destroy-plan-metadata.json": keys.destroy_plan_metadata,
        "tfsec.output": keys.tfsec_output,
        "tfsec.json": keys.tfsec_json,
        "infracost.output": keys.infracost_output,
        "infracost.json": keys.infracost_json,
        "apply.out": f"{keys.prefix}/apply.out",
        "destroy.out": f"{keys.prefix}/destroy.out",
        "plan-show.out": f"{keys.prefix}/plan-show.out",
    }
    return by_name[storage_name]


_MUTATION_APPLY_ARTIFACT_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Run", (("manifest.json", "manifest.json"),)),
    (
        "Plan",
        (
            ("plan.tfplan", "tf/plan.tfplan"),
            ("plan.tfplan.sha256", "tf/plan.tfplan.sha256"),
            ("plan.out", "tf/plan.out"),
        ),
    ),
    ("Apply", (("apply.out", "apply.out"),)),
)

_MUTATION_DESTROY_ARTIFACT_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Run", (("manifest.json", "manifest.json"),)),
    (
        "Destroy plan",
        (
            ("destroy.plan.tfplan", "tf/destroy.plan.tfplan"),
            ("destroy.plan.tfplan.sha256", "tf/destroy.plan.tfplan.sha256"),
        ),
    ),
    ("Destroy", (("destroy.out", "destroy.out"),)),
)


def _mutation_artifact_groups(action: str) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    if action == "apply":
        return _MUTATION_APPLY_ARTIFACT_GROUPS
    return _MUTATION_DESTROY_ARTIFACT_GROUPS


def _nested_artifact_groups_collapsible(
    *,
    group_specs: tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
    repo_name: str,
    run_id: str,
    folder: str,
    existing_names: frozenset[str],
    tmp_bucket: str,
    region: str,
    hub_account_id: str | None,
    identity_center_start_url: str | None,
    identity_center_role_name: str | None,
    pr_number: int | None = None,
    action: str,
) -> str:
    nested_parts: list[str] = []
    for group_name, members in group_specs:
        group_lines: list[str] = []
        for display_name, storage_name in members:
            if storage_name not in existing_names:
                continue
            key = _report_artifact_key(
                repo_name=repo_name,
                run_id=run_id,
                folder=folder,
                storage_name=storage_name,
                pr_number=pr_number,
                action=action,
            )
            url = s3_object_console_url(
                tmp_bucket,
                key,
                region=region,
                account_id=hub_account_id,
                identity_center_start_url=identity_center_start_url,
                identity_center_role_name=identity_center_role_name,
            )
            group_lines.append(f"[{display_name}]({url})")
        if group_lines:
            nested_parts.append(
                _report_child_collapsible(
                    f"{group_name} artifacts",
                    "\n".join(group_lines),
                )
            )
    if not nested_parts:
        return ""
    return _report_child_collapsible("Artifacts", "\n\n".join(nested_parts))


def _report_artifact_link_lines(
    *,
    repo_name: str,
    run_id: str,
    folder: str,
    existing_names: frozenset[str],
    tmp_bucket: str,
    region: str,
    hub_account_id: str | None,
    identity_center_start_url: str | None,
    identity_center_role_name: str | None,
    pr_number: int | None = None,
    action: str = "report",
) -> list[str]:
    if not repo_name or not run_id or not tmp_bucket or not region:
        return []
    lines: list[str] = []
    for group_name, members in _report_artifact_groups(action):
        group_lines: list[str] = []
        for display_name, storage_name in members:
            if storage_name not in existing_names:
                continue
            key = _report_artifact_key(
                repo_name=repo_name,
                run_id=run_id,
                folder=folder,
                storage_name=storage_name,
                pr_number=pr_number,
                action=action,
            )
            url = s3_object_console_url(
                tmp_bucket,
                key,
                region=region,
                account_id=hub_account_id,
                identity_center_start_url=identity_center_start_url,
                identity_center_role_name=identity_center_role_name,
            )
            group_lines.append(f"  [{display_name}]({url})")
        if group_lines:
            lines.append(group_name)
            lines.extend(group_lines)
    return lines


def _report_artifacts_collapsible(
    *,
    repo_name: str,
    run_id: str,
    folder: str,
    existing_names: frozenset[str],
    tmp_bucket: str,
    region: str,
    hub_account_id: str | None,
    identity_center_start_url: str | None,
    identity_center_role_name: str | None,
    pr_number: int | None = None,
    action: str = "report",
    approved_plan_pointer_key: str | None = None,
) -> str:
    lines = _report_artifact_link_lines(
        repo_name=repo_name,
        run_id=run_id,
        folder=folder,
        existing_names=existing_names,
        tmp_bucket=tmp_bucket,
        region=region,
        hub_account_id=hub_account_id,
        identity_center_start_url=identity_center_start_url,
        identity_center_role_name=identity_center_role_name,
        pr_number=pr_number,
        action=action,
    )
    if approved_plan_pointer_key and tmp_bucket and region:
        pointer_label = approved_plan_pointer_key.rsplit("/", 1)[-1]
        pointer_url = s3_object_console_url(
            tmp_bucket,
            approved_plan_pointer_key,
            region=region,
            account_id=hub_account_id,
            identity_center_start_url=identity_center_start_url,
            identity_center_role_name=identity_center_role_name,
        )
        lines.insert(0, "Approved plan")
        lines.insert(1, f"  [{pointer_label}]({pointer_url})")
    if not lines:
        return ""
    return _report_child_collapsible("Artifacts", "\n".join(lines))


def _report_execution_collapsible(
    *,
    console_url: str | None,
    codebuild_url: str | None = None,
    codebuild_account_id: str | None = None,
    lowercase_links: bool = False,
) -> str:
    lines: list[str] = []
    if console_url:
        label = "step function" if lowercase_links else "Step Functions execution"
        lines.append(f"[{label}]({console_url})")
    if codebuild_url:
        account_note = (
            f" · hub account `{codebuild_account_id}`; switch the AWS console to this account first"
            if codebuild_account_id
            else ""
        )
        label = "codebuild" if lowercase_links else "CodeBuild job"
        lines.append(f"[{label}]({codebuild_url}){account_note}")
    if not lines:
        return ""
    return _report_child_collapsible("Execution", "\n".join(lines))


def _report_folder_comment(
    folder: str,
    outcome: dict[str, Any],
    artifacts: dict[str, str],
    *,
    action: str = "report",
    console_url: str | None = None,
    run_id: str | None = None,
    repo_name: str = "",
    pr_number: int | None = None,
    existing_names: frozenset[str] | None = None,
    tmp_bucket: str = "",
    region: str = "",
    hub_account_id: str | None = None,
    identity_center_start_url: str | None = None,
    identity_center_role_name: str | None = None,
    approved_plan_pointer_key: str | None = None,
) -> str:
    if folder != "config":
        _require_account_id(outcome, folder)
    names = existing_names or frozenset(artifacts)
    plan_text = _human_plan_text(action, artifacts)
    parts = [
        _report_setup_collapsible(
            artifacts.get("init.out", ""), artifacts.get("validate.out", "")
        ),
        _report_plan_collapsible(plan_text),
        _report_tfsec_collapsible(
            artifacts.get("tfsec.json", ""),
            artifacts.get("tfsec.output", ""),
        ),
        _report_infracost_collapsible(
            artifacts.get("infracost.json", ""),
            artifacts.get("infracost.output", ""),
        ),
        _report_execution_collapsible(console_url=console_url),
        _report_artifacts_collapsible(
            repo_name=repo_name,
            run_id=str(run_id or ""),
            folder=folder,
            existing_names=names,
            tmp_bucket=tmp_bucket,
            region=region,
            hub_account_id=hub_account_id,
            identity_center_start_url=identity_center_start_url,
            identity_center_role_name=identity_center_role_name,
            pr_number=pr_number,
            action=action,
            approved_plan_pointer_key=approved_plan_pointer_key,
        ),
    ]
    return _wrap_collapsed(
        _report_folder_summary_line(folder, outcome, artifacts, action=action),
        "\n\n".join(part for part in parts if part),
    )


def _report_summary_row(
    folder: str,
    drift_icon: str,
    security_icon: str,
    cost: str,
    *,
    folder_urls: dict[str, str] | None = None,
) -> str:
    folder_cell = _folder_table_cell(folder, folder_urls=folder_urls)
    return f"| {folder_cell} | {drift_icon} | {security_icon} | {cost} |"


def _report_summary(
    outcomes: list[dict[str, Any]],
    artifacts_by_folder: dict[str, dict[str, str]] | None = None,
    *,
    action: str = "report",
    folder_urls: dict[str, str] | None = None,
    steps: list[list[str]] | None = None,
) -> str:
    artifacts = artifacts_by_folder or {}
    report_rows = _report_rows(outcomes, artifacts, action=action)
    attention = sorted(
        (row for row in report_rows if row.needs_attention),
        key=lambda row: row.sort_key,
    )
    clean = sorted(
        (row for row in report_rows if row.clean), key=lambda row: row.folder
    )
    lines: list[str] = [_action_heading(action), "", _action_type_line(action), ""]
    if steps is not None and len(steps) > 1:
        lines.extend(_pipeline_step_rows(steps, outcomes))
        lines.append("")
    lines.extend(
        [
            f"**{len(report_rows)} folders** · "
            f"**{len(attention)} need attention** · "
            f"**{len(clean)} clean**",
            "",
        ]
    )
    if attention:
        lines.extend(
            [
                "### Needs attention",
                "",
                "| Folder | Drift | Security | Cost |",
                "|--------|-------|----------|------|",
            ]
        )
        for row in attention:
            lines.append(
                _report_summary_row(
                    row.folder,
                    row.drift_icon(),
                    row.security_icon(),
                    row.cost,
                    folder_urls=folder_urls,
                )
            )
        lines.append("")
    if clean:
        clean_table = [
            "| Folder | Drift | Security | Cost |",
            "|--------|-------|----------|------|",
        ]
        for row in clean:
            clean_table.append(
                _report_summary_row(
                    row.folder,
                    row.drift_icon(),
                    row.security_icon(),
                    row.cost,
                    folder_urls=folder_urls,
                )
            )
        lines.append(
            _wrap_collapsed(
                f"{len(clean)} clean folder{'s' if len(clean) != 1 else ''} ✅",
                "\n".join(clean_table),
            )
        )
    return "\n".join(lines)


def _cost_cell(infracost_text: str) -> str:
    monthly = extract_infracost_monthly(infracost_text)
    return monthly if monthly != "N/A" else " "


def status_comment_marker(run_id: str, *, now: int | None = None) -> str:
    """Run-specific hidden marker for transient in-progress CI status comments."""
    expire_epoch = str((now if now is not None else int(time.time())) + 3600)
    return f"#openci-tf:::status_comment\t{run_id}\t{expire_epoch}"


def status_comment_marker_prefix(run_id: str) -> str:
    """Prefix that uniquely identifies one run's transient status comment."""
    return f"#openci-tf:::status_comment\t{run_id}\t"


def status_comment_in_progress(
    commit_hash: str, console_url: str, run_id: str, *, now: int | None = None
) -> str:
    """Original-style transient CI Details comment posted immediately after command acceptance."""
    return "\n".join(
        [
            "\n## CI Details ",
            f"+ {commit_hash}",
            f"+ [ci pipeline]({console_url})",
            "+ status: in_progress",
            "",
            status_comment_marker(run_id, now=now),
        ]
    )


def mutation_status_comment_in_progress(
    *,
    action: str,
    folder: str,
    commit_hash: str,
    grace_seconds: int,
    console_url: str,
    run_id: str,
    codebuild_url: str | None = None,
    codebuild_account_id: str | None = None,
    now: int | None = None,
) -> str:
    """In-progress comment for apply/destroy with grace period and execution links."""
    verb = action.title()
    lines = [
        f"\n## {verb} in progress — `{folder}`",
        f"+ commit: `{commit_hash[:7] if commit_hash else 'unknown'}`",
        f"+ grace period: {grace_seconds}s — stop the outer Step Functions execution during this wait to abort before CodeBuild starts",
        f"+ [Step Functions execution]({console_url})",
    ]
    if codebuild_url:
        account_note = (
            f" — hub account `{codebuild_account_id}`; switch the AWS console to this account first"
            if codebuild_account_id
            else ""
        )
        lines.append(f"+ [CodeBuild job]({codebuild_url}){account_note}")
    lines.extend(["+ status: in_progress", "", status_comment_marker(run_id, now=now)])
    return "\n".join(lines)


def _mutation_plan_collapsible(
    plan_show_text: str | None,
    *,
    plan_show_pointer: str | None = None,
    pinned_plan_artifact: str | None = None,
) -> str:
    """Render bounded pinned-plan output inside a neutral collapsed Plan child."""
    if plan_show_pointer and not plan_show_text:
        return _report_child_collapsible(
            "Plan", f"plan show output: `{plan_show_pointer}`"
        )
    if not plan_show_text:
        return ""
    stripped = _strip_ansi(plan_show_text)
    bounded = _bound_plan_display_text(stripped)
    parts: list[str] = []
    if pinned_plan_artifact:
        parts.append(f"**Pinned plan:** `{pinned_plan_artifact}`")
        parts.append("")
    body = _append_plan_truncation_note(
        _fenced_block(_highlight_plan(bounded.text), "diff"),
        truncated=bounded.truncated,
    )
    parts.append(body)
    if plan_show_pointer:
        parts.append(f"\nplan show output: `{plan_show_pointer}`")
    return _report_child_collapsible("Plan", "\n".join(parts))


def _mutation_artifacts_collapsible(
    pinned_plan_artifact: str,
    *,
    repo_name: str,
    run_id: str,
    folder: str,
    existing_names: frozenset[str],
    tmp_bucket: str,
    region: str,
    hub_account_id: str | None,
    identity_center_start_url: str | None,
    identity_center_role_name: str | None,
    pr_number: int | None = None,
    action: str,
) -> str:
    return _nested_artifact_groups_collapsible(
        group_specs=_mutation_artifact_groups(action),
        repo_name=repo_name,
        run_id=run_id,
        folder=folder,
        existing_names=existing_names,
        tmp_bucket=tmp_bucket,
        region=region,
        hub_account_id=hub_account_id,
        identity_center_start_url=identity_center_start_url,
        identity_center_role_name=identity_center_role_name,
        pr_number=pr_number,
        action=action,
    )


def mutation_terminal_comment(
    *,
    action: str,
    folder: str,
    account_id: str,
    commit_hash: str,
    succeeded: bool,
    pinned_plan_artifact: str,
    console_url: str | None,
    codebuild_url: str | None,
    codebuild_account_id: str | None,
    plan_show_text: str | None,
    plan_show_pointer: str | None,
    source_plan_run_id: str | None = None,
    error: str | None = None,
    run_id: str = "",
    repo_name: str = "",
    pr_number: int | None = None,
    existing_names: frozenset[str] | None = None,
    tmp_bucket: str = "",
    region: str = "",
    hub_account_id: str | None = None,
    identity_center_start_url: str | None = None,
    identity_center_role_name: str | None = None,
) -> str:
    """Terminal apply/destroy folder comment with bounded plan show output."""
    body_parts: list[str] = []
    if error:
        bounded_error = redact_and_bound_terminal_evidence(error)
        if not isinstance(bounded_error, str):
            raise TypeError("mutation terminal error must be a string")
        body_parts.append(_error_block("error", bounded_error))
    child_parts = [
        _mutation_plan_collapsible(
            plan_show_text,
            plan_show_pointer=plan_show_pointer,
            pinned_plan_artifact=pinned_plan_artifact,
        ),
        _report_execution_collapsible(
            console_url=console_url,
            codebuild_url=codebuild_url,
            codebuild_account_id=codebuild_account_id,
            lowercase_links=True,
        ),
        _mutation_artifacts_collapsible(
            pinned_plan_artifact,
            repo_name=repo_name,
            run_id=run_id,
            folder=folder,
            existing_names=existing_names or frozenset(),
            tmp_bucket=tmp_bucket,
            region=region,
            hub_account_id=hub_account_id,
            identity_center_start_url=identity_center_start_url,
            identity_center_role_name=identity_center_role_name,
            pr_number=pr_number,
            action=action,
        ),
    ]
    body_parts.extend(part for part in child_parts if part)
    return _wrap_collapsed(
        _mutation_summary_line(
            folder, action, succeeded=succeeded, account_id=account_id
        ),
        "\n\n".join(body_parts),
    )


def _format_error(outcome: dict[str, Any]) -> str:
    bounded = redact_and_bound_terminal_evidence(
        outcome.get("error") or "unknown error"
    )
    if not isinstance(bounded, str):
        raise TypeError("folder terminal error must be a string")
    return redact_confirm_token(bounded)


def _terminal_status(outcomes: list[dict[str, Any]]) -> str:
    if any(
        outcome.get("status") in {"failed", "infrastructure_error"}
        or outcome.get("succeeded") is False
        for outcome in outcomes
    ):
        return "failed"
    return "succeeded"


def pending_plan_comment(
    folder: str, account_id: str, commit_hash: str, action: str
) -> str:
    body = (
        f"{_action_heading(action)}\n\n"
        f"{_running_label(action)} at `{_short_hash(commit_hash)}`…"
    )
    return _wrap_collapsed(f"{folder} · Drift ⏳ · Security ⏳", body)


def pending_summary_all(commit_hash: str, action: str) -> str:
    short = commit_hash[:7]
    verb = _running_label(action)
    lines = [_action_heading(action), "", _action_type_line(action), ""]
    return f"{'\n'.join(lines)}\n{verb} all folders at `{short}`…"


def pending_summary(
    folders: list[dict[str, Any]],
    skipped: list[dict[str, Any]] | None = None,
    *,
    action: str = "plan",
) -> str:
    rows = [
        _action_heading(action),
        "",
        _action_type_line(action),
        "",
        "| Folder | Drift | Security | Cost |",
        "|--------|-------|----------|------|",
    ]
    for item in folders:
        folder = str(item.get("folder", "unknown"))
        rows.append(
            _report_summary_row(folder, "⏳", "⏳", "—")
        )
    for item in skipped or []:
        folder = str(item.get("folder", "unknown"))
        rows.append(
            _report_summary_row(folder, "⏳", "⏳", "—")
        )
    return "\n".join(rows)


def _terminal_failure_comment(
    *,
    folder: str,
    outcome: dict[str, Any],
    artifacts: dict[str, str],
    body: str,
    action: str,
    console_url: str | None,
) -> str:
    """Assemble one terminal failure comment with its execution link attached."""
    execution = _report_execution_collapsible(console_url=console_url)
    if execution:
        body = f"{body}\n\n{execution}"
    return _wrap_collapsed(
        _report_folder_summary_line(folder, outcome, artifacts, action=action),
        body,
    )


def folder_comment(
    folder: str,
    outcome: dict[str, Any],
    artifacts: dict[str, str],
    *,
    action: str = "plan",
    commit_hash: str = "",
    console_url: str | None = None,
    run_id: str | None = None,
    repo_name: str = "",
    pr_number: int | None = None,
    existing_names: frozenset[str] | None = None,
    tmp_bucket: str = "",
    region: str = "",
    hub_account_id: str | None = None,
    identity_center_start_url: str | None = None,
    identity_center_role_name: str | None = None,
    approved_plan_pointer_key: str | None = None,
) -> str:
    if folder == "config" and outcome.get("status") == "infrastructure_error":
        error = _format_error(outcome)[:_MAX_CONFIGURATION_ERROR_CHARS]
        body = (
            "## openci-tf configuration error\n\n openci-tf did not start.\n\n"
            f"{_fenced_block(error)}"
        )
        config_outcome = {**outcome, "folder": folder, "status": "infrastructure_error"}
        return _terminal_failure_comment(
            folder=folder,
            outcome=config_outcome,
            artifacts=artifacts,
            body=body,
            action=action,
            console_url=console_url,
        )

    if folder != "config":
        _require_account_id(outcome, folder)

    if outcome.get("status") == "in_progress":
        body = f"{_action_heading(action)}\n\n{outcome.get('reply', 'Run already in progress.')}"
        return _wrap_collapsed(
            _report_folder_summary_line(folder, outcome, artifacts, action=action),
            body,
        )
    if outcome.get("status") == "infrastructure_error":
        return _terminal_failure_comment(
            folder=folder,
            outcome=outcome,
            artifacts=artifacts,
            body=_error_block("Infrastructure error", _format_error(outcome)),
            action=action,
            console_url=console_url,
        )
    if outcome.get("credential_expired"):
        return _terminal_failure_comment(
            folder=folder,
            outcome={**outcome, "status": "failed", "succeeded": False},
            artifacts=artifacts,
            body="Credentials expired while the folder run was executing.",
            action=action,
            console_url=console_url,
        )
    if outcome.get("succeeded") is False or outcome.get("status") == "failed":
        return _terminal_failure_comment(
            folder=folder,
            outcome=outcome,
            artifacts=artifacts,
            body=_error_block("Folder execution failed", _format_error(outcome)),
            action=action,
            console_url=console_url,
        )

    return _report_folder_comment(
        folder,
        outcome,
        artifacts,
        action=action,
        console_url=console_url,
        run_id=run_id,
        repo_name=repo_name,
        pr_number=pr_number,
        existing_names=existing_names,
        tmp_bucket=tmp_bucket,
        region=region,
        hub_account_id=hub_account_id,
        identity_center_start_url=identity_center_start_url,
        identity_center_role_name=identity_center_role_name,
        approved_plan_pointer_key=approved_plan_pointer_key,
    )


def _pipeline_step_rows(
    steps: list[list[str]], outcomes: list[dict[str, Any]]
) -> list[str]:
    rows = ["| Pipeline Step |", "|---------------|"]
    by_folder = {str(outcome.get("folder") or ""): outcome for outcome in outcomes}
    total = len(steps)
    for index, folders in enumerate(steps, start=1):
        status = _pipeline_step_status(folders, by_folder)
        rows.append(f"| Step {index}/{total} · {', '.join(folders)} · {status} |")
    return rows


def _pipeline_step_status(
    folders: list[str], by_folder: dict[str, dict[str, Any]]
) -> str:
    found = [by_folder.get(folder) for folder in folders]
    if not found or any(item is None for item in found):
        return "not run"
    if all(item is not None and item.get("status") == "skipped" for item in found):
        return "not run"
    for item in found:
        if item is None:
            return "not run"
        status = str(item.get("status") or "")
        if (
            status in {"failed", "infrastructure_error"}
            or item.get("succeeded") is False
        ):
            return "failed"
    return "ok"


def _pipeline_plan_preview_note(action: str) -> str:
    if action == "plan_destroy":
        return (
            "> [!CAUTION]\n"
            "> This preview shows only Terraform destroy plans, in reverse pipeline order. "
            "Security and cost analysis remain available on a regular single-folder `tf plan`."
        )
    return (
        "> [!IMPORTANT]\n"
        "> This preview shows only Terraform plans, in the order they interact. "
        "Security and cost analysis remain available on a regular single-folder `tf plan`."
    )


def _pipeline_preview_order_label(action: str) -> str:
    return "destroy order" if action == "plan_destroy" else "apply order"


def _pipeline_plan_table_cell(counts: tuple[int, int, int] | None, action: str) -> str:
    if counts is None:
        return "unavailable"
    add, change, destroy = counts
    if action == "plan_destroy":
        if destroy:
            return f"0 to add, 0 to change, **{destroy} to destroy**"
        return "0 to add, 0 to change, 0 to destroy"
    if add:
        return f"**{add} to add**, {change} to change, {destroy} to destroy"
    return f"{add} to add, {change} to change, {destroy} to destroy"


def _pipeline_plan_step_summary_line(counts: tuple[int, int, int] | None) -> str:
    if counts is None:
        return "Plan unavailable"
    add, change, destroy = counts
    return f"**Plan:** {add} to add, {change} to change, {destroy} to destroy."


def _pipeline_plan_step_collapsible(
    *,
    step_index: int,
    step_count: int,
    folder: str,
    plan_text: str,
    action: str,
) -> str:
    counts = _plan_counts(_strip_ansi(plan_text))
    if action == "plan_destroy" and counts is not None:
        title_counts = f"{counts[2]} to destroy"
    elif counts is not None and counts[0]:
        title_counts = f"{counts[0]} to add"
    elif counts is not None:
        title_counts = f"{counts[2]} to destroy" if action == "plan_destroy" else "no changes"
    else:
        title_counts = "plan unavailable"
    clean = _neutralize_comment_identity_lines(_strip_ansi(plan_text))
    bounded = _bound_plan_display_text(clean, max_chars=_REPORT_PLAN_CHARS)
    body_parts = [
        _append_plan_truncation_note(
            _fenced_block(_highlight_plan(bounded.text), "diff"),
            truncated=bounded.truncated,
        ),
        "",
        _pipeline_plan_step_summary_line(counts),
    ]
    summary = f"Step {step_index}/{step_count} · `{folder}` · {title_counts}"
    return _wrap_collapsed(summary, "\n".join(body_parts))


def _ordered_pipeline_preview_outcomes(
    outcomes: list[dict[str, Any]],
    steps: list[list[str]] | None,
) -> list[dict[str, Any]]:
    if not steps:
        return list(outcomes)
    by_folder = {str(item.get("folder") or ""): item for item in outcomes}
    ordered: list[dict[str, Any]] = []
    for folders in steps:
        for folder in folders:
            outcome = by_folder.get(folder)
            if outcome is not None:
                ordered.append(outcome)
    return ordered


def pipeline_plan_preview_comment(
    outcomes: list[dict[str, Any]],
    artifacts_by_folder: dict[str, dict[str, str]],
    *,
    action: str = "plan",
    steps: list[list[str]] | None = None,
) -> str:
    """Focused pipeline plan preview without security, cost, or per-folder report sections."""
    ordered = _ordered_pipeline_preview_outcomes(outcomes, steps)
    rows: list[_ReportRow] = []
    total_add = 0
    total_change = 0
    total_destroy = 0
    for outcome in ordered:
        folder = str(outcome.get("folder", "unknown"))
        artifacts = artifacts_by_folder.get(folder, {})
        row = _report_row(outcome, artifacts, action=action)
        rows.append(row)
        if row.plan_counts is not None:
            add, change, destroy = row.plan_counts
            total_add += add
            total_change += change
            total_destroy += destroy
    step_count = len(steps) if steps else 1
    lines = [
        f"> **Pipeline plan preview · {_pipeline_preview_order_label(action)}**",
        "",
        _pipeline_plan_preview_note(action),
        "",
        f"**{len(rows)} folders** · **{total_add} resources to add** · "
        f"**{total_change} to change** · **{total_destroy} to destroy**",
        "",
        "| Step | Folder | Plan |",
        "|---|---|---|",
    ]
    step_index = 0
    for folders in steps or [[]]:
        for folder in folders:
            step_index += 1
            row = next((item for item in rows if item.folder == folder), None)
            counts = row.plan_counts if row is not None else None
            lines.append(
                f"| {step_index}/{step_count} | `{folder}` | "
                f"{_pipeline_plan_table_cell(counts, action)} |"
            )
    lines.append("")
    step_index = 0
    for folders in steps or [[]]:
        for folder in folders:
            step_index += 1
            artifacts = artifacts_by_folder.get(folder, {})
            plan_text = _human_plan_text(action, artifacts)
            lines.append(
                _pipeline_plan_step_collapsible(
                    step_index=step_index,
                    step_count=step_count,
                    folder=folder,
                    plan_text=plan_text,
                    action=action,
                )
            )
            lines.append("")
    return "\n".join(lines).rstrip()


def _pipeline_mutation_note(action: str) -> str:
    if action == "destroy":
        return (
            "> [!CAUTION]\n"
            "> This pipeline was previewed with `tf plan --destroy pipeline <name>`. "
            "Destroy runs in reverse pipeline order. Before each folder is destroyed, "
            "openci-tf requires a fresh destroy plan using the state left by the previous "
            "checkpoint. That exact pinned destroy plan requires its own confirmation."
        )
    return (
        "> [!IMPORTANT]\n"
        "> This pipeline was previewed with `tf plan pipeline <name>`. Before each folder "
        "is applied, openci-tf requires a fresh plan using the state left by the previous "
        "checkpoint. That exact pinned plan requires its own confirmation."
    )


def _pipeline_mutation_result_label(action: str, *, succeeded: bool) -> str:
    if action == "apply":
        return "Apply succeeded ✅" if succeeded else "Apply failed ❌"
    return "Destroy succeeded ✅" if succeeded else "Destroy failed ❌"


def _pipeline_mutation_plan_label(action: str, *, replanned: bool) -> str:
    if replanned:
        return "Replanned after prior checkpoint"
    if action == "destroy":
        return "Fresh destroy plan"
    return "Fresh plan"


def pipeline_mutation_aggregate_comment(
    *,
    action: str,
    pipeline: str,
    commit_hash: str,
    requested_command: str,
    checkpoint_index: int,
    checkpoint_count: int,
    folder: str,
    account_id: str,
    succeeded: bool,
    plan_show_text: str | None = None,
    pinned_plan_artifact: str,
    replanned_after_prior: bool = False,
    confirmation_status: str = "Confirmed ✅",
    footer: str | None = None,
    metadata_lines: list[str] | None = None,
) -> str:
    """Render one stable aggregate pipeline apply/destroy checkpoint comment."""
    result = _pipeline_mutation_result_label(action, succeeded=succeeded)
    plan_label = _pipeline_mutation_plan_label(
        action, replanned=replanned_after_prior
    )
    lines = [
        f"> **Pipeline {action} · checkpoint {checkpoint_index}/{checkpoint_count}**",
        "",
        _pipeline_mutation_note(action),
        "",
        f"**{checkpoint_index} checkpoint{'s' if checkpoint_count != 1 else ''}** · "
        f"**{1 if succeeded else 0} succeeded** · **{0 if succeeded else 1} failed**",
        "",
        "| Step | Folder | Plan | Confirmation | Result |",
        "|---|---|---|---|---|",
        (
            f"| {checkpoint_index}/{checkpoint_count} | `{folder}` | {plan_label} | "
            f"{confirmation_status} | {result} |"
        ),
        "",
        _wrap_collapsed(
            f"Step {checkpoint_index}/{checkpoint_count} · `{folder}` · "
            f"{account_id} · {result}",
            "\n\n".join(
                part
                for part in [
                    _mutation_plan_collapsible(
                        plan_show_text,
                        pinned_plan_artifact=pinned_plan_artifact,
                    ),
                ]
                if part
            ),
        ),
    ]
    if footer:
        lines.extend(["", footer])
    if metadata_lines:
        lines.extend(["", _wrap_collapsed("Metadata", "\n".join(metadata_lines))])
    return bound_comment("\n".join(lines))


def summary(
    outcomes: list[dict[str, Any]],
    artifacts_by_folder: dict[str, dict[str, str]] | None = None,
    *,
    action: str = "plan",
    folder_urls: dict[str, str] | None = None,
    commit_hash: str = "",
    console_url: str | None = None,
    steps: list[list[str]] | None = None,
) -> str:
    if action in {"plan", "plan_destroy", "drift", "report"}:
        return _report_summary(
            outcomes,
            artifacts_by_folder,
            action=action,
            folder_urls=folder_urls,
            steps=steps,
        )
    if action in {"apply", "destroy"}:
        return _mutation_summary(
            outcomes,
            action=action,
            folder_urls=folder_urls,
        )
    raise ValueError(f"unsupported summary action: {action}")
