# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""tfsec artifact rendering: JSON findings to bounded markdown sections."""

from __future__ import annotations

import json
import re
from typing import Any


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text)


def _artifact_skipped(text: str, reason: str) -> bool:
    if not text:
        return True
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("skipped") is True and payload.get("reason") == reason


def _valid_tfsec_findings(findings: Any) -> list[dict[str, Any]] | None:
    if not isinstance(findings, list):
        return None
    if not findings:
        return []
    validated: list[dict[str, Any]] = []
    for item in findings:
        if not isinstance(item, dict):
            return None
        severity = item.get("severity")
        if not isinstance(severity, str) or not severity.strip():
            return None
        validated.append(item)
    return validated


def _analyze_tfsec_severity(text: str) -> str:
    if not text or not text.strip():
        return "unknown"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "unknown"
    if not isinstance(payload, dict) or not payload or payload.get("skipped") is True:
        return "unknown"
    if "results" not in payload and "findings" not in payload:
        return "unknown"
    findings = _valid_tfsec_findings(
        payload["results"] if "results" in payload else payload["findings"],
    )
    if findings is None:
        return "unknown"
    if not findings:
        return "success"
    severities = {str(item.get("severity", "")).upper() for item in findings}
    if severities & {"CRITICAL", "HIGH"}:
        return "high"
    if "MEDIUM" in severities:
        return "medium"
    if "LOW" in severities:
        return "low"
    return "unknown"


def _tfsec_findings_list(payload: dict[str, Any]) -> list[Any] | None:
    if "results" in payload:
        findings = payload["results"]
    elif "findings" in payload:
        findings = payload["findings"]
    else:
        return None
    if not isinstance(findings, list):
        return None
    return findings


def _tfsec_text_from_json(text: str) -> str:
    """Render tfsec JSON as human-readable text matching the legacy .out layout."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _strip_ansi(text)

    if not isinstance(payload, dict):
        return "Security data unavailable.\n"
    if payload.get("skipped"):
        return ""

    findings = _tfsec_findings_list(payload)
    if findings is None:
        return "Security data unavailable.\n"
    validated = _valid_tfsec_findings(findings)
    if validated is None:
        return "Security data unavailable.\n"
    if not validated:
        return "No problems detected.\n"

    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
    sorted_findings = sorted(
        validated,
        key=lambda item: severity_order.get(str(item.get("severity", "")).upper(), 99),
    )

    counts: dict[str, int] = {}
    for item in validated:
        severity = str(item.get("severity", "UNKNOWN")).upper()
        counts[severity] = counts.get(severity, 0) + 1

    lines = ["Results", ""]
    for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
        if severity in counts:
            lines.append(f"{severity.lower()} {counts[severity]}")
    lines.append("")

    for index, item in enumerate(sorted_findings, 1):
        severity = str(item.get("severity", "UNKNOWN")).upper()
        description = (
            item.get("rule_description")
            or item.get("description")
            or item.get("rule_id")
            or "Finding"
        )
        lines.append(f"Result #{index} {severity} {description}")
        lines.append("─" * 77)
        location = item.get("location") or {}
        filename = location.get("filename", "")
        start = location.get("start_line")
        end = location.get("end_line")
        if filename and start:
            end_suffix = f"-{end}" if end and end != start else ""
            lines.append(f"  {filename}:{start}{end_suffix}")
        resource = item.get("resource") or location.get("resource")
        if resource:
            lines.append(f"   via {resource}")
        impact = item.get("impact")
        if impact:
            lines.append(f"  Impact: {impact}")
        resolution = item.get("resolution")
        if resolution:
            lines.append(f"  Resolution: {resolution}")
        for link in item.get("links") or []:
            url = link if isinstance(link, str) else link.get("url") or link.get("href")
            if url:
                lines.append(f"  See {url}")
                break
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def tfsec_summary_line(text: str) -> tuple[str, int | None]:
    """Return report security severity and finding count when the JSON is valid."""
    if not text or not text.strip():
        return "unknown", None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "unknown", None
    if not isinstance(payload, dict) or not payload or payload.get("skipped"):
        return "unknown", None
    findings = _tfsec_findings_list(payload)
    if findings is None:
        return "unknown", None
    validated = _valid_tfsec_findings(findings)
    if validated is None:
        return "unknown", None
    if not validated:
        return "clean", 0
    severities = {str(item.get("severity", "")).upper() for item in validated}
    if "CRITICAL" in severities:
        return "critical", len(validated)
    if "HIGH" in severities:
        return "high", len(validated)
    if "MEDIUM" in severities:
        return "medium", len(validated)
    if "LOW" in severities:
        return "low", len(validated)
    return "unknown", None


def tfsec(text: str) -> str:
    if _artifact_skipped(text, "not run"):
        return ""
    readable = _tfsec_text_from_json(text)
    if not readable.strip():
        return ""
    body = "\n **Security Scan Results**\n\n```\n" + readable + "\n```"
    return f"### Security Scan\n<details>\n<summary>show details</summary>\n{body}\n\n</details>"
