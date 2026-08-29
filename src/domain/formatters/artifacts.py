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
    latest_plan_pointer,
    pointer_type_for_action,
    pr_pointer_key,
    run_scoped_plan_pointer,
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
)
from src.domain.formatters.infracost_table import render_infracost_table
from src.domain.formatters.tfsec_findings import (  # noqa: F401  (re-exported)
    _analyze_tfsec_severity,
    _artifact_skipped,
    _strip_ansi,
    _tfsec_text_from_json,
    tfsec,
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


def _terminal_label(action: str, outcome: dict[str, Any], *, folder: str) -> str:
    status = outcome.get("status")
    if status == "in_progress":
        return "In progress"
    if status == "skipped":
        return "Not run"
    if outcome.get("credential_expired"):
        return "Credentials expired"
    if folder == "config" and status == "infrastructure_error":
        return "Configuration error"
    if status == "infrastructure_error":
        return "Infrastructure error"
    if outcome.get("succeeded") is False or status == "failed":
        return "Failed"
    return {
        "plan": "Plan succeeded",
        "drift": "Drift check succeeded",
        "report": "Report succeeded",
        "apply": "Apply succeeded",
        "destroy": "Destroy succeeded",
    }.get(action, f"{action.title()} succeeded")


def _summary_line(
    folder: str, account_id: str, commit_hash: str, status_label: str
) -> str:
    account = account_id if account_id else "—"
    return f"`{folder}` · {account} · {_short_hash(commit_hash)} · {status_label}"


def _wrap_collapsed(summary_line: str, body: str) -> str:
    return f"<details>\n<summary>{_html_escape(summary_line)}</summary>\n\n{body.rstrip()}\n\n</details>"


def _folder_heading(folder: str, account_id: str, *, action: str = "plan") -> str:
    if action == "report":
        return f'## Report - "{folder}" ({account_id})\n\n'
    return f"## Terraform: `{folder}` ({account_id})\n\n"


def section(title: str, text: str, language: str = "") -> str:
    body = _fenced_block(text, language) if text else "No artifact was produced."
    return f"### {title}\n<details>\n<summary>show details</summary>\n\n{body}\n\n</details>"


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


def mutation_command_context_block(
    *,
    action: str,
    requested_comment_body: str | None,
    requested_comment_id: int | None = None,
    requested_comment_link: str | None = None,
    confirmation_comment_body: str | None = None,
    confirmation_comment_id: int | None = None,
    confirmation_comment_link: str | None = None,
    run_id: str | None = None,
    commit_hash: str | None = None,
    comments_removed: bool = False,
) -> str:
    """Command header for terminal apply/destroy comments with both request and confirm."""
    requested_line = _describe_command_line(
        action,
        folders=None,
        comment_body=requested_comment_body,
    )
    confirmation_line = (
        normalized_command_context_line(confirmation_comment_body)
        if confirmation_comment_body and confirmation_comment_body.strip()
        else f"tf {action} confirm <redacted>"
    )
    lines = [
        "### openci-tf command",
        "",
        f"- requested command: `{requested_line}`",
        f"- confirmation command: `{confirmation_line}`",
    ]
    if requested_comment_id is not None:
        if comments_removed or not requested_comment_link:
            lines.append(
                f"- requested comment id: `{requested_comment_id}` (removed after acknowledgement)"
            )
        else:
            lines.append(
                f"- requested comment: [{requested_comment_id}]({requested_comment_link})"
            )
    if confirmation_comment_id is not None:
        if comments_removed or not confirmation_comment_link:
            lines.append(
                f"- confirmation comment id: `{confirmation_comment_id}` (removed after acknowledgement)"
            )
        else:
            lines.append(
                f"- confirmation comment: [{confirmation_comment_id}]({confirmation_comment_link})"
            )
    if run_id:
        lines.append(f"- run id: `{run_id}`")
    if commit_hash:
        lines.append(f"- commit: `{_short_hash(commit_hash)}`")
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


_PLAN_SUMMARY_ACTIONS = frozenset({"plan", "plan_destroy"})
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


def _plan_cell(plan_text: str) -> str:
    summary = extract_plan_summary(plan_text)
    if not summary:
        return "unknown"
    add = int(summary["add"])
    change = int(summary["change"])
    destroy = int(summary["destroy"])
    if add == 0 and change == 0 and destroy == 0:
        return "no changes"
    return f"+{add} ~{change} -{destroy}"


def _drift_cell(plan_text: str) -> str:
    summary = extract_plan_summary(plan_text)
    if not summary:
        return "unknown"
    if all(int(summary[key]) == 0 for key in ("add", "change", "destroy")):
        return "clean"
    return "changes"


def _summary_delta_header(action: str) -> str:
    if action == "report":
        return "Drift"
    return "Plan" if action in _PLAN_SUMMARY_ACTIONS else "Drift Check"


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


def _report_row(outcome: dict[str, Any], artifacts: dict[str, str]) -> _ReportRow:
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
        plan_counts=_plan_counts(artifacts.get("tf/plan.out", "")),
        security=security,
        finding_count=finding_count,
        cost=_cost_cell(artifacts.get("infracost.json", "{}")),
    )


def _report_rows(
    outcomes: list[dict[str, Any]], artifacts_by_folder: dict[str, dict[str, str]]
) -> list[_ReportRow]:
    return [
        _report_row(
            outcome, artifacts_by_folder.get(str(outcome.get("folder", "")), {})
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
) -> str:
    row = _report_row({**outcome, "folder": folder}, artifacts)
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
    parts.append(_fenced_block(_highlight_plan(clean), "diff"))
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


def _report_artifact_layout_keys(
    *,
    repo_name: str,
    run_id: str,
    folder: str,
    pr_number: int | None,
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
            pointer_type = pointer_type_for_action("report")
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
) -> str:
    keys = _report_artifact_layout_keys(
        repo_name=repo_name,
        run_id=run_id,
        folder=folder,
        pr_number=pr_number,
    )
    by_name = {
        "manifest.json": keys.manifest_json,
        "init.out": keys.init_out,
        "validate.out": keys.validate_out,
        "tf/plan.out": keys.plan_out,
        "tf/plan.tfplan": keys.plan_tfplan,
        "tf/plan.tfplan.sha256": keys.plan_sha256,
        "tf/plan-metadata.json": keys.plan_metadata,
        "tfsec.output": keys.tfsec_output,
        "tfsec.json": keys.tfsec_json,
        "infracost.output": keys.infracost_output,
        "infracost.json": keys.infracost_json,
    }
    return by_name[storage_name]


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
) -> str:
    if not repo_name or not run_id or not tmp_bucket or not region:
        return ""
    lines: list[str] = []
    for group_name, members in _REPORT_ARTIFACT_GROUPS:
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
    if not lines:
        return ""
    return _report_child_collapsible("Artifacts", "\n".join(lines))


def _report_execution_collapsible(
    *,
    console_url: str | None,
    codebuild_url: str | None = None,
) -> str:
    lines: list[str] = []
    if console_url:
        lines.append(f"[Step Functions execution]({console_url})")
    if codebuild_url:
        lines.append(f"[CodeBuild job]({codebuild_url})")
    if not lines:
        return ""
    return _report_child_collapsible("Execution", "\n".join(lines))


def _report_folder_comment(
    folder: str,
    outcome: dict[str, Any],
    artifacts: dict[str, str],
    *,
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
) -> str:
    _require_account_id(outcome, folder)
    names = existing_names or frozenset(artifacts)
    parts = [
        _report_setup_collapsible(
            artifacts.get("init.out", ""), artifacts.get("validate.out", "")
        ),
        _report_plan_collapsible(artifacts.get("tf/plan.out", "")),
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
        ),
    ]
    return _wrap_collapsed(
        _report_folder_summary_line(folder, outcome, artifacts),
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
    folder_cell = (
        f"[`{folder}`]({folder_urls[folder]})"
        if folder_urls and folder in folder_urls
        else f"`{folder}`"
    )
    return f"| {folder_cell} | {drift_icon} | {security_icon} | {cost} |"


def _report_summary(
    outcomes: list[dict[str, Any]],
    artifacts_by_folder: dict[str, dict[str, str]] | None = None,
    *,
    folder_urls: dict[str, str] | None = None,
    steps: list[list[str]] | None = None,
) -> str:
    artifacts = artifacts_by_folder or {}
    report_rows = _report_rows(outcomes, artifacts)
    attention = sorted(
        (row for row in report_rows if row.needs_attention),
        key=lambda row: row.sort_key,
    )
    clean = sorted(
        (row for row in report_rows if row.clean), key=lambda row: row.folder
    )
    lines: list[str] = ["## openci-tf report", "", "**Type:** Report", ""]
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


def _summary_delta_cell(action: str, plan_text: str) -> str:
    if action in _PLAN_SUMMARY_ACTIONS:
        return _plan_cell(plan_text)
    return _drift_cell(plan_text)


def _security_cell(tfsec_text: str) -> str:
    severity = _analyze_tfsec_severity(tfsec_text)
    return {
        "success": "clean",
        "high": "high",
        "medium": "medium",
        "low": "low",
    }.get(severity, "unknown")


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


def _mutation_pinned_plan_section(
    plan_show_text: str | None,
    *,
    plan_show_pointer: str | None = None,
) -> str | None:
    """Render bounded pinned-plan output inside a collapsed details block."""
    if plan_show_pointer and not plan_show_text:
        body = f"+ plan show output: `{plan_show_pointer}`"
        return _wrap_collapsed("Pinned plan (tofu show)", body)
    if not plan_show_text:
        return None
    bounded = plan_show_text[:8000]
    if len(plan_show_text) > 8000:
        bounded += "\n\n> Output truncated. See S3 artifacts for full plan show."
    body = _fenced_block(bounded)
    if plan_show_pointer:
        body += f"\n\n+ plan show output: `{plan_show_pointer}`"
    return _wrap_collapsed("Pinned plan (tofu show)", body)


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
) -> str:
    """Terminal apply/destroy folder comment with bounded plan show output."""
    verb = action.title()
    status = "succeeded" if succeeded else "failed"
    lines = [
        f"## {verb} {status} — `{folder}` ({account_id})",
        f"+ pinned plan: `{pinned_plan_artifact}`",
    ]
    if source_plan_run_id:
        source_label = (
            "source destroy-plan run id"
            if action == "destroy"
            else "source plan run id"
        )
        lines.append(f"+ {source_label}: `{source_plan_run_id}`")
    lines.append(f"+ commit: `{commit_hash[:7] if commit_hash else 'unknown'}`")
    if console_url:
        lines.append(f"+ [Step Functions execution]({console_url})")
    if codebuild_url:
        account_note = (
            f" — hub account `{codebuild_account_id}`; switch the AWS console to this account first"
            if codebuild_account_id
            else ""
        )
        lines.append(f"+ [CodeBuild job]({codebuild_url}){account_note}")
    if error:
        bounded_error = redact_and_bound_terminal_evidence(error)
        if not isinstance(bounded_error, str):
            raise TypeError("mutation terminal error must be a string")
        lines.append(_error_block("+ error", bounded_error))
    plan_section = _mutation_pinned_plan_section(
        plan_show_text, plan_show_pointer=plan_show_pointer
    )
    if plan_section:
        lines.append(plan_section)
    return "\n".join(lines)


def ci_details(commit_hash: str, console_url: str | None, status: str) -> str:
    short = commit_hash[:7] if commit_hash else "unknown"
    icon = " SUCCESS" if status == "succeeded" else " FAILED"
    lines = ["\n## CI Details", f"+ `{short}`"]
    if console_url:
        lines.append(f"+ [ci pipeline]({console_url})")
    lines.append(f"+ {icon}")
    return "\n".join(lines)


def initialize(text: str) -> str:
    return section("1️⃣ Initialize", _decorate_init(text))


def validate(text: str) -> str:
    clean = _neutralize_comment_identity_lines(_strip_ansi(text))
    if "success!" in clean.lower() or "configuration is valid" in clean.lower():
        body = "\n **Configuration is valid**\n"
    else:
        body = f"\n **Validation failed**\n\n{_fenced_block(clean)}"
    return f"### Validate\n<details>\n<summary>show details</summary>\n{body}\n\n</details>"


def _decorate_init(text: str) -> str:
    result = []
    for line in _strip_ansi(text).splitlines():
        if "Initializing the backend" in line:
            result.append("🔧 " + line)
        elif "Initializing provider plugins" in line:
            result.append("🔌 " + line)
        elif "- Installing" in line or "- Using" in line:
            result.append("   " + line.strip("- "))
        elif "successfully initialized" in line.lower():
            result.append("\n " + line)
        elif "warning" in line.lower() or "error" in line.lower():
            result.append(" " + line)
        else:
            result.append(line)
    return "\n".join(result)


def plan(text: str) -> str:
    text = _neutralize_comment_identity_lines(_strip_ansi(text))
    summary = extract_plan_summary(text)
    parts: list[str] = []
    if summary:
        parts.extend(
            [
                "\n** Plan Summary:**",
                f"- **{summary['add']} to add**",
                f"- **{summary['change']} to change**",
                f"- **{summary['destroy']} to destroy**",
                "",
            ]
        )
    highlighted = "\n".join(
        "+ " + line
        if line.strip().startswith("+ resource")
        else "- " + line
        if line.strip().startswith("- resource")
        else "! " + line
        if line.strip().startswith(("~ resource", "# resource"))
        else line
        for line in text.splitlines()
    )
    parts.append(_fenced_block(highlighted, "diff"))
    body = "\n".join(parts)
    return f"### Plan\n<details>\n<summary>show details</summary>\n{body}\n\n</details>"


def plan_artifact_pointer(
    *,
    repo_name: str,
    run_id: str,
    folder: str,
    pr_number: int | None = None,
    pointer_type: str = "plan",
) -> str:
    if pr_number is not None:
        plan_env = pr_pointer_key(
            repo_name=repo_name,
            pr_number=pr_number,
            folder_path=folder,
            pointer_type=pointer_type,
        )
        pointer_label = (
            "Destroy plan pointer" if pointer_type == "destroy" else "Plan pointer"
        )
        return (
            "### Plan Artifact\n"
            f"- Execution ID: `{run_id}`\n"
            f"- {pointer_label}: `{plan_env}`\n\n"
            "> Checksums and expiry live in manifest.json."
        )
    latest = latest_plan_pointer(repo_name=repo_name, folder_path=folder)
    run_scoped = run_scoped_plan_pointer(
        repo_name=repo_name, run_id=run_id, folder_path=folder
    )
    return (
        "### Plan Artifact\n"
        f"- Execution ID: `{run_id}`\n"
        f"- Latest plan: `{latest}`\n"
        f"- Run-scoped plan: `{run_scoped}`\n\n"
        "> Checksums and expiry live in manifest.json."
    )


def execution_artifacts_section(
    run_id: str,
    manifest_s3_uri: str,
) -> str:
    """Copyable execution-artifact pointers for PR comments and registry consumers."""
    return (
        "### Execution Artifacts\n"
        f"- Execution ID: `{run_id}`\n"
        f"- Manifest: `{manifest_s3_uri}`\n"
    )


def infracost(text: str) -> str:
    if _artifact_skipped(text, "not run"):
        return ""
    body = "\n **Cost Analysis**\n\n"
    try:
        data = json.loads(text)
        skipped = data.get("skipped")
    except json.JSONDecodeError:
        # Unparseable artifact: render the explicit unavailable message
        # instead of an empty summary plus a garbage table.
        body += "Cost data unavailable (invalid JSON).\n"
        return (
            "### Cost Analysis\n<details>\n<summary>show details</summary>\n"
            f"{body}\n</details>"
        )
    if skipped:
        body += "**Summary:**\n- Cost analysis: not configured\n\n"
    else:
        monthly = extract_infracost_monthly(text)
        if monthly not in {"N/A", "not configured"}:
            body += f"**Summary:**\n- Monthly cost {monthly}\n\n"
    table = render_infracost_table(text)
    body += _fenced_block(table)
    return f"### Cost Analysis\n<details>\n<summary>show details</summary>\n{body}\n\n</details>"


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
    status_label = _running_label(action)
    body = f"{_folder_heading(folder, account_id, action=action)} {status_label} at `{_short_hash(commit_hash)}`…"
    return _wrap_collapsed(
        _summary_line(folder, account_id, commit_hash, status_label), body
    )


def pending_summary_all(commit_hash: str, action: str) -> str:
    short = commit_hash[:7]
    verb = _running_label(action)
    if action == "report":
        return f"## openci-tf report\n\n{verb} all folders at `{short}`…"
    return f"## Terraform Multi-Folder Summary\n\n {verb} all folders at `{short}`…"


def _summary_row(
    folder: str,
    account_id: str,
    drift: str,
    security: str,
    cost: str,
    *,
    folder_urls: dict[str, str] | None = None,
) -> str:
    folder_cell = (
        f"[`{folder}`]({folder_urls[folder]})"
        if folder_urls and folder in folder_urls
        else f"`{folder}`"
    )
    return f"| {folder_cell} | `{account_id}` | {drift} | {security} | {cost} |"


def pending_summary(
    folders: list[dict[str, Any]],
    skipped: list[dict[str, Any]] | None = None,
    *,
    action: str = "plan",
) -> str:
    delta_header = _summary_delta_header(action)
    heading = (
        "## openci-tf report"
        if action == "report"
        else "## Terraform Multi-Folder Summary"
    )
    rows = [
        heading,
        "",
        f"| Folder | Account | {delta_header} | Security | Cost |",
        "|--------|---------|------------|----------|------|",
    ]
    for item in folders:
        folder = str(item.get("folder", "unknown"))
        account_id = _require_account_id(item, folder)
        rows.append(
            _summary_row(
                folder, account_id, "in progress", "in progress", "in progress"
            )
        )
    for item in skipped or []:
        folder = str(item.get("folder", "unknown"))
        account_id = _require_account_id(item, folder)
        rows.append(
            _summary_row(
                folder, account_id, "in progress", "in progress", "in progress"
            )
        )
    return "\n".join(rows)


def _terminal_failure_execution_section(
    *,
    action: str,
    console_url: str | None,
    commit_hash: str,
    include_ci_details: bool,
) -> str:
    """Deterministic execution-link section for terminal failure bodies.

    Report comments keep the finalized report surface: the Step Functions link
    is a separate Execution child and CodeBuild never appears. Plan and drift
    keep the CI Details convention used by terminal single-folder comments.
    """
    if not console_url:
        return ""
    if action == "report":
        return _report_execution_collapsible(console_url=console_url)
    if include_ci_details and commit_hash:
        return ci_details(commit_hash, console_url, "failed")
    return ""


def _terminal_failure_comment(
    *,
    folder: str,
    account_id: str,
    commit_hash: str,
    status_label: str,
    body: str,
    action: str,
    console_url: str | None,
    include_ci_details: bool,
) -> str:
    """Assemble one terminal failure comment with its execution link attached."""
    execution = _terminal_failure_execution_section(
        action=action,
        console_url=console_url,
        commit_hash=commit_hash,
        include_ci_details=include_ci_details,
    )
    if execution:
        body = f"{body}\n\n{execution}"
    return _wrap_collapsed(
        _summary_line(folder, account_id, commit_hash, status_label), body
    )


def folder_comment(
    folder: str,
    outcome: dict[str, Any],
    artifacts: dict[str, str],
    *,
    action: str = "plan",
    commit_hash: str = "",
    console_url: str | None = None,
    include_ci_details: bool = True,
    manifest_s3_uri: str | None = None,
    run_id: str | None = None,
    repo_name: str = "",
    pr_number: int | None = None,
    existing_names: frozenset[str] | None = None,
    tmp_bucket: str = "",
    region: str = "",
    hub_account_id: str | None = None,
    identity_center_start_url: str | None = None,
    identity_center_role_name: str | None = None,
) -> str:
    if folder == "config" and outcome.get("status") == "infrastructure_error":
        status_label = _terminal_label(action, outcome, folder=folder)
        error = _format_error(outcome)[:_MAX_CONFIGURATION_ERROR_CHARS]
        body = (
            "## openci-tf configuration error\n\n openci-tf did not start.\n\n"
            f"{_fenced_block(error)}"
        )
        return _wrap_collapsed(
            _summary_line(folder, "", commit_hash, status_label), body
        )

    account_id = _require_account_id(outcome, folder)
    status_label = _terminal_label(action, outcome, folder=folder)

    if outcome.get("status") == "in_progress":
        body = f"{_folder_heading(folder, account_id, action=action)} {outcome.get('reply', 'Run already in progress.')}"
        return _wrap_collapsed(
            _summary_line(folder, account_id, commit_hash, status_label), body
        )
    if outcome.get("status") == "infrastructure_error":
        return _terminal_failure_comment(
            folder=folder,
            account_id=account_id,
            commit_hash=commit_hash,
            status_label=status_label,
            body=(
                f"{_folder_heading(folder, account_id, action=action)}"
                f"{_error_block('Infrastructure error', _format_error(outcome))}"
            ),
            action=action,
            console_url=console_url,
            include_ci_details=include_ci_details,
        )
    if outcome.get("credential_expired"):
        return _terminal_failure_comment(
            folder=folder,
            account_id=account_id,
            commit_hash=commit_hash,
            status_label=status_label,
            body=(
                f"{_folder_heading(folder, account_id, action=action)}"
                " Credentials expired while the folder run was executing."
            ),
            action=action,
            console_url=console_url,
            include_ci_details=include_ci_details,
        )
    if outcome.get("succeeded") is False or outcome.get("status") == "failed":
        return _terminal_failure_comment(
            folder=folder,
            account_id=account_id,
            commit_hash=commit_hash,
            status_label=status_label,
            body=(
                f"{_folder_heading(folder, account_id, action=action)}"
                f"{_error_block('Folder execution failed', _format_error(outcome))}"
            ),
            action=action,
            console_url=console_url,
            include_ci_details=include_ci_details,
        )

    if action == "report":
        return _report_folder_comment(
            folder,
            outcome,
            artifacts,
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
        )

    sections = [
        _folder_heading(folder, account_id, action=action).rstrip(),
        initialize(artifacts.get("init.out", "")),
        validate(artifacts.get("validate.out", "")),
    ]
    plan_output_key = "destroy.plan.out" if action == "plan_destroy" else "tf/plan.out"
    sections.append(plan(artifacts.get(plan_output_key, "")))
    if action in {"plan", "report", "plan_destroy"}:
        pointer_type = "destroy" if action == "plan_destroy" else "plan"
        sections.extend(
            [
                plan_artifact_pointer(
                    repo_name=repo_name,
                    run_id=str(run_id or ""),
                    folder=folder,
                    pr_number=pr_number,
                    pointer_type=pointer_type,
                )
                if run_id and repo_name
                else "",
                tfsec(artifacts.get("tfsec.json", "")),
                infracost(artifacts.get("infracost.json", "")),
            ]
        )
    parts = [part for part in sections if part]
    if include_ci_details and console_url and commit_hash:
        parts.append(
            ci_details(
                commit_hash,
                console_url,
                "succeeded" if outcome.get("succeeded", True) else "failed",
            )
        )
    if manifest_s3_uri and run_id:
        parts.append(execution_artifacts_section(run_id, manifest_s3_uri))
    body = "\n\n".join(parts)
    return _wrap_collapsed(
        _summary_line(folder, account_id, commit_hash, status_label), body
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
    if action == "report":
        return _report_summary(
            outcomes,
            artifacts_by_folder,
            folder_urls=folder_urls,
            steps=steps,
        )
    delta_header = _summary_delta_header(action)
    rows = ["## Terraform Multi-Folder Summary", ""]
    if steps is not None and len(steps) > 1:
        rows.extend(_pipeline_step_rows(steps, outcomes))
        rows.append("")
    rows.extend(
        [
            f"| Folder | Account | {delta_header} | Security | Cost |",
            "|--------|---------|------------|----------|------|",
        ]
    )
    for outcome in outcomes:
        folder = str(outcome.get("folder", "unknown"))
        account_id = "—" if folder == "config" else _account_cell(outcome, folder)
        status = outcome.get(
            "status", "succeeded" if outcome.get("succeeded") else "unknown"
        )
        if status == "in_progress":
            drift, security, cost = "in progress", "in progress", "in progress"
        elif status == "skipped":
            drift, security, cost = "not run", "not run", "n/a"
        elif (
            status in {"failed", "infrastructure_error"}
            or outcome.get("succeeded") is False
        ):
            drift, security, cost = "failed", "not run", "n/a"
        else:
            artifacts = (artifacts_by_folder or {}).get(folder, {})
            drift = _summary_delta_cell(action, artifacts.get("tf/plan.out", ""))
            security = _security_cell(artifacts.get("tfsec.json", ""))
            cost = _cost_cell(artifacts.get("infracost.json", "{}"))
        rows.append(
            _summary_row(
                folder, account_id, drift, security, cost, folder_urls=folder_urls
            )
        )
    body = "\n".join(rows)
    if console_url and commit_hash:
        body += ci_details(commit_hash, console_url, _terminal_status(outcomes))
    return body
