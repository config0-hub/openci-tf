"""Parse repo-root .openci_tf/config.yaml into GlobalConfig."""

from __future__ import annotations

from typing import Any

import yaml

from src.core.errors import ConfigValidationError

from src.core.models import (
    CostReportConfig,
    CostReportFilter,
    GlobalConfig,
    GlobalSettings,
)


def parse_global_config(yaml_content: str) -> GlobalConfig:
    """Parse raw YAML string into a GlobalConfig."""
    data = yaml.safe_load(yaml_content)
    if not data:
        return GlobalConfig()
    return _build_global_config(data)


def _build_global_config(data: dict[str, Any]) -> GlobalConfig:
    version = data.get("version", 1)
    settings = _build_settings(data.get("settings", {}))
    pipelines = data.get("pipelines", {})
    reports = _build_reports(data.get("reports", {}))
    return GlobalConfig(
        version=version,
        settings=settings,
        pipelines=pipelines,
        reports=reports,
    )


def _build_settings(data: dict[str, Any]) -> GlobalSettings:
    if not data:
        return GlobalSettings()
    return GlobalSettings(
        destroy_wait_seconds=data.get("destroy_wait_seconds", 120),
        apply_wait_seconds=data.get("apply_wait_seconds", 60),
        default_timeout=data.get("default_timeout", 300),
        job_timeout=data.get("job_timeout", 1800),
        poll_interval=data.get("poll_interval", 30),
        tf_runtime=data.get("tf_runtime", "tofu:1.8.0"),
    )


def _build_reports(data: dict[str, Any]) -> dict[str, dict[str, CostReportConfig]]:
    if not data:
        return {}
    result: dict[str, dict[str, CostReportConfig]] = {}
    for category, reports in data.items():
        result[category] = {}
        for name, cfg in reports.items():
            filter_data = cfg.get("filter", {})
            filt = CostReportFilter(tags=filter_data.get("tags", {}))
            group_by = cfg.get("group_by", [])
            result[category][name] = CostReportConfig(filter=filt, group_by=group_by)
    return result
