# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GITHUB_TOKEN_DOC = (ROOT / "docs" / "GITHUB_TOKEN.md").read_text()
INSTALL_DOC = (ROOT / "docs" / "INSTALL.md").read_text()
REGISTER_REPO = (ROOT / "scripts" / "register_repo.sh").read_text()
CONFIG0_REGISTER_REPO = (ROOT / "install" / "register_repo.py").read_text()
INSTALL_TOKEN = (ROOT / "scripts" / "install_github_control_token.sh").read_text()


def test_documented_fine_grained_control_token_permissions_are_exact():
    assert "Only selected repositories" in GITHUB_TOKEN_DOC
    for permission in [
        "Metadata | Read",
        "Contents | Read and write",
        "Pull requests | Read and write",
        "Issues | Read and write",
    ]:
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
        "standalone verifier uses only `GET` endpoints",
        "repository PR listing",
        "repository-wide issue comments",
        "collaborator-permission lookup",
        "exact PR metadata",
        "changed-files",
        "There is no public skip",
    ]:
        assert phrase in GITHUB_TOKEN_DOC


def test_addon_permission_contract_matches_the_real_mutation_probe():
    for operation in [
        'github.request("PUT", f"/repos/{repo}/contents/{file_path}"',
        '"POST",\n            f"/repos/{repo}/pulls"',
        '"POST",\n            f"/repos/{repo}/issues/{number}/comments"',
        '"PATCH", f"/repos/{repo}/pulls/{number}"',
        '("delete probe branch", "DELETE", ref_path',
    ]:
        assert operation in CONFIG0_REGISTER_REPO
    for phrase in [
        "mutation probe verifies",
        "Contents, Pull requests, and Issues write",
        "legacy registration",
    ]:
        assert phrase in GITHUB_TOKEN_DOC


def test_addon_install_documents_control_token_at_the_consumed_default_path():
    assert "just install-github-control-token --repo <owner/repo>" in INSTALL_DOC
    assert "/openci-tf/clone-token/<owner>-<repo>-control" in INSTALL_DOC
    assert "install/register_repo.py" in INSTALL_DOC
