from pathlib import Path

import pytest

from src.core.errors import ConfigResolutionError
from src.domain.config.outer_state import resolve_outer_state
from src.domain.engine.outer_map_state import assert_outer_state_within_budget

_UPSTREAMS = {
    "tofu": "https://downloads.example/tofu",
    "tfsec": "https://downloads.example/tfsec",
    "infracost": "https://downloads.example/infracost",
}


def _folder(root: Path, path: str) -> None:
    config_dir = root / path / ".openci_tf"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("account_alias: target\n")


def _pipeline(root: Path, name: str, text: str) -> None:
    path = root / ".openci_tf" / "pipelines" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_resolve_outer_state_expands_pipeline_steps_and_flat_folders(tmp_path: Path) -> None:
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

    resolved = resolve_outer_state(
        str(tmp_path), [], _UPSTREAMS, "plan", pipeline="data/primary"
    )

    assert resolved["steps"] == [["infra/vpc"], ["infra/rds", "infra/ec2"]]
    assert resolved["folders"] == ["infra/vpc", "infra/rds", "infra/ec2"]
    assert list(resolved["folder_configs"]) == ["infra/vpc", "infra/rds", "infra/ec2"]


def test_resolve_outer_state_reverses_plan_destroy_pipeline_order(tmp_path: Path) -> None:
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

    resolved = resolve_outer_state(
        str(tmp_path), [], _UPSTREAMS, "plan_destroy", pipeline="data/primary"
    )

    assert resolved["steps"] == [["infra/rds", "infra/ec2"], ["infra/vpc"]]
    assert resolved["folders"] == ["infra/rds", "infra/ec2", "infra/vpc"]


def test_resolve_outer_state_returns_single_step_for_non_pipeline(tmp_path: Path) -> None:
    _folder(tmp_path, "infra/vpc")
    _folder(tmp_path, "infra/rds")

    resolved = resolve_outer_state(
        str(tmp_path), ["infra/vpc", "infra/rds"], _UPSTREAMS, "drift"
    )

    assert resolved["steps"] == [["infra/vpc", "infra/rds"]]
    assert resolved["folders"] == ["infra/vpc", "infra/rds"]


def test_resolve_outer_state_reports_unknown_pipeline(tmp_path: Path) -> None:
    with pytest.raises(ConfigResolutionError, match="unknown pipeline: missing"):
        resolve_outer_state(str(tmp_path), [], _UPSTREAMS, "plan", pipeline="missing")


def test_twenty_folder_pipeline_steps_fit_outer_state_budget(tmp_path: Path) -> None:
    for index in range(20):
        _folder(tmp_path, f"infra/f{index}")
    _pipeline(
        tmp_path,
        "twenty",
        "steps:\n" + "".join(f"  - folder: infra/f{index}\n" for index in range(20)),
    )

    resolved = resolve_outer_state(str(tmp_path), [], _UPSTREAMS, "plan", pipeline="twenty")
    state = {
        "action": "plan",
        "folders": resolved["folders"],
        "steps": resolved["steps"],
        "folder_configs": resolved["folder_configs"],
        "upstream_urls": resolved["upstream_urls"],
        "map_items": [],
        "skipped": [],
    }

    assert_outer_state_within_budget(state, stage="validate-and-resolve")
