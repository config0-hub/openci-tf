# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
from src.domain.formatters.artifacts import folder_comment, summary


_ACCOUNT = "123456789012"


def test_render_mixed_outcomes_includes_folder_reports_and_summary_columns():
    good = folder_comment("infra/good", {"status": "succeeded", "account_id": _ACCOUNT}, {"tf/plan.out": "Plan: 1 to add, 0 to change, 0 to destroy"})
    failed = folder_comment("infra/bad", {"status": "infrastructure_error", "error": "engine failed", "account_id": _ACCOUNT}, {})
    table = summary([
        {"folder": "infra/good", "status": "succeeded", "account_id": _ACCOUNT},
        {"folder": "infra/bad", "status": "infrastructure_error", "account_id": _ACCOUNT},
    ])
    assert "Initialize" in good and "Infrastructure error" in failed
    assert "| Folder | Account | Drift Check | Security | Cost |" in table


def test_rejected_artifact_is_rendered_as_bounded_text():
    comment = folder_comment("infra/x", {"status": "succeeded", "account_id": _ACCOUNT}, {"tf/plan.out": "[artifact rejected: exceeds size limit]"})
    assert "artifact rejected" in comment
