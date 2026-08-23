"""Validate pinned GitHub HTTPS clone origins."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

_ALLOWED_HOSTS = frozenset({"github.com"})
_REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def canonical_github_https_url(repo_name: str) -> str:
    """Return the canonical HTTPS clone URL for a registered repository."""
    if not isinstance(repo_name, str) or not _REPO_NAME.fullmatch(repo_name):
        raise ValueError("repo_name must be org/repo")
    return f"https://github.com/{repo_name}.git"


def _normalized_repo_path(path: str) -> str:
    cleaned = path.rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    cleaned = cleaned.strip("/")
    if not cleaned:
        raise ValueError("git_url path is required")
    segments = [unquote(segment) for segment in cleaned.split("/")]
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("git_url path contains invalid segments")
    if len(segments) != 2:
        raise ValueError("git_url must reference exactly org/repo")
    return "/".join(segments)


def validate_clone_source(git_url: str, repo_name: str) -> str:
    """Reject non-GitHub or repo-mismatched clone URLs before token use."""
    if not isinstance(git_url, str) or not git_url.strip():
        raise ValueError("git_url is required")
    parsed = urlparse(git_url.strip())
    if parsed.scheme != "https":
        raise ValueError("git_url must use https")
    if parsed.params or parsed.query or parsed.fragment:
        raise ValueError("git_url must not include params, query, or fragment")
    if parsed.username or parsed.password:
        raise ValueError("git_url must not include userinfo")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise ValueError("git_url host is not allowed")
    if parsed.port not in (None, 443):
        raise ValueError("git_url port is not allowed")
    resolved_repo = _normalized_repo_path(parsed.path or "")
    if not isinstance(repo_name, str) or not _REPO_NAME.fullmatch(repo_name):
        raise ValueError("repo_name must be org/repo")
    if resolved_repo != repo_name:
        raise ValueError("git_url does not match registered repository")
    return canonical_github_https_url(repo_name)
