"""Tests for changed-folder resolution ported from .original."""

from typing import Any, cast

import pytest

from src.core.errors import ConfigResolutionError
from src.domain.command.affected_folders import (
    MAX_PR_CHANGED_FILES,
    changed_directories,
    enforce_changed_files_limit,
    normalize_changed_directories,
    resolve_affected_folders,
)
from src.domain.command.grammar import ParseError, parse_command
from src.platform.github.client import GitHubChangedFilesLimitExceeded, GitHubClient


def test_bare_tf_plan_uses_affected_selection():
    command = parse_command("tf plan")
    assert command.action == "plan"
    assert command.affected_flag is True
    assert command.all_flag is False
    assert command.folders == []


def test_tf_plan_all_is_accepted():
    command = parse_command("tf plan all")
    assert command.action == "plan"
    assert command.all_flag is True


def test_validate_is_not_a_public_verb():
    with pytest.raises(ParseError, match="validate is not a supported command"):
        parse_command("tf validate infra/vpc")


@pytest.mark.parametrize("verb", ["drift", "report"])
def test_drift_and_report_still_accept_all(verb: str):
    command = parse_command(f"tf {verb} all")
    assert command.all_flag is True


def test_changed_directories_skip_special_paths():
    assert changed_directories(["~", ".", "infra/vpc/main.tf"]) == ["infra/vpc"]


def test_normalize_changed_directories_strips_openci_tf_and_parents():
    changed = ["infra/vpc/.openci_tf", "infra/vpc/modules/net"]
    assert normalize_changed_directories(changed) == ["infra/vpc"]


def test_resolve_affected_folders_matches_paths_renames_and_global_config():
    configured = ["infra/vpc", "infra/rds"]
    changed_files = [
        {"filename": "infra/vpc/main.tf", "status": "modified"},
        {"filename": "infra/rds/main.tf", "status": "renamed", "previous_filename": "infra/rds/old.tf"},
        {"filename": ".openci_tf/config.yaml", "status": "modified"},
    ]
    assert resolve_affected_folders(changed_files, configured) == ["infra/rds", "infra/vpc"]


def test_resolve_affected_folders_returns_empty_when_nothing_matches():
    configured = ["infra/vpc"]
    changed_files = [{"filename": "README.md", "status": "modified"}]
    assert resolve_affected_folders(changed_files, configured) == []


def test_changed_files_limit_fails_loud():
    with pytest.raises(ConfigResolutionError, match=str(MAX_PR_CHANGED_FILES)):
        enforce_changed_files_limit([{"filename": f"f{i}.tf"} for i in range(MAX_PR_CHANGED_FILES + 1)])


class _Response:
    def __init__(self, payload: list[dict[str, str]], error: Exception | None = None):
        self.payload = payload
        self.error = error

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error

    def json(self) -> list[dict[str, str]]:
        return self.payload


class _Session:
    def __init__(self, pages: list[_Response]):
        self.pages = pages
        self.seen_pages: list[int] = []
        self.headers: dict[str, str] = {}

    def get(self, _url: str, params: dict[str, int] | None = None) -> _Response:
        page = (params or {}).get("page", 1)
        self.seen_pages.append(page)
        return self.pages[page - 1]


def _client_with_session(session: _Session) -> GitHubClient:
    client = GitHubClient("token")
    client.session = cast(Any, session)
    return client


def test_changed_files_pagination_fails_boundedly_at_cap():
    session = _Session([
        _Response([{"filename": "a.tf"}, {"filename": "b.tf"}]),
        _Response([{"filename": "c.tf"}]),
    ])
    client = _client_with_session(session)

    with pytest.raises(GitHubChangedFilesLimitExceeded, match="more than 2"):
        client.get_pr_changed_files("org/repo", 7, max_files=2)
    assert session.seen_pages == [1, 2]


def test_changed_files_pagination_preserves_http_failures():
    error = RuntimeError("github unavailable")
    client = _client_with_session(_Session([_Response([], error=error)]))

    with pytest.raises(RuntimeError, match="github unavailable"):
        client.get_pr_changed_files("org/repo", 7, max_files=2)
