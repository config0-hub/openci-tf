# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""config0-addon install stages for openci-tf.

Installs openci-tf into a tenant AWS account, reusing the tenant's existing
AWS execution engine and Terraform state bucket. State locking uses the S3
native lock file (tofu >= 1.10, ``use_lockfile``); no DynamoDB lock table is
created or referenced anywhere on this path.

Stages (compose order lives in ``just install --mode config0-addon``):

  --stage ecr      Targeted ``module.ecr`` apply on infra/deploy, with the same
                   backend and tfvars as the full apply, so the ECR repository
                   exists before the GHCR -> ECR image copy.
  --stage deploy   Applies infra/foundation, waits for the image tag to exist
                   in ECR, then applies infra/deploy fully.

Every failure is fatal; there are no fallbacks and no dry-run mode.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

API_CALLER_ACTIONS = ["plan", "drift", "report"]
API_CALLER_ARTIFACT_CLASSES = ["manifest", "json", "text"]
MINIMUM_TOFU_VERSION = (1, 10)

_ROLE_ARN = re.compile(r"^arn:aws:iam::\d{12}:role/.+$")
_ACCOUNT_ID = re.compile(r"^\d{12}$")


class InstallError(RuntimeError):
    """Raised when a config0-addon install stage cannot continue."""


def run(command: list[str], *, cwd: Path = REPO_ROOT) -> None:
    """Run one subprocess loudly; a non-zero exit aborts the stage."""
    print(f"+ {' '.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=cwd, check=False)
    if completed.returncode != 0:
        raise InstallError(f"command failed (exit {completed.returncode}): {' '.join(command)}")


def require_tofu() -> None:
    """Fail loud unless tofu >= 1.10 (S3 native lock file support) is on PATH."""
    try:
        completed = subprocess.run(
            ["tofu", "version", "-json"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise InstallError("tofu is required on PATH for config0-addon installs") from error
    version = json.loads(completed.stdout).get("terraform_version", "")
    parts = version.split("-")[0].split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (IndexError, ValueError) as error:
        raise InstallError(f"could not parse tofu version {version!r}") from error
    if (major, minor) < MINIMUM_TOFU_VERSION:
        raise InstallError(
            f"tofu >= {MINIMUM_TOFU_VERSION[0]}.{MINIMUM_TOFU_VERSION[1]} is required for the "
            f"S3 native lock file; found {version}"
        )


def image_tag() -> str:
    """Return the checked-in IMAGE_VERSION tag, mirroring scripts/image_tag.sh."""
    version_file = REPO_ROOT / "IMAGE_VERSION"
    if not version_file.is_file():
        raise InstallError(f"missing checked-in image version: {version_file}")
    tag = version_file.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise InstallError("invalid Docker image tag in IMAGE_VERSION")
    return tag


def build_api_caller_policy(role_arns: list[str], trigger_id: str) -> dict[str, dict]:
    """One api_caller_policy_json entry per tenant executor role: plan|drift|report only."""
    for arn in role_arns:
        if not _ROLE_ARN.fullmatch(arn):
            raise InstallError(f"--api-caller-role-arn must be an IAM role ARN, got {arn!r}")
    if role_arns and not trigger_id:
        raise InstallError("--trigger-id is required when --api-caller-role-arn is set")
    return {
        arn: {
            "trigger_ids": [trigger_id],
            "actions": list(API_CALLER_ACTIONS),
            "artifact_classes": list(API_CALLER_ARTIFACT_CLASSES),
            "binary_plan": False,
        }
        for arn in role_arns
    }


def prepare_root(args: argparse.Namespace, root: str, state_key: str, tfvars: list[str]) -> Path:
    """Write tfvars and an S3 backend (no lock table), then init with use_lockfile."""
    root_dir = REPO_ROOT / root
    run(["./scripts/write_tfvars.sh", root, *tfvars])
    # No fifth (lock table) argument: config0-addon state never touches DynamoDB.
    run(["./scripts/generate_backend.sh", args.state_bucket, state_key, args.region, root])
    run(
        [
            "tofu",
            f"-chdir={root}",
            "init",
            "-reconfigure",
            "-input=false",
            "-backend-config=use_lockfile=true",
        ]
    )
    return root_dir


def deploy_tfvars(args: argparse.Namespace) -> list[str]:
    """The full infra/deploy variable set shared by the ecr and deploy stages."""
    policy = build_api_caller_policy(args.api_caller_role_arn, args.trigger_id or "")
    return [
        f"aws_region={args.region}",
        f"project_name={args.project_name}",
        f"image_tag={image_tag()}",
        "install_mode=config0-addon",
        f"state_bucket_name={args.state_bucket}",
        f"engine_name={args.engine_name}",
        f"target_account_ids={json.dumps(sorted(set(args.target_account_id)))}",
        f"api_caller_policy_json={json.dumps(policy)}",
    ]


def wait_for_image(args: argparse.Namespace) -> None:
    """Block until the copied image tag exists in the tenant ECR repository."""
    import boto3

    ecr = boto3.client("ecr", region_name=args.region)
    tag = image_tag()
    deadline = time.monotonic() + args.image_wait_timeout
    while True:
        try:
            ecr.describe_images(
                repositoryName=args.project_name, imageIds=[{"imageTag": tag}]
            )
            print(f"image {args.project_name}:{tag} present in ECR")
            return
        except ecr.exceptions.ImageNotFoundException:
            pass
        except ecr.exceptions.RepositoryNotFoundException as error:
            raise InstallError(
                f"ECR repository {args.project_name!r} does not exist; run --stage ecr "
                "and the GHCR image copy first"
            ) from error
        if time.monotonic() >= deadline:
            raise InstallError(
                f"image {args.project_name}:{tag} did not appear in ECR within "
                f"{args.image_wait_timeout}s; copy the GHCR image before --stage deploy"
            )
        print(f"waiting for image {args.project_name}:{tag} in ECR ...", flush=True)
        time.sleep(10)


def stage_ecr(args: argparse.Namespace) -> None:
    prepare_root(args, "infra/deploy", "deploy", deploy_tfvars(args))
    run(
        [
            "tofu",
            "-chdir=infra/deploy",
            "apply",
            "-input=false",
            "-auto-approve",
            "-target=module.ecr",
        ]
    )


def stage_deploy(args: argparse.Namespace) -> None:
    prepare_root(
        args,
        "infra/foundation",
        "foundation",
        [f"aws_region={args.region}", f"name_prefix={args.project_name}"],
    )
    run(["tofu", "-chdir=infra/foundation", "apply", "-input=false", "-auto-approve"])
    wait_for_image(args)
    prepare_root(args, "infra/deploy", "deploy", deploy_tfvars(args))
    run(["tofu", "-chdir=infra/deploy", "apply", "-input=false", "-auto-approve"])
    run(["tofu", "-chdir=infra/deploy", "output"])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="config0_addon.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--stage",
        required=True,
        choices=["ecr", "deploy"],
        help="ecr: targeted module.ecr apply on infra/deploy; deploy: foundation apply, ECR image wait, full infra/deploy apply",
    )
    parser.add_argument("--region", required=True, help="AWS region of the tenant install")
    parser.add_argument(
        "--project-name",
        default="openci-tf",
        help="Resource name prefix for this openci-tf install (default: openci-tf)",
    )
    parser.add_argument(
        "--state-bucket",
        required=True,
        help="The tenant's existing Terraform state bucket; holds the install state and the targets/ execution state",
    )
    parser.add_argument(
        "--engine-name",
        required=True,
        help="Name prefix of the tenant's existing AWS execution engine (<name>-init-job, <name>-codebuild, <name>-worker, <name>-finalizer)",
    )
    parser.add_argument(
        "--trigger-id",
        default="",
        help="Registered repository trigger id granted to --api-caller-role-arn entries",
    )
    parser.add_argument(
        "--api-caller-role-arn",
        action="append",
        default=[],
        help="Tenant executor role ARN granted exactly plan|drift|report on the runs API (repeatable)",
    )
    parser.add_argument(
        "--target-account-id",
        action="append",
        default=[],
        help="Registered 12-digit target AWS account id (repeatable; addon trust also matches executor-* roles by pattern)",
    )
    parser.add_argument(
        "--image-wait-timeout",
        type=int,
        default=900,
        help="Seconds to wait for the copied image tag in ECR before failing (default: 900)",
    )
    args = parser.parse_args(argv)
    for account_id in args.target_account_id:
        if not _ACCOUNT_ID.fullmatch(account_id):
            parser.error(f"--target-account-id must be a 12-digit AWS account id, got {account_id!r}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    require_tofu()
    if args.stage == "ecr":
        stage_ecr(args)
    else:
        stage_deploy(args)
    print(f"config0-addon stage {args.stage} complete")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except InstallError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
