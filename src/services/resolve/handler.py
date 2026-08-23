# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lambda handler: openci-tf-parse-command

First Step Function state. Parses the command from the webhook event and
returns routing data for the Choice state.
"""

from __future__ import annotations

from typing import Any

from src.core.logging import get_logger
from src.domain.command.grammar import ParseError, parse_command

logger = get_logger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entrypoint for openci-tf-parse-command.

    Input: Step Function state with webhook_info and settings.
    Output: Adds the parsed action and folder selection to the state.
    """
    if isinstance(event.get("action"), str) and event["action"] in {
        "plan", "drift", "report", "plan_destroy", "apply", "destroy",
    }:
        return event

    webhook_info = event["webhook_info"]
    comment_body = webhook_info.get("comment_body", "")

    if not comment_body:
        # pull_request events (auto-plan) don't have comment_body
        if webhook_info["event_type"] == "pull_request":
            return {
                **event,
                "action": "plan",
                "folders": [],
                "all_flag": False,
                "affected_flag": True,
                "auto_plan": True,
            }
        return {**event, "action": "noop"}

    try:
        cmd = parse_command(comment_body)
    except ParseError as e:
        logger.warning("Failed to parse command", extra={
            "repo": webhook_info.get("repo_name"),
            "error": str(e),
        })
        return {**event, "action": "noop", "parse_error": str(e)}

    # Issue-driven commands: restrict to read-only
    if (
        webhook_info.get("issue_number")
        and not webhook_info.get("pr_number")
        and cmd.action in ("apply", "destroy")
    ):
        logger.warning("Mutating action from issue context blocked", extra={
            "action": cmd.action,
            "repo": webhook_info.get("repo_name"),
        })
        return {
            **event,
            "action": "noop",
            "parse_error": f"{cmd.action} is not allowed from issues",
        }

    result = {
        **event,
        "action": cmd.effective_action,
        "folders": cmd.folders,
        "all_flag": cmd.all_flag,
        "affected_flag": cmd.affected_flag,
    }
    if cmd.pipeline is not None:
        result["pipeline"] = cmd.pipeline
    if cmd.pipeline_step is not None:
        result["pipeline_step"] = cmd.pipeline_step
    if cmd.confirm_token:
        result["confirm_token"] = cmd.confirm_token
        result["intent_confirm"] = True
    elif cmd.action in {"apply", "destroy"}:
        result["intent_create"] = True

    # Preserve the prepared execution contract; parsing only owns command grammar.
    for field in ("folder_configs", "upstream_urls"):
        if field in event:
            result[field] = event[field]

    return result
