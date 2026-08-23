# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only GitHub control-token capability verifier."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any

import requests

from src.platform.github.client import GITHUB_API

CONTROL_TOKEN_FINE_GRAINED_PERMISSIONS = {
    "Repository access": "Only selected repositories: each explicitly registered repository",
    "Metadata": "Read",
    "Contents": "Read",
    "Pull requests": "Read",
    "Issues": "Read and Write",
}

CONTROL_CREDENTIAL_BOUNDARIES = {
    "webhook_secret": "/openci-tf/install/<project>/webhook_secret",
    "github_control_token": "/openci-tf/clone-token/<repo-token-name>",
    "private_module_token": "/openci-tf/env/github/<owner>/<repo>",
}

OFFICIAL_PERMISSION_EVIDENCE = (
    (
        "Get a repository: https://docs.github.com/rest/repos/repos#get-a-repository "
        "(fine-grained token: Metadata read)."
    ),
    (
        "Get repository content: https://docs.github.com/rest/repos/contents#get-repository-content "
        "(fine-grained token: Contents read; repository must contain initial content/default branch)."
    ),
    (
        "List pull requests, Get a pull request, and List pull request files: "
        "https://docs.github.com/rest/pulls/pulls#list-pull-requests, "
        "https://docs.github.com/rest/pulls/pulls#get-a-pull-request, and "
        "https://docs.github.com/rest/pulls/pulls#list-pull-requests-files "
        "(fine-grained token: Pull requests read)."
    ),
    (
        "List repository issue comments and list/create/update/delete issue comments: "
        "https://docs.github.com/rest/issues/comments "
        "(fine-grained token: Issues read for listing; Issues write for mutation)."
    ),
    (
        "Get repository permissions for a user: "
        "https://docs.github.com/rest/collaborators/collaborators#get-repository-permissions-for-a-user "
        "(fine-grained token: Metadata read; Administration read is not requested)."
    ),
)

_REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_USERNAME = re.compile(r"^(?!-)[A-Za-z0-9-]{1,39}(?<!-)$")
_SAFE_METHODS = frozenset({"GET"})
_VALID_PERMISSIONS = frozenset({"admin", "maintain", "write", "triage", "read", "none"})
_TOKENISH = re.compile(r"(gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)")


class CapabilityVerificationError(RuntimeError):
    """Raised when a control token cannot prove a required read capability."""


@dataclass(frozen=True)
class CapabilityCheck:
    """One bounded verifier result."""

    name: str
    endpoint: str
    status: str
    detail: str


@dataclass(frozen=True)
class CapabilityReport:
    """Control-token verifier report safe to print."""

    repo: str
    checks: tuple[CapabilityCheck, ...]
    issue_write_note: str

    def to_lines(self) -> list[str]:
        lines = [f"GitHub control token capability check passed for {self.repo}"]
        lines.extend(
            f"- {check.name}: {check.status} ({check.detail})" for check in self.checks
        )
        lines.append(f"- Issues write: not mutated ({self.issue_write_note})")
        return lines


def validate_repo_name(repo: str) -> str:
    """Return a normalized owner/repo name or fail before any HTTP call."""
    if not isinstance(repo, str) or not _REPO_NAME.fullmatch(repo):
        raise CapabilityVerificationError("repository must be exactly owner/repo")
    if ".." in repo or repo.startswith(("-", ".")) or repo.endswith(("-", ".")):
        raise CapabilityVerificationError("repository must be exactly owner/repo")
    return repo


def validate_github_username(username: str) -> str:
    """Return a GitHub username that is safe for URL path interpolation."""
    if not isinstance(username, str) or not _GITHUB_USERNAME.fullmatch(username):
        raise CapabilityVerificationError(
            "GitHub collaborator username must be 1-39 alphanumeric/hyphen characters, "
            "and cannot start or end with a hyphen"
        )
    return username


def redact_secret(value: str, token: str = "") -> str:
    """Redact known token material from bounded diagnostic text."""
    redacted = value
    if token:
        redacted = redacted.replace(token, "<redacted>")
    return _TOKENISH.sub("<redacted>", redacted)


def verify_control_token_capabilities(
    token: str,
    repo: str,
    *,
    pr_number: int | None = None,
    collaborator: str | None = None,
    session: requests.Session | None = None,
    api_url: str = GITHUB_API,
) -> CapabilityReport:
    """Verify the current control PAT using only non-mutating GitHub endpoints."""
    cleaned_token = token.strip()
    if not cleaned_token:
        raise CapabilityVerificationError("GitHub control token is empty")
    repo_name = validate_repo_name(repo)
    if pr_number is not None and pr_number < 1:
        raise CapabilityVerificationError("pull request number must be positive")
    collaborator_name = validate_github_username(collaborator) if collaborator is not None else None

    http = session or requests.Session()
    http.headers.update({
        "Authorization": f"Bearer {cleaned_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })

    checks: list[CapabilityCheck] = []
    user = _get_json(
        http,
        api_url,
        "GET",
        "/user",
        "authenticated access",
        cleaned_token,
    )
    login = validate_github_username(_json_string(user, "login", "authenticated access"))
    checks.append(CapabilityCheck("authenticated access", "/user", "ok", f"login={login}"))

    _get_json(http, api_url, "GET", f"/repos/{repo_name}", "repository metadata", cleaned_token)
    checks.append(CapabilityCheck("repository metadata", f"/repos/{repo_name}", "ok", "Metadata read"))

    _get_json(http, api_url, "GET", f"/repos/{repo_name}/contents", "contents read", cleaned_token)
    checks.append(CapabilityCheck(
        "contents read",
        f"/repos/{repo_name}/contents",
        "ok",
        "Contents read; repository has initial content/default branch",
    ))

    _get_json(
        http,
        api_url,
        "GET",
        f"/repos/{repo_name}/pulls",
        "pull requests read",
        cleaned_token,
        params={"state": "all", "per_page": 1},
    )
    checks.append(CapabilityCheck(
        "pull requests read",
        f"/repos/{repo_name}/pulls?state=all&per_page=1",
        "ok",
        "Pull requests read",
    ))

    _get_json(
        http,
        api_url,
        "GET",
        f"/repos/{repo_name}/issues/comments",
        "repository issue comments read",
        cleaned_token,
        params={"per_page": 1},
    )
    checks.append(CapabilityCheck(
        "repository issue comments read",
        f"/repos/{repo_name}/issues/comments?per_page=1",
        "ok",
        "Issues read repository-wide; no mutation attempted",
    ))

    if pr_number is not None:
        _get_json(
            http,
            api_url,
            "GET",
            f"/repos/{repo_name}/pulls/{pr_number}",
            "pull request metadata read",
            cleaned_token,
        )
        checks.append(CapabilityCheck(
            "pull request metadata read",
            f"/repos/{repo_name}/pulls/{pr_number}",
            "ok",
            "Pull requests read for the requested PR",
        ))
        _get_json(
            http,
            api_url,
            "GET",
            f"/repos/{repo_name}/pulls/{pr_number}/files",
            "pull request changed-files read",
            cleaned_token,
            params={"per_page": 1},
        )
        checks.append(CapabilityCheck(
            "pull request changed-files read",
            f"/repos/{repo_name}/pulls/{pr_number}/files?per_page=1",
            "ok",
            "Pull requests read for changed files",
        ))
        _get_json(
            http,
            api_url,
            "GET",
            f"/repos/{repo_name}/issues/{pr_number}/comments",
            "pull request issue comments read",
            cleaned_token,
            params={"per_page": 1},
        )
        checks.append(CapabilityCheck(
            "pull request issue comments read",
            f"/repos/{repo_name}/issues/{pr_number}/comments?per_page=1",
            "ok",
            "Issues read for the requested PR issue comments",
        ))

    collaborator_login = collaborator_name or login
    permission_payload = _get_json(
        http,
        api_url,
        "GET",
        f"/repos/{repo_name}/collaborators/{collaborator_login}/permission",
        "collaborator permission lookup",
        cleaned_token,
    )
    permission = _permission_value(permission_payload, "collaborator permission lookup")
    checks.append(CapabilityCheck(
        "collaborator permission lookup",
        f"/repos/{repo_name}/collaborators/{collaborator_login}/permission",
        "ok",
        f"returned permission={permission}",
    ))

    return CapabilityReport(
        repo_name,
        tuple(checks),
        "not mutated: create/update/delete are intentionally not tested by the verifier; first real comment fails loud if Issues write is missing",
    )


def _get_json(
    session: requests.Session,
    api_url: str,
    method: str,
    path: str,
    capability: str,
    token: str,
    *,
    params: dict[str, Any] | None = None,
) -> Any:
    if method not in _SAFE_METHODS:
        raise CapabilityVerificationError(f"verifier refuses mutating method {method}")
    url = api_url.rstrip("/") + path
    try:
        response = session.request(method, url, params=params, timeout=10)
    except requests.Timeout as exc:
        raise CapabilityVerificationError(
            f"GitHub {capability} check failed: request timed out"
        ) from exc
    except requests.ConnectionError as exc:
        raise CapabilityVerificationError(
            f"GitHub {capability} check failed: connection failure"
        ) from exc
    except requests.RequestException as exc:
        raise CapabilityVerificationError(
            f"GitHub {capability} check failed: request failure"
        ) from exc

    status_code = int(response.status_code)
    if 200 <= status_code < 300:
        try:
            return response.json()
        except ValueError as exc:
            raise CapabilityVerificationError(
                f"GitHub {capability} check failed: malformed JSON response"
            ) from exc
    raise CapabilityVerificationError(_bounded_http_error(response, capability, token))


def _bounded_http_error(response: requests.Response, capability: str, token: str) -> str:
    status_code = int(response.status_code)
    if status_code == 401:
        return f"GitHub {capability} check failed: 401 unauthorized"
    if status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
        return f"GitHub {capability} check failed: rate limit exhausted"
    if status_code == 403:
        return f"GitHub {capability} check failed: 403 forbidden/missing permission"
    if status_code == 404:
        return f"GitHub {capability} check failed: 404 not found, repository not selected, or collaborator is not accessible"
    message = f"GitHub {capability} check failed: HTTP {status_code}"
    return redact_secret(message, token)


def _json_string(payload: Any, key: str, capability: str) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), str) or not payload[key]:
        raise CapabilityVerificationError(f"GitHub {capability} check returned malformed JSON")
    return payload[key]


def _permission_value(payload: Any, capability: str) -> str:
    permission = _json_string(payload, "permission", capability)
    if permission not in _VALID_PERMISSIONS:
        raise CapabilityVerificationError(f"GitHub {capability} check returned malformed JSON")
    return permission


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Verify openci-tf GitHub control token capabilities without mutation.")
    parser.add_argument("--repo", required=True, help="GitHub repository as owner/repo")
    parser.add_argument("--token-stdin", action="store_true", help="read the token from stdin")
    parser.add_argument("--github-capability-pr-number", type=int, default=None, help="optional existing PR number for exact PR metadata, changed-files, and issue-comment read checks")
    parser.add_argument("--github-capability-collaborator", default=None, help="known direct collaborator username; defaults to the token owner's login")
    args = parser.parse_args(argv)
    if not args.token_stdin:
        parser.error("token must be supplied via --token-stdin")

    token = sys.stdin.read()
    try:
        report = verify_control_token_capabilities(
            token,
            args.repo,
            pr_number=args.github_capability_pr_number,
            collaborator=args.github_capability_collaborator,
        )
    except CapabilityVerificationError as exc:
        print(redact_secret(str(exc), token.strip()), file=sys.stderr)
        return 1
    for line in report.to_lines():
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
