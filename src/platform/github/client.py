# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub API client for PR comment operations."""

from __future__ import annotations

import hashlib

import requests

GITHUB_API = "https://api.github.com"


class GitHubChangedFilesLimitExceeded(ValueError):
    """Raised when changed-file pagination exceeds a caller-provided cap."""


class GitHubClient:
    """Minimal GitHub REST API client for PR comments."""

    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        })

    def create_comment(self, repo: str, pr_number: int, body: str) -> int:
        """Create a PR comment. Returns comment_id."""
        url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
        resp = self.session.post(url, json={"body": body})
        resp.raise_for_status()
        return resp.json()["id"]

    def update_comment(self, repo: str, comment_id: int, body: str) -> None:
        """Update an existing PR comment."""
        url = f"{GITHUB_API}/repos/{repo}/issues/comments/{comment_id}"
        resp = self.session.patch(url, json={"body": body})
        resp.raise_for_status()

    def delete_comment(self, repo: str, comment_id: int) -> None:
        """Delete a PR comment."""
        url = f"{GITHUB_API}/repos/{repo}/issues/comments/{comment_id}"
        resp = self.session.delete(url)
        resp.raise_for_status()

    def find_comment_by_tag(
        self, repo: str, pr_number: int, tag: str
    ) -> int | None:
        """Search PR comments for one containing the search tag. Returns comment_id or None."""
        url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
        page = 1
        while True:
            resp = self.session.get(url, params={"page": page, "per_page": 100})
            resp.raise_for_status()
            comments = resp.json()
            if not comments:
                break
            for comment in comments:
                if tag in comment.get("body", ""):
                    return comment["id"]
            page += 1
        return None

    def find_comments_by_tag(
        self, repo: str, pr_number: int, tag: str
    ) -> list[int]:
        """Find all PR comments containing the search tag."""
        url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
        matches = []
        page = 1
        while True:
            resp = self.session.get(url, params={"page": page, "per_page": 100})
            resp.raise_for_status()
            comments = resp.json()
            if not comments:
                break
            for comment in comments:
                if tag in comment.get("body", ""):
                    matches.append(comment["id"])
            page += 1
        return matches

    def upsert_comment(
        self, repo: str, pr_number: int, body: str, tag: str
    ) -> int:
        """Find existing comments by tag, collapse duplicates, update or create."""
        matches = self.find_comments_by_tag(repo, pr_number, tag)
        for comment_id in matches[1:]:
            self.delete_comment(repo, comment_id)
        if matches:
            self.update_comment(repo, matches[0], body)
            return matches[0]
        return self.create_comment(repo, pr_number, body)

    def delete_and_repost(
        self, repo: str, pr_number: int, body: str, tag: str
    ) -> int:
        """Delete all existing comments matching tag, then post one new comment at bottom."""
        for comment_id in self.find_comments_by_tag(repo, pr_number, tag):
            self.delete_comment(repo, comment_id)
        return self.create_comment(repo, pr_number, body)

    def cleanup_comments(
        self, repo: str, pr_number: int, tags: list[str]
    ) -> int:
        """Delete all comments matching any of the given tags. Returns count deleted."""
        deleted = 0
        for tag in tags:
            comment_ids = self.find_comments_by_tag(repo, pr_number, tag)
            for cid in comment_ids:
                self.delete_comment(repo, cid)
                deleted += 1
        return deleted

    def get_pr_changed_files(
        self,
        repo: str,
        pr_number: int,
        *,
        max_files: int | None = None,
    ) -> list[dict]:
        """Fetch changed files for a PR, failing boundedly when ``max_files`` is exceeded.

        Returns list of file dicts with 'filename', 'status', etc. HTTP failures
        propagate via ``raise_for_status`` before any limit handling.
        """
        url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files"
        files: list[dict] = []
        page = 1
        per_page = 100
        while True:
            resp = self.session.get(url, params={"page": page, "per_page": per_page})
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            if max_files is not None and len(files) + len(batch) > max_files:
                raise GitHubChangedFilesLimitExceeded(
                    f"pull request changed more than {max_files} files"
                )
            files.extend(batch)
            page += 1
        return files

    def get_pr_head_sha(self, repo: str, pr_number: int) -> str:
        """Get the HEAD commit SHA for a PR."""
        url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
        resp = self.session.get(url)
        resp.raise_for_status()
        return resp.json()["head"]["sha"]

    def pr_has_approved_review(self, repo: str, pr_number: int) -> bool:
        """Return True when at least one reviewer has APPROVED as their latest review."""
        url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews"
        latest_by_user: dict[int, tuple[str, str]] = {}
        page = 1
        while True:
            resp = self.session.get(url, params={"per_page": 100, "page": page})
            resp.raise_for_status()
            reviews = resp.json()
            if not reviews:
                break
            for review in reviews:
                user = review.get("user") or {}
                user_id = user.get("id")
                if user_id is None:
                    continue
                submitted_at = str(review.get("submitted_at") or "")
                state = str(review.get("state") or "")
                previous = latest_by_user.get(user_id)
                if previous is None or submitted_at >= previous[0]:
                    latest_by_user[user_id] = (submitted_at, state)
            if len(reviews) < 100:
                break
            page += 1
        return any(state == "APPROVED" for _, state in latest_by_user.values())


def get_pr_head_sha(pr_api_url: str, token: str) -> str:
    """Fetch the head SHA for a pull request via GitHub API.

    Args:
        pr_api_url: Full GitHub API URL (e.g. https://api.github.com/repos/org/repo/pulls/123)
        token: GitHub token for authentication
    """
    resp = requests.get(
        pr_api_url,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["head"]["sha"]

def get_pull_request(pr_api_url: str, token: str) -> dict:
    """Fetch one PR document for immutable SHA and fork checks."""
    response = requests.get(pr_api_url, headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}, timeout=10)
    response.raise_for_status()
    return response.json()

def get_collaborator_permission(repo: str, username: str, token: str) -> str:
    """Return GitHub's raw permission level; callers own the policy."""
    response = requests.get(f"{GITHUB_API}/repos/{repo}/collaborators/{username}/permission", headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}, timeout=10)
    if response.status_code == 404: return "none"
    response.raise_for_status()
    return str(response.json().get("permission", "none"))


def comment_url(repo: str, pr_number: int, comment_id: int) -> str:
    """Return a stable browser URL for a PR issue comment."""
    return f"https://github.com/{repo}/pull/{pr_number}#issuecomment-{comment_id}"


def generate_search_tag(repo_name: str, pr_number: int, suffix: str = "") -> str:
    """Generate a deterministic legacy search tag for PR comment migration."""
    raw = f"{repo_name}{pr_number}{suffix}"
    md5 = hashlib.md5(raw.encode()).hexdigest()
    return f"openci-tf:::tag::{md5}"
