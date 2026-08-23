# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

import pytest

from src.core.errors import ConfigResolutionError, ConfigValidationError
from src.domain.config.pipeline import canonical_pipeline_sha256, discover_pipelines, load_pipeline, parse_pipeline


_VALID_CONFIG = "account_alias: target\n"


def _folder(root: Path, path: str) -> None:
    config_dir = root / path / ".openci_tf"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(_VALID_CONFIG)


def _pipeline(root: Path, name: str, text: str) -> None:
    path = root / ".openci_tf" / "pipelines" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_load_pipeline_golden_happy_path(tmp_path: Path) -> None:
    _folder(tmp_path, "infra/vpc")
    _folder(tmp_path, "infra/rds")
    _folder(tmp_path, "infra/ec2")
    _pipeline(
        tmp_path,
        "data/primary",
        """
steps:
  - folder: infra/vpc
  - parallel:
      - folder: infra/rds
      - folder: infra/ec2
""",
    )

    assert discover_pipelines(tmp_path) == {
        "data/primary": (tmp_path / ".openci_tf" / "pipelines" / "data" / "primary.yaml").resolve()
    }
    pipeline = load_pipeline(tmp_path, "data/primary")

    assert pipeline.name == "data/primary"
    assert [step.folders for step in pipeline.steps] == [
        ("infra/vpc",),
        ("infra/rds", "infra/ec2"),
    ]


@pytest.mark.parametrize(
    "text,match",
    [
        ("{}", "steps must be a non-empty list"),
        ("steps: []\n", "steps must be a non-empty list"),
        ("steps:\n  - folder: infra/vpc\nextra: true\n", "unknown keys: extra"),
        ("steps:\n  - folder: infra/vpc\n    extra: true\n", "unknown keys: extra"),
        ("steps:\n  - parallel:\n      - folder: infra/rds\n", "at least 2"),
        ("steps:\n  - folder: infra/vpc\n  - folder: infra/vpc\n", "duplicate"),
        ("steps:\n  - pipeline: other\n", "nested pipeline"),
        ("steps:\n  - folder: ../escape\n", "invalid folder path"),
    ],
)
def test_parse_pipeline_rejects_invalid_documents(text: str, match: str) -> None:
    with pytest.raises(ConfigValidationError, match=match):
        parse_pipeline(text)


def test_canonical_pipeline_hash_ignores_whitespace_and_comments() -> None:
    first = parse_pipeline(
        """
# comment
steps:
  - folder: infra/vpc
  - parallel:
      - folder: infra/rds
      - folder: infra/ec2
"""
    )
    second = parse_pipeline("steps: [{folder: infra/vpc}, {parallel: [{folder: infra/rds}, {folder: infra/ec2}]}]\n")

    assert canonical_pipeline_sha256(first) == canonical_pipeline_sha256(second)


def test_canonical_pipeline_hash_changes_after_reorder() -> None:
    first = parse_pipeline("steps:\n  - folder: infra/vpc\n  - folder: infra/db\n")
    reordered = parse_pipeline("steps:\n  - folder: infra/db\n  - folder: infra/vpc\n")

    assert canonical_pipeline_sha256(first) != canonical_pipeline_sha256(reordered)


def test_parse_pipeline_rejects_more_than_twenty_folders() -> None:
    text = "steps:\n" + "".join(
        f"  - folder: infra/folder-{index}\n" for index in range(21)
    )

    with pytest.raises(ConfigValidationError, match="maximum of 20"):
        parse_pipeline(text)


def test_load_pipeline_rejects_folder_not_discovered(tmp_path: Path) -> None:
    _pipeline(tmp_path, "primary", "steps:\n  - folder: infra/missing\n")

    with pytest.raises(ConfigValidationError, match="unknown pipeline folder: infra/missing"):
        load_pipeline(tmp_path, "primary")


def test_load_pipeline_rejects_unsafe_name_and_path_escape(tmp_path: Path) -> None:
    with pytest.raises(ConfigResolutionError, match="invalid pipeline name"):
        load_pipeline(tmp_path, "../primary")
    with pytest.raises(ConfigResolutionError, match="invalid pipeline name"):
        load_pipeline(tmp_path, "/primary")
    with pytest.raises(ConfigResolutionError, match="invalid pipeline name"):
        load_pipeline(tmp_path, "primary:bad")
    with pytest.raises(ConfigResolutionError, match="pipeline name 'all' is reserved"):
        load_pipeline(tmp_path, "all")


def test_discover_pipelines_rejects_unsafe_name(tmp_path: Path) -> None:
    path = tmp_path / ".openci_tf" / "pipelines" / "bad:name.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("steps:\n  - folder: infra/vpc\n")

    with pytest.raises(ConfigResolutionError, match="invalid pipeline name"):
        discover_pipelines(tmp_path)


def test_discover_pipelines_rejects_reserved_all_name(tmp_path: Path) -> None:
    path = tmp_path / ".openci_tf" / "pipelines" / "all.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("steps:\n  - folder: infra/vpc\n")

    with pytest.raises(ConfigResolutionError, match="pipeline name 'all' is reserved"):
        discover_pipelines(tmp_path)
