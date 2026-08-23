"""Prepare the encrypted package and dispatch it to the execution engine."""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile

import boto3  # type: ignore[import-not-found]
from botocore.exceptions import ClientError

from dataclasses import replace

from src.core.errors import CredentialExpiredError
from src.core.logging import get_logger
from src.core.models import FolderConfig
from src.domain.accounts.binding import (
    AccountBinding,
    account_binding_from_compact,
    account_binding_from_dict,
)
from src.domain.accounts.budget import compute_ttl
from src.domain.accounts.external_id import derive_external_id
from src.domain.accounts.target_session import render_target_session_policy
from src.domain.cmd_builder.cmd_resolver import resolve_commands
from src.domain.deadlines import remaining_seconds
from src.domain.cmd_builder.installers import (
    cache_key,
    env_suffix,
    installer_key,
    require_pinned_runtime,
)
from src.domain.cmd_builder.script_generator import ScriptParams, render
from src.domain.engine.artifact_limits import MAX_PACKAGE_BYTES
from src.domain.engine.artifact_paths import artifact_env_suffix
from src.domain.engine.run_artifact_layout import resolve_run_artifact_layout
from src.domain.engine.execution_id import compose_execution_id
from src.domain.engine.payload import EnginePayload
from src.domain.engine.prepare import prepare_and_submit
from src.domain.engine.presign import effective_horizon, validate_presign_budget
from src.domain.engine.result import has_credential_expiry_signature
from src.domain.ssm_env.resolve import resolve_ssm_env_vars
from src.platform.aws import engine, s3, sops, sts
from src.platform.aws.run_registry import put_folder_submission
from src.platform.aws.ssm import get_github_token, get_parameter
from src.platform.git.clone import cleanup_clone, shallow_clone
from src.platform.git.origin import validate_clone_source
from src.platform.git.package import build_package
from src.services.run_folder.notify import (
    _MUTATION_ACTIONS,
    _notify_after_acceptance,
)
from src.services.run_folder.secrets import (
    _destroy_plan_artifact_secrets,
    _infracost_secret,
    _pinned_plan_secrets,
    _plan_artifact_secrets,
)

logger = get_logger(__name__)

_SAFE_ACTIONS = frozenset(
    {"plan", "drift", "report", "plan_destroy", "apply", "destroy"}
)
_READ_ACTIONS = frozenset({"plan", "drift", "report", "plan_destroy"})
_SHARED_INSTALLERS = (("tfsec", "1.28.10"), ("infracost", "0.10.39"))


def _assume_target_role(
    role_arn: str,
    execution_id: str,
    budget: int,
    max_ttl: int,
    external_id: str,
    policy_json: str,
    frozen_account_id: str,
) -> dict[str, str]:
    try:
        credentials = sts.assume_role(
            role_arn,
            session_name=execution_id,
            duration_seconds=compute_ttl(budget, max_ttl),
            external_id=external_id,
            policy_json=policy_json,
        )
        caller_account_id = sts.get_caller_account_id(credentials)
    except ClientError as error:
        if has_credential_expiry_signature(str(error)):
            raise CredentialExpiredError("target role credentials expired") from error
        raise
    if caller_account_id != frozen_account_id:
        raise ValueError(
            "assumed target identity account mismatch: "
            f"expected {frozen_account_id}, got {caller_account_id}"
        )
    return credentials


def _validate_folder_pin(folder_pin: dict, *, account_id: str, tf_runtime: str) -> None:
    pinned_account = folder_pin.get("account_id")
    pinned_runtime = folder_pin.get("tf_runtime")
    if not isinstance(pinned_account, str) or pinned_account != account_id:
        raise ValueError("folder_pin account_id does not match resolved folder account")
    if not isinstance(pinned_runtime, str) or pinned_runtime != tf_runtime:
        raise ValueError("folder_pin tf_runtime does not match resolved folder runtime")


def _validated_external_id(stored_external_id: object, target_account_id: str) -> str:
    hub_account_id = sts.get_caller_account_id()
    derived = derive_external_id(hub_account_id, target_account_id)
    if stored_external_id != derived:
        raise ValueError(
            "account alias external_id does not match canonical derived ExternalId; re-run just register-target"
        )
    return derived


def _folder_config(event: dict) -> FolderConfig:
    config = event.get("folder_config", {})
    if not isinstance(config, dict):
        raise TypeError("folder_config must be an object")
    folder_config = FolderConfig(**config)
    require_pinned_runtime(folder_config.tf_runtime)
    return folder_config


_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def _clone_inputs(event: dict) -> tuple[str, str, str]:
    git_url = event.get("git_url")
    commit_hash = event.get("commit_hash")
    repo_name = event.get("repo_name")
    token_path = event.get("ssm_openci_tf_github_token")
    if not isinstance(repo_name, str) or not repo_name:
        raise ValueError("repo_name is required")
    if not isinstance(git_url, str) or not git_url:
        raise ValueError("git_url is required")
    if not isinstance(commit_hash, str) or not _FULL_SHA.fullmatch(commit_hash):
        raise ValueError("commit_hash must be a full 40-character git SHA")
    validated_url = validate_clone_source(git_url, repo_name)
    token = get_github_token(str(token_path))
    return validated_url, commit_hash, token


def _update_without_collision(
    target: dict[str, str], incoming: dict[str, str], source: str
) -> None:
    overlap = sorted(set(incoming) & set(target))
    if overlap:
        raise ValueError(f"{source} collides with existing secrets: {overlap}")
    target.update(incoming)


def _artifact_names(action: str) -> tuple[str, ...]:
    if action == "plan_destroy":
        return ("init.out", "validate.out", "destroy.plan.out")
    if action == "apply":
        return ("init.out", "validate.out", "plan-show.out", "apply.out")
    if action == "destroy":
        return ("init.out", "validate.out", "plan-show.out", "destroy.out")
    names = ("init.out", "validate.out", "tf/plan.out", "drift.json")
    if action in {"plan", "report"}:
        return (*names, "tfsec.json", "infracost.json")
    return names


def _installers(action: str, config: FolderConfig) -> tuple[tuple[str, str], ...]:
    runtime = ((config.binary, config.runtime_version),)
    if action in {"plan", "report"}:
        return (*runtime, *_SHARED_INSTALLERS)
    return runtime


def _upload_bounded_package(archive: str, bucket: str, key: str) -> None:
    if os.path.isfile(archive):
        size = os.path.getsize(archive)
        if size > MAX_PACKAGE_BYTES:
            raise ValueError(f"package exceeds {MAX_PACKAGE_BYTES} bytes")
    s3.upload_file(archive, bucket, key, content_type="application/zip")


def _lane_mode() -> str:
    return os.environ.get("LANE_MODE", "read")


def _role_arn_for_lane(binding: AccountBinding, *, lane_mode: str) -> str:
    if lane_mode in _MUTATION_ACTIONS:
        if not binding.poweruser_role_name:
            raise ValueError(
                f"poweruser role not frozen for target account; {lane_mode} is physically impossible"
            )
        return (
            f"arn:aws:iam::{binding.account_id}:role/"
            f"{binding.poweruser_role_name}"
        )
    return (
        f"arn:aws:iam::{binding.account_id}:role/{binding.readonly_role_name}"
    )


def _account_binding(event: dict) -> AccountBinding:
    raw_binding = event.get("account_binding")
    frozen_account_id = event.get("account_id")
    if isinstance(raw_binding, list):
        binding = account_binding_from_compact(raw_binding, frozen_account_id)
    elif isinstance(raw_binding, dict):
        binding = account_binding_from_dict(raw_binding)
    else:
        raise ValueError("frozen account_binding is required")
    if frozen_account_id != binding.account_id:
        raise ValueError("account_binding account_id does not match frozen folder account_id")
    return binding


def _validate_lane_action(action: str, lane_mode: str) -> None:
    if lane_mode == "read":
        if action in _MUTATION_ACTIONS:
            raise ValueError(f"read lane rejects mutation action: {action}")
        if action not in _READ_ACTIONS:
            raise ValueError(f"unsafe action for read lane: {action}")
    elif lane_mode in _MUTATION_ACTIONS:
        if action != lane_mode:
            raise ValueError(f"{lane_mode} lane rejects action: {action}")
    else:
        raise ValueError(f"unknown lane mode: {lane_mode}")


def _persist_submission_acknowledgement(
    *,
    event: dict,
    account_id: str,
    result: dict[str, object],
) -> None:
    if not os.environ.get("RUN_REGISTRY_TABLE_NAME"):
        return
    submitted_at = result.get("submitted_at")
    if not isinstance(submitted_at, (int, float)) or isinstance(submitted_at, bool):
        raise TypeError("accepted submission requires numeric submitted_at")
    accepted = put_folder_submission(
        run_id=str(event["run_id"]),
        folder=str(event["folder"]),
        account_id=account_id,
        execution_id=str(result["exec_id"]),
        attempt=int(result["attempt"]),
        submitted_at=float(submitted_at),
        engine_execution_arn=(
            str(result["engine_execution_arn"])
            if isinstance(result.get("engine_execution_arn"), str)
            else None
        ),
        codebuild_build_id=(
            str(result["codebuild_build_id"])
            if isinstance(result.get("codebuild_build_id"), str)
            else None
        ),
    )
    if not isinstance(accepted, dict):
        return
    authoritative_submitted_at = accepted.get("submitted_at")
    if isinstance(authoritative_submitted_at, str):
        result["submitted_at"] = float(authoritative_submitted_at)
    stored_build_id = accepted.get("codebuild_build_id")
    if isinstance(stored_build_id, str) and stored_build_id:
        result["codebuild_build_id"] = stored_build_id


def handler(event: dict, _context: object) -> dict:
    logger.info(
        "prepare_and_submit handler invoked",
        extra={"run_id": event.get("run_id"), "folder": event.get("folder"), "action": event.get("action")},
    )
    action = event["action"]
    lane_mode = _lane_mode()
    _validate_lane_action(action, lane_mode)
    if action not in _SAFE_ACTIONS:
        raise ValueError(f"unsafe action: {action}")
    attempt, budget = int(event["attempt"]), int(event["budget"])
    deadline_at = event["deadline_at"]
    execution_budget = remaining_seconds(deadline_at, cap_seconds=budget)
    credentials = boto3.Session().get_credentials()
    validate_presign_budget(
        execution_budget, effective_horizon(credentials=credentials)
    )
    config = _folder_config(event)
    resolved = resolve_commands(action, config)
    if lane_mode in _MUTATION_ACTIONS:
        resolved = replace(resolved, execution_target="codebuild")
    execution_id = compose_execution_id(event["run_id"], event["folder"], attempt)
    package_bucket, done_bucket, tmp_bucket = (
        os.environ["PACKAGE_BUCKET_NAME"],
        os.environ["DONE_BUCKET_NAME"],
        os.environ["TMP_BUCKET_NAME"],
    )
    package_key = f"{execution_id}.zip"
    binding = _account_binding(event)
    external_id = _validated_external_id(binding.external_id, binding.account_id)
    role_arn = _role_arn_for_lane(binding, lane_mode=lane_mode)
    session_policy = render_target_session_policy(
        account_id=binding.account_id,
        repo_name=str(event["repo_name"]),
        folder=str(event["folder"]),
        action=str(action),
        project_name=os.environ["PROJECT_NAME"],
        region=os.environ["AWS_REGION"],
    )
    upstream_urls = event["upstream_urls"]
    if not isinstance(upstream_urls, dict):
        raise TypeError("upstream_urls must be an object")
    installers = _installers(action, config)
    expiry = execution_budget
    secrets = {"ARTIFACTS_DIR": "/tmp/artifacts"}
    plan_secrets, plan_metadata_uri = _plan_artifact_secrets(
        action=action,
        bucket=tmp_bucket,
        repo_name=event["repo_name"],
        run_id=event["run_id"],
        commit_hash=event["commit_hash"],
        account_id=binding.account_id,
        folder=event["folder"],
        expiry=expiry,
        config=config,
    )
    _update_without_collision(secrets, plan_secrets, "plan artifact")
    destroy_plan_secrets, destroy_plan_metadata_uri = _destroy_plan_artifact_secrets(
        action=action,
        bucket=tmp_bucket,
        repo_name=event["repo_name"],
        run_id=event["run_id"],
        commit_hash=event["commit_hash"],
        account_id=binding.account_id,
        folder=event["folder"],
        expiry=expiry,
        config=config,
    )
    _update_without_collision(secrets, destroy_plan_secrets, "destroy plan artifact")
    folder_pin = event.get("folder_pin")
    if action in {"apply", "destroy"}:
        if not isinstance(folder_pin, dict):
            raise ValueError("mutation requires folder_pin")
        _validate_folder_pin(
            folder_pin, account_id=binding.account_id, tf_runtime=config.tf_runtime
        )
    if isinstance(folder_pin, dict):
        _update_without_collision(
            secrets,
            _pinned_plan_secrets(
                action=action,
                bucket=tmp_bucket,
                repo_name=event["repo_name"],
                source_run_id=str(folder_pin.get("source_run_id") or ""),
                folder=event["folder"],
                plan_sha256=str(folder_pin.get("plan_sha256") or ""),
                plan_artifact_name=str(folder_pin.get("plan_artifact_name") or ""),
                expiry=expiry,
            ),
            "pinned plan",
        )
    folder_keys = resolve_run_artifact_layout(
        repo_name=event["repo_name"],
        run_id=event["run_id"],
        folder_path=event["folder"],
        action=action,
    ).folder_keys
    for artifact in _artifact_names(action):
        key = {
            "init.out": folder_keys.init_out,
            "validate.out": folder_keys.validate_out,
            "tf/plan.out": folder_keys.plan_out,
            "destroy.plan.out": folder_keys.destroy_plan_out,
            "apply.out": f"{folder_keys.prefix}apply.out",
            "destroy.out": f"{folder_keys.prefix}destroy.out",
            "plan-show.out": f"{folder_keys.prefix}plan-show.out",
            "drift.json": folder_keys.drift_json,
            "tfsec.json": folder_keys.tfsec_json,
            "infracost.json": folder_keys.infracost_json,
        }[artifact]
        secrets[f"ARTIFACT_PUT_URL_{artifact_env_suffix(artifact)}"] = s3.presign_put(
            tmp_bucket, key, expiry
        )
    for binary, version in installers:
        installer_lookup_key = installer_key(binary, version)
        upstream_url = upstream_urls.get(installer_lookup_key)
        if upstream_url is None:
            upstream_url = upstream_urls.get(binary)
        if not isinstance(upstream_url, str) or not upstream_url:
            raise ValueError(
                f"upstream_urls missing URL for pinned installer {installer_lookup_key}"
            )
        name = env_suffix(binary, version)
        installer_cache_key = cache_key(binary, version)
        secrets[f"CACHE_GET_URL_{name}"] = s3.presign_get(
            package_bucket, installer_cache_key, expiry
        )
        secrets[f"CACHE_PUT_URL_{name}"] = s3.presign_put(
            package_bucket, installer_cache_key, expiry
        )
        secrets[f"UPSTREAM_URL_{name}"] = upstream_url
    if config.ssm_env_paths:
        _update_without_collision(
            secrets,
            resolve_ssm_env_vars(
                config.ssm_env_paths,
                fetch=lambda path: get_parameter(path, with_decryption=True),
                existing=secrets,
            ),
            "ssm_env_paths",
        )
    _update_without_collision(
        secrets,
        _assume_target_role(
            role_arn,
            execution_id,
            execution_budget,
            binding.max_ttl,
            external_id,
            session_policy,
            binding.account_id,
        ),
        "target credentials",
    )
    _update_without_collision(
        secrets,
        _infracost_secret(action, event.get("ssm_infracost_api_key"), secrets),
        "infracost",
    )
    script = render(
        ScriptParams(
            resolved.verb,
            resolved.execution_target,
            config.binary,
            config.runtime_version,
            event["folder"],
            resolved.normalize_drift,
            resolved.extra_flags,
        )
    )
    # CodeBuild environment overrides require strings. The shared engine
    # contract canonically coerces an empty KMS sops_path back to None.
    payload = EnginePayload(
        execution_id,
        f"s3://{package_bucket}/{package_key}",
        "kms",
        "",
        base64.b64encode(json.dumps(resolved.cmds).encode()).decode(),
        f"s3://{done_bucket}/{execution_id}/done",
        resolved.execution_target,
        execution_budget,
    )
    payload.validate()

    git_url, commit_hash, token = _clone_inputs(event)
    clone_dir = shallow_clone(
        git_url, repo_name=event["repo_name"], commit_hash=commit_hash, token=token
    )
    package_root = clone_dir
    done_key = f"{execution_id}/done"
    baseline = s3.head_object(done_bucket, done_key)
    done_baseline_version_id = baseline.get("version_id") if baseline else None

    def package(encrypted: str) -> str:
        fd, destination = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        return build_package(package_root, destination, script, encrypted)

    try:
        try:
            if lane_mode in _MUTATION_ACTIONS:
                codebuild_arn = os.environ.get("ENGINE_CODEBUILD_STATE_MACHINE_ARN", "")
                if not codebuild_arn:
                    raise ValueError(
                        "ENGINE_CODEBUILD_STATE_MACHINE_ARN is required for mutation lanes"
                    )
                def submit(body: dict) -> dict:
                    return engine.start_codebuild_execution(codebuild_arn, body)
            else:
                def submit(body: dict) -> dict:
                    return engine.invoke_init_job(
                        os.environ["ENGINE_INIT_LAMBDA_NAME"], body
                    )

            def assert_submission_before_deadline() -> None:
                """Refuse engine work after package construction consumed the run."""
                remaining_seconds(deadline_at, cap_seconds=budget)

            submission = prepare_and_submit(
                payload=payload.__dict__,
                secrets=secrets,
                encrypt=lambda plain: sops.encrypt_file(
                    plain, os.environ["KMS_KEY_ARN"]
                ),
                package=package,
                upload=lambda archive: _upload_bounded_package(
                    archive, package_bucket, package_key
                ),
                submit=submit,
                pre_submit=assert_submission_before_deadline,
            )
        except (ClientError, RuntimeError) as error:
            if has_credential_expiry_signature(str(error)):
                raise CredentialExpiredError(
                    "preparation credentials expired"
                ) from error
            raise
        result: dict[str, object] = {
            "exec_id": execution_id,
            "attempt": attempt,
            "submitted_at": submission["submitted_at"],
            "done_baseline_version_id": done_baseline_version_id,
            "plan_metadata_uri": plan_metadata_uri or destroy_plan_metadata_uri,
            "submission_status": "accepted",
        }
        if lane_mode in _MUTATION_ACTIONS:
            engine_execution_arn = submission.get("engine_execution_arn")
            codebuild_build_id = submission.get("codebuild_build_id")
            if isinstance(engine_execution_arn, str):
                result["engine_execution_arn"] = engine_execution_arn
            if isinstance(codebuild_build_id, str) and codebuild_build_id:
                result["codebuild_build_id"] = codebuild_build_id
        # This durable acknowledgement is the authoritative boundary. Nothing
        # below it may classify an accepted engine execution as unsubmitted.
        _persist_submission_acknowledgement(
            event=event,
            account_id=binding.account_id,
            result=result,
        )
    finally:
        if clone_dir:
            cleanup_clone(clone_dir)
    result.update(
        _notify_after_acceptance(
            event=event,
            config=config,
            lane_mode=lane_mode,
            result=result,
        )
    )
    logger.info(
        "prepare_and_submit handler completed",
        extra={"run_id": event.get("run_id"), "folder": event.get("folder"), "action": action},
    )
    return result
