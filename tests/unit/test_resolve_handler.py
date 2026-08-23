"""Tests for the wired parse-command Lambda entrypoint."""

from src.services.resolve.handler import handler


def test_tf_plan_is_parsed_into_step_function_routing_data() -> None:
    event = {
        "webhook_info": {
            "comment_body": "tf plan infra/vpc", "event_type": "issue_comment",
            "repo_name": "org/repo",
        }
    }
    result = handler(event, None)
    assert result["action"] == "plan"
    assert result["folders"] == ["infra/vpc"]
    assert result["all_flag"] is False
