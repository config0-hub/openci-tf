from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GITHUB_TOKEN_DOC = (ROOT / "docs" / "GITHUB_TOKEN.md").read_text()
REGISTER_REPO = (ROOT / "scripts" / "register_repo.sh").read_text()
INSTALL_TOKEN = (ROOT / "scripts" / "install_github_control_token.sh").read_text()


def test_documented_fine_grained_control_token_permissions_are_exact():
    assert "Only selected repositories" in GITHUB_TOKEN_DOC
    for permission in ["Metadata | Read", "Contents | Read", "Pull requests | Read", "Issues | Read and write"]:
        assert permission in GITHUB_TOKEN_DOC
    assert "classic `repo`" in GITHUB_TOKEN_DOC
    assert "Do **not** select Administration" in GITHUB_TOKEN_DOC
    assert "repository must have initial content/default branch" in GITHUB_TOKEN_DOC


def test_control_webhook_and_private_module_credentials_use_separate_ssm_paths():
    assert "/openci-tf/install/<project>/webhook_secret" in GITHUB_TOKEN_DOC
    assert "/openci-tf/clone-token/<repo-token-name>" in GITHUB_TOKEN_DOC
    assert "/openci-tf/env/github/<owner>/<repo>" in GITHUB_TOKEN_DOC
    assert "--github-token-ssm" in REGISTER_REPO
    assert "/openci-tf/clone-token/" in INSTALL_TOKEN


def test_registration_verifier_reads_token_from_ssm_and_stdin_not_argv():
    assert "aws ssm get-parameter" in REGISTER_REPO
    assert "--with-decryption" in REGISTER_REPO
    assert "--token-stdin" in REGISTER_REPO
    assert "--value" in INSTALL_TOKEN
    assert "--token-file" in INSTALL_TOKEN


def test_registration_has_no_public_break_glass_capability_flag():
    assert "break-glass flag" in GITHUB_TOKEN_DOC


def test_registration_documents_required_and_optional_read_only_checks():
    for phrase in [
        "repository PR listing",
        "repository-wide issue comments",
        "collaborator-permission lookup",
        "exact PR metadata",
        "changed-files",
        "There is no public skip",
    ]:
        assert phrase in GITHUB_TOKEN_DOC
