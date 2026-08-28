# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure markdown rendering for bounded execution artifacts."""

from __future__ import annotations

import html
import json
import re
import time
from typing import Any

from src.core.terminal_evidence import redact_and_bound_terminal_evidence
from src.domain.engine.artifact_paths import (
    latest_plan_pointer,
    pr_pointer_key,
    run_scoped_plan_pointer,
)
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
    tfsec,
)

_MAX_CONFIGURATION_ERROR_CHARS = 465
_ACCOUNT_ID = re.compile(r"^\d{12}$")
_COMMENT_OBJECT_ID_LINE = re.compile(r"\bcomment_object_id\s*:", re.IGNORECASE)
_HIDDEN_AUDIT_ROW_ID = re.compile(r"<!--\s*[dl]:[^\s>]+\s*-->")
_HIDDEN_DELIVERY_ID_LINE = re.compile(
    r"^\s*<!--\s*openci-tf:[^>]*\bdelivery:[^>]+-->\s*$"
)
_LEGACY_COMMENT_ID_LINE = re.compile(
    r"^\s*#openci-tf:::(?:tag::|status_comment\b).*$"
)
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
        return f"- triggering comment id: `{comment_id}` (removed after acknowledgement)"
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
        "Commands must be posted on an open pull request."
        + detail
    )


_PLAN_SUMMARY_ACTIONS = frozenset({"plan", "report", "plan_destroy"})


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
    return "Plan" if action in _PLAN_SUMMARY_ACTIONS else "Drift Check"


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
        source_label = "source destroy-plan run id" if action == "destroy" else "source plan run id"
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
        pointer_label = "Destroy plan pointer" if pointer_type == "destroy" else "Plan pointer"
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
    rows = [
        "## Terraform Multi-Folder Summary",
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
        body = (
            f"{_folder_heading(folder, account_id, action=action)}"
            f"{_error_block('Infrastructure error', _format_error(outcome))}"
        )
        return _wrap_collapsed(
            _summary_line(folder, account_id, commit_hash, status_label), body
        )
    if outcome.get("credential_expired"):
        body = f"{_folder_heading(folder, account_id, action=action)} Credentials expired while the folder run was executing."
        return _wrap_collapsed(
            _summary_line(folder, account_id, commit_hash, status_label), body
        )
    if outcome.get("succeeded") is False or outcome.get("status") == "failed":
        body = (
            f"{_folder_heading(folder, account_id, action=action)}"
            f"{_error_block('Folder execution failed', _format_error(outcome))}"
        )
        return _wrap_collapsed(
            _summary_line(folder, account_id, commit_hash, status_label), body
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


def _pipeline_step_rows(steps: list[list[str]], outcomes: list[dict[str, Any]]) -> list[str]:
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
        if status in {"failed", "infrastructure_error"} or item.get("succeeded") is False:
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
