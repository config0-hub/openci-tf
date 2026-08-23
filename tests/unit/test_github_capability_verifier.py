# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import pytest
import requests

from src.platform.github.capability_verifier import (
    CONTROL_CREDENTIAL_BOUNDARIES,
    CONTROL_TOKEN_FINE_GRAINED_PERMISSIONS,
    OFFICIAL_PERMISSION_EVIDENCE,
    CapabilityVerificationError,
    redact_secret,
    validate_github_username,
    validate_repo_name,
    verify_control_token_capabilities,
)


class FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None, *, json_error: bool = False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("malformed-json body with github_pat_secretvalue")
        return self._payload


class FakeSession:
    def __init__(self, statuses=None):
        self.headers = {}
        self.statuses = statuses or {}
        self.requests = []

    def request(self, method, url, params=None, timeout=None):
        path = url.removeprefix("https://api.github.test")
        self.requests.append({"method": method, "path": path, "params": params, "timeout": timeout})
        response = self.statuses.get(path)
        if isinstance(response, requests.RequestException):
            raise response
        if response is not None:
            return response
        if path == "/user":
            return FakeResponse(200, {"login": "bot-user"})
        if path.endswith("/permission"):
            return FakeResponse(200, {"permission": "write"})
        if path.endswith("/contents"):
            return FakeResponse(200, {"type": "dir"})
        return FakeResponse(200, [])


def _paths(session: FakeSession) -> list[str]:
    return [request["path"] for request in session.requests]


def test_capability_verifier_required_checks_do_not_require_existing_pr():
    session = FakeSession()

    report = verify_control_token_capabilities(
        "github_pat_testtoken",
        "org/repo",
        session=session,
        api_url="https://api.github.test",
    )

    assert all(request["method"] == "GET" for request in session.requests)
    assert _paths(session) == [
        "/user",
        "/repos/org/repo",
        "/repos/org/repo/contents",
        "/repos/org/repo/pulls",
        "/repos/org/repo/issues/comments",
        "/repos/org/repo/collaborators/bot-user/permission",
    ]
    assert session.requests[3]["params"] == {"state": "all", "per_page": 1}
    assert session.requests[4]["params"] == {"per_page": 1}
    lines = "\n".join(report.to_lines())
    assert "skipped" not in lines
    assert "not run" not in lines
    assert "repository issue comments read" in lines
    assert "Issues write" in report.to_lines()[-1]


def test_capability_verifier_optional_pr_number_checks_exact_pr_metadata_files_and_comments():
    session = FakeSession()

    verify_control_token_capabilities(
        "github_pat_testtoken",
        "org/repo",
        pr_number=7,
        session=session,
        api_url="https://api.github.test",
    )

    assert "/repos/org/repo/pulls/7" in _paths(session)
    assert "/repos/org/repo/pulls/7/files" in _paths(session)
    assert "/repos/org/repo/issues/7/comments" in _paths(session)
    assert session.requests[_paths(session).index("/repos/org/repo/pulls/7/files")]["params"] == {"per_page": 1}
    assert session.requests[_paths(session).index("/repos/org/repo/issues/7/comments")]["params"] == {"per_page": 1}


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/repos/org/repo/contents", "contents read"),
        ("/repos/org/repo/pulls", "pull requests read"),
        ("/repos/org/repo/issues/comments", "repository issue comments read"),
        ("/repos/org/repo/collaborators/bot-user/permission", "collaborator permission"),
    ],
)
def test_capability_verifier_fails_loud_on_missing_permission(path: str, message: str):
    session = FakeSession({path: FakeResponse(403)})

    with pytest.raises(CapabilityVerificationError, match=message):
        verify_control_token_capabilities(
            "github_pat_testtoken",
            "org/repo",
            session=session,
            api_url="https://api.github.test",
        )


def test_capability_verifier_collaborator_404_is_failure_not_success():
    session = FakeSession({"/repos/org/repo/collaborators/bot-user/permission": FakeResponse(404)})

    with pytest.raises(CapabilityVerificationError, match="404 not found"):
        verify_control_token_capabilities(
            "github_pat_testtoken",
            "org/repo",
            session=session,
            api_url="https://api.github.test",
        )


def test_capability_verifier_validates_permission_json_field():
    session = FakeSession({"/repos/org/repo/collaborators/bot-user/permission": FakeResponse(200, {"permission": "owner"})})

    with pytest.raises(CapabilityVerificationError, match="malformed JSON"):
        verify_control_token_capabilities(
            "github_pat_testtoken",
            "org/repo",
            session=session,
            api_url="https://api.github.test",
        )


@pytest.mark.parametrize(
    ("status", "headers", "message"),
    [
        (401, {}, "401 unauthorized"),
        (403, {}, "403 forbidden/missing permission"),
        (403, {"x-ratelimit-remaining": "0"}, "rate limit exhausted"),
    ],
)
def test_capability_verifier_reports_auth_and_rate_limit(status: int, headers: dict[str, str], message: str):
    session = FakeSession({"/user": FakeResponse(status, headers=headers)})

    with pytest.raises(CapabilityVerificationError, match=message):
        verify_control_token_capabilities(
            "github_pat_testtoken",
            "org/repo",
            session=session,
            api_url="https://api.github.test",
        )


@pytest.mark.parametrize(
    ("exception", "message"),
    [
        (requests.Timeout("timeout for https://api.github.test?token=github_pat_secretvalue"), "request timed out"),
        (requests.ConnectionError("connection failed for github_pat_secretvalue"), "connection failure"),
        (requests.RequestException("boom github_pat_secretvalue"), "request failure"),
    ],
)
def test_capability_verifier_bounds_and_redacts_request_exceptions(exception: requests.RequestException, message: str):
    token = "github_pat_secretvalue"
    session = FakeSession({"/user": exception})

    with pytest.raises(CapabilityVerificationError) as exc_info:
        verify_control_token_capabilities(
            token,
            "org/repo",
            session=session,
            api_url="https://api.github.test",
        )

    assert message in str(exc_info.value)
    assert token not in str(exc_info.value)
    assert "https://" not in str(exc_info.value)


def test_capability_verifier_bounds_and_redacts_malformed_json():
    token = "github_pat_secretvalue"
    session = FakeSession({"/user": FakeResponse(200, json_error=True)})

    with pytest.raises(CapabilityVerificationError) as exc_info:
        verify_control_token_capabilities(
            token,
            "org/repo",
            session=session,
            api_url="https://api.github.test",
        )

    message = str(exc_info.value)
    assert "malformed JSON response" in message
    assert token not in message
    assert "body" not in message


@pytest.mark.parametrize("repo", ["org", "org/repo/extra", "../repo", "org/../repo", ""])
def test_capability_verifier_rejects_malformed_repo_before_http(repo: str):
    session = FakeSession()

    with pytest.raises(CapabilityVerificationError, match="owner/repo"):
        verify_control_token_capabilities(
            "github_pat_testtoken",
            repo,
            session=session,
            api_url="https://api.github.test",
        )

    assert session.requests == []


@pytest.mark.parametrize("username", ["bad/name", "bad.name", "-bad", "bad-", "", "x" * 40])
def test_capability_verifier_rejects_invalid_collaborator_before_http(username: str):
    session = FakeSession()

    with pytest.raises(CapabilityVerificationError, match="collaborator username"):
        verify_control_token_capabilities(
            "github_pat_testtoken",
            "org/repo",
            collaborator=username,
            session=session,
            api_url="https://api.github.test",
        )

    assert session.requests == []


def test_capability_verifier_uses_explicit_valid_collaborator_username():
    session = FakeSession()

    verify_control_token_capabilities(
        "github_pat_testtoken",
        "org/repo",
        collaborator="known-user",
        session=session,
        api_url="https://api.github.test",
    )

    assert "/repos/org/repo/collaborators/known-user/permission" in _paths(session)


def test_capability_verifier_redacts_token_like_values():
    token = "github_pat_secretvalue"

    redacted = redact_secret(f"bad token {token} ghp_abcdef012345", token)

    assert token not in redacted
    assert "ghp_abcdef012345" not in redacted
    assert redacted.count("<redacted>") == 2


def test_control_token_contract_documents_exact_fine_grained_permissions_without_administration():
    assert CONTROL_TOKEN_FINE_GRAINED_PERMISSIONS == {
        "Repository access": "Only selected repositories: each explicitly registered repository",
        "Metadata": "Read",
        "Contents": "Read",
        "Pull requests": "Read",
        "Issues": "Read and Write",
    }
    assert "Administration" not in CONTROL_TOKEN_FINE_GRAINED_PERMISSIONS
    assert any("Get repository permissions for a user" in item for item in OFFICIAL_PERMISSION_EVIDENCE)
    assert any("Administration read is not requested" in item for item in OFFICIAL_PERMISSION_EVIDENCE)
    assert any("repository must contain initial content/default branch" in item for item in OFFICIAL_PERMISSION_EVIDENCE)


def test_control_credentials_remain_separate_ssm_namespaces():
    assert CONTROL_CREDENTIAL_BOUNDARIES["webhook_secret"].startswith("/openci-tf/install/")
    assert CONTROL_CREDENTIAL_BOUNDARIES["github_control_token"].startswith("/openci-tf/clone-token/")
    assert CONTROL_CREDENTIAL_BOUNDARIES["private_module_token"].startswith("/openci-tf/env/github/")
    assert len(set(CONTROL_CREDENTIAL_BOUNDARIES.values())) == 3


def test_validate_repo_name_accepts_owner_repo():
    assert validate_repo_name("Org-1/repo.name") == "Org-1/repo.name"


def test_validate_github_username_accepts_conservative_usernames():
    assert validate_github_username("known-user1") == "known-user1"
