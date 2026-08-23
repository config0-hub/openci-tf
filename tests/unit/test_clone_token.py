"""Clone-token namespace validation."""

import pytest

from src.platform.aws.clone_token import CLONE_TOKEN_PREFIX, validate_clone_token_path
from src.platform.aws.ssm import get_github_token


def test_validate_clone_token_path_accepts_namespace():
    path = f"{CLONE_TOKEN_PREFIX}openci-tf-smoke-20260805115630"
    assert validate_clone_token_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "/openci-tf/install/openci-tf-smoke/github_token",
        "/openci-tf/install/openci-tf-smoke/webhook_secret",
        "/token",
        "../openci-tf/clone-token/evil",
        "/openci-tf/clone-token/../install/secret",
        "/openci-tf/clone-token/",
        "/openci-tf/clone-token/evil\"},\"pk\":{\"S\":\"tamper\"}",
    ],
)
def test_validate_clone_token_path_rejects_outside_namespace(path: str):
    with pytest.raises(ValueError, match="clone token|clone-token"):
        validate_clone_token_path(path)


def test_get_github_token_strips_trailing_whitespace(monkeypatch):
    monkeypatch.setattr(
        "src.platform.aws.ssm.get_parameter",
        lambda path, **_kwargs: f"  token-value\n",
    )
    assert get_github_token("/openci-tf/clone-token/test") == "token-value"


def test_get_github_token_rejects_install_secret_path(monkeypatch):
    monkeypatch.setattr(
        "src.platform.aws.ssm.get_parameter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("SSM must not be called")),
    )
    with pytest.raises(ValueError, match="clone-token|clone token"):
        get_github_token("/openci-tf/install/openci-tf-smoke/github_token")
