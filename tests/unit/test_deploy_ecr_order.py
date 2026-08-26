# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _REPO_ROOT / "justfile"


def test_deploy_recipe_bootstraps_ecr_before_image_push_and_full_apply():
    deploy = _JUSTFILE.read_text(encoding="utf-8").split("deploy:", 1)[1].split(
        "deploy-destroy:", 1
    )[0]
    ecr_plan = deploy.find("plan -input=false -target=module.ecr -detailed-exitcode")
    ecr_apply = deploy.find("apply -input=false -auto-approve -target=module.ecr")
    docker_push = deploy.find("just docker-push")
    full_apply = deploy.rfind(
        "terraform -chdir=infra/deploy apply -input=false -auto-approve"
    )
    assert ecr_plan != -1
    assert ecr_apply != -1
    assert docker_push != -1
    assert full_apply != -1
    assert ecr_plan < ecr_apply < docker_push < full_apply
