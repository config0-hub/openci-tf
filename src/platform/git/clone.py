# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shallow clone a repo to read .openci_tf configs."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from src.core.logging import get_logger
from src.platform.git.origin import validate_clone_source

logger = get_logger(__name__)


def _git_auth_args(token: Optional[str]) -> list[str]:
    if not token:
        return []
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {auth}"]


_GIT_TRANSPORT_GUARDS = [
    "-c",
    "protocol.file.allow=never",
    "-c",
    "protocol.ext.allow=never",
]


def shallow_clone(
    git_url: str,
    *,
    repo_name: str,
    commit_hash: Optional[str] = None,
    branch: Optional[str] = None,
    token: Optional[str] = None,
) -> str:
    """Shallow clone a repo to a temp directory. Returns the clone path.

    The clone is depth=1 — just enough to read config files.
    """
    validated_url = validate_clone_source(git_url, repo_name)
    clone_dir = tempfile.mkdtemp(prefix="openci-tf-clone-")
    auth_args = _git_auth_args(token)

    cmd = ["git", *_GIT_TRANSPORT_GUARDS, *auth_args, "clone", "--depth", "1"]
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([validated_url, clone_dir])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        logger.error("Git clone failed")
        raise RuntimeError("git clone failed")

    if commit_hash and not branch:
        fetch_cmd = ["git", *_GIT_TRANSPORT_GUARDS, *auth_args, "-C", clone_dir, "fetch", "origin", commit_hash]
        fetch_result = subprocess.run(fetch_cmd, capture_output=True, text=True, timeout=60)
        if fetch_result.returncode != 0:
            raise RuntimeError("git fetch of pinned commit failed")
        checkout_cmd = ["git", *_GIT_TRANSPORT_GUARDS, *auth_args, "-C", clone_dir, "checkout", commit_hash]
        checkout_result = subprocess.run(checkout_cmd, capture_output=True, text=True, timeout=60)
        if checkout_result.returncode != 0:
            raise RuntimeError("git checkout of pinned commit failed")

    return clone_dir


def cleanup_clone(clone_dir: str) -> None:
    """Remove the temporary clone directory."""
    if os.path.isdir(clone_dir):
        shutil.rmtree(clone_dir, ignore_errors=True)
