"""Resolve configured Terraform folders affected by a pinned PR diff."""

from __future__ import annotations

from src.core.errors import ConfigResolutionError

MAX_PR_CHANGED_FILES = 2000
_GLOBAL_CONFIG_PATH = ".openci_tf/config.yaml"


class ChangedFilesLimitError(ConfigResolutionError):
    """Raised when a PR exceeds the bounded changed-file safety cap."""


def enforce_changed_files_limit(files: list[dict]) -> None:
    """Fail loudly when GitHub returns more changed files than the safety cap."""
    if len(files) > MAX_PR_CHANGED_FILES:
        raise ChangedFilesLimitError(
            f"pull request changed {len(files)} files; limit is {MAX_PR_CHANGED_FILES}"
        )


def changed_paths(files: list[dict]) -> list[str]:
    """Collect changed file paths, including rename and deletion sources."""
    enforce_changed_files_limit(files)
    paths: list[str] = []
    for file_obj in files:
        filepath = str(file_obj.get("filename", ""))
        if filepath in {"", "~"} or (filepath.startswith(".") and len(filepath) == 1):
            continue
        status = str(file_obj.get("status", ""))
        if status == "renamed":
            previous = str(file_obj.get("previous_filename", ""))
            if previous:
                paths.append(previous)
        if filepath:
            paths.append(filepath)
    return paths


def changed_directories(paths: list[str], *, include_root: bool = False) -> list[str]:
    """Port of ``.original/src/openci_tf/common/github_pr.py::get_changed_dirs``."""
    dirs: set[str] = set()
    for filepath in paths:
        if filepath in {"", "~"} or (filepath.startswith(".") and len(filepath) == 1):
            continue
        if "/" in filepath:
            dirs.add("/".join(filepath.split("/")[:-1]))
        elif include_root and filepath:
            dirs.add(".")
    return sorted(dirs)


def normalize_changed_directories(changed_dirs: list[str]) -> list[str]:
    """Port of ``.original/src/main_webhook.py::_get_openci_tf_folder`` dir normalization."""
    matched_dirs = list({
        changed_dir.split("/.openci_tf")[0] if "/.openci_tf" in changed_dir else changed_dir
        for changed_dir in changed_dirs
    })
    return sorted(
        path
        for path in matched_dirs
        if not any(path.startswith(other + "/") for other in matched_dirs if other != path)
    )


def _longest_configured_prefix(path: str, configured: set[str]) -> str | None:
    matches = [folder for folder in configured if path == folder or path.startswith(folder + "/")]
    if not matches:
        return None
    return max(matches, key=len)


def _folders_for_path(path: str, configured: set[str]) -> set[str]:
    if path == _GLOBAL_CONFIG_PATH:
        return set(configured)
    folder = _longest_configured_prefix(path, configured)
    return {folder} if folder else set()


def resolve_affected_folders(
    changed_files: list[dict],
    configured_folders: list[str],
) -> list[str]:
    """Return configured folders whose Terraform state may be impacted by the PR diff."""
    configured = set(configured_folders)
    if not configured:
        return []

    paths = changed_paths(changed_files)
    affected: set[str] = set()
    for path in paths:
        affected.update(_folders_for_path(path, configured))

    for directory in normalize_changed_directories(changed_directories(paths)):
        if directory in configured:
            affected.add(directory)
            continue
        if ".openci_tf" in directory:
            folder = directory.split("/.openci_tf")[0]
            if folder in configured:
                affected.add(folder)
                continue
        folder = _longest_configured_prefix(directory, configured)
        if folder:
            affected.add(folder)

    return sorted(affected)
