# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _REPO_ROOT / "justfile"
_ECR_BOOTSTRAP_TARGETS = (
    "module.ecr",
    "module.hub_setup.aws_iam_role.executor_local",
    "module.hub_setup.aws_iam_role_policy.executor_local",
)


def _deploy_recipe() -> str:
    return _JUSTFILE.read_text(encoding="utf-8").split("deploy:", 1)[1].split(
        "deploy-destroy:", 1
    )[0]


def _line_containing(text: str, fragment: str) -> str:
    for line in text.splitlines():
        if fragment in line:
            return line
    raise AssertionError(f"missing line containing {fragment!r}")


def test_deploy_recipe_bootstraps_ecr_before_image_push_and_full_apply():
    deploy = _deploy_recipe()
    ecr_plan = deploy.find("terraform -chdir=infra/deploy plan")
    ecr_apply = deploy.find(
        "terraform -chdir=infra/deploy apply -input=false -auto-approve -target=module.ecr"
    )
    docker_push = deploy.find("just docker-push")
    full_apply = deploy.rfind(
        "terraform -chdir=infra/deploy apply -input=false -auto-approve"
    )
    assert ecr_plan != -1
    assert ecr_apply != -1
    assert docker_push != -1
    assert full_apply != -1
    assert ecr_plan < ecr_apply < docker_push < full_apply


def test_deploy_ecr_bootstrap_targets_include_moved_executor_resources():
    deploy = _deploy_recipe()
    ecr_plan = _line_containing(deploy, "terraform -chdir=infra/deploy plan")
    ecr_apply = _line_containing(
        deploy,
        "terraform -chdir=infra/deploy apply -input=false -auto-approve -target=module.ecr",
    )

    for target in _ECR_BOOTSTRAP_TARGETS:
        assert f"-target={target}" in ecr_plan
        assert f"-target={target}" in ecr_apply
