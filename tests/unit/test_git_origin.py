"""GitHub clone origin validation."""

import pytest

from src.platform.git.origin import canonical_github_https_url, validate_clone_source
from src.services.run_folder import prepare_and_submit

_REPO = "org/repo"
_FULL_SHA = "a" * 40
_CLONE_TOKEN = "/openci-tf/clone-token/test"


def test_canonical_github_https_url():
    assert canonical_github_https_url(_REPO) == "https://github.com/org/repo.git"


@pytest.mark.parametrize(
    "git_url",
    [
        "https://github.com/org/repo.git",
        "https://github.com/org/repo",
    ],
)
def test_validate_clone_source_accepts_canonical_repo(git_url: str):
    assert validate_clone_source(git_url, _REPO) == "https://github.com/org/repo.git"


@pytest.mark.parametrize(
    "git_url,repo_name",
    [
        ("https://attacker.example/repo.git", _REPO),
        ("file:///etc/passwd", _REPO),
        ("https://github.com/org/repo.git", "other/repo"),
        ("https://user:pass@github.com/org/repo.git", _REPO),
        ("https://github.com/org/repo.git?evil=1", _REPO),
        ("https://github.com/org/repo.git#fragment", _REPO),
        ("https://github.com/org/../evil.git", _REPO),
        ("git@github.com:org/repo.git", _REPO),
    ],
)
def test_validate_clone_source_rejects_untrusted_or_mismatched(git_url: str, repo_name: str):
    with pytest.raises(ValueError):
        validate_clone_source(git_url, repo_name)


def test_clone_inputs_rejects_untrusted_git_url_before_token_fetch(monkeypatch):
    monkeypatch.setattr(
        prepare_and_submit,
        "get_github_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("token must not be fetched")),
    )
    event = {
        "repo_name": _REPO,
        "git_url": "https://attacker.example/repo.git",
        "commit_hash": _FULL_SHA,
        "ssm_openci_tf_github_token": _CLONE_TOKEN,
    }
    with pytest.raises(ValueError, match="host is not allowed"):
        prepare_and_submit._clone_inputs(event)
