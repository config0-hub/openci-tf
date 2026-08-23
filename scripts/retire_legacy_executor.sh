#!/usr/bin/env bash
# Retire or restore legacy executor-local (hub) or executor-remote (target) durably.
# Persists install SSM first, then applies Terraform so normal deploy/target-create cannot recreate.
set -euo pipefail

PROJECT="${OPENCI_TF_PROJECT:-openci-tf}"
LANE=""
RESTORE=false
HUB_ACCOUNT_ID=""
STATE_BUCKET=""
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

usage() {
	echo 'Usage: retire_legacy_executor.sh --lane local|remote [--restore] [--hub-account-id 12_DIGITS] [--state-bucket NAME]' >&2
	exit 1
}

validate_project_name() {
	[[ "$PROJECT" =~ ^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$ ]] || {
		echo "ERROR: OPENCI_TF_PROJECT must be lowercase alphanumeric with hyphens (3-64 chars): ${PROJECT}" >&2
		exit 1
	}
}

validate_region() {
	[[ "$AWS_REGION" =~ ^[a-z]{2}-[a-z]+-[0-9]+$ ]] || {
		echo "ERROR: AWS region must match ^[a-z]{2}-[a-z]+-[0-9]+$: ${AWS_REGION}" >&2
		exit 1
	}
}

while [[ $# -gt 0 ]]; do
	case "$1" in
	--help | -h) usage ;;
	--lane)
		LANE="$2"
		shift 2
		;;
	--restore)
		RESTORE=true
		shift
		;;
	--hub-account-id)
		HUB_ACCOUNT_ID="$2"
		shift 2
		;;
	--state-bucket)
		STATE_BUCKET="$2"
		shift 2
		;;
	*)
		echo "Unknown arg: $1" >&2
		exit 1
		;;
	esac
done

case "$LANE" in
local | remote) ;;
*)
	echo "ERROR: --lane must be local or remote" >&2
	exit 1
	;;
esac

validate_project_name
validate_region

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || {
	echo "ERROR: could not resolve a valid AWS account id from caller identity" >&2
	exit 1
}

PROVISION_VALUE=false
if [ "$RESTORE" = true ]; then
	PROVISION_VALUE=true
fi

apply_failed() {
	echo "ERROR: Terraform apply failed; legacy retirement was NOT completed." >&2
	echo "ERROR: Install SSM already records provision_legacy_executor_${LANE}=${PROVISION_VALUE}; fix the apply error and rerun." >&2
	exit 1
}

case "$LANE" in
local)
	SSM_KEY="provision_legacy_executor_local"
	TF_ROOT="infra/deploy"
	STATE_KEY="deploy"
	LOCK_TABLE="${PROJECT}-tf-locks"
	BUCKET="${PROJECT}-state-${ACCOUNT_ID}"

	./scripts/ssm_config.sh set "$SSM_KEY" "$PROVISION_VALUE"

	TARGET_ACCOUNT_IDS="$(./scripts/ssm_config.sh get-or target_account_ids '[]')"
	IMAGE_TAG="$(./scripts/image_tag.sh)"
	RUN_HISTORY_RETENTION_DAYS="$(./scripts/ssm_config.sh get-or run_history_retention_days 90)"
	TMP_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or tmp_lifecycle_days 3)"
	PACKAGE_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or package_lifecycle_days 30)"
	DONE_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or done_lifecycle_days 365)"
	PLAN_RETENTION_DAYS="$(./scripts/ssm_config.sh get-or plan_retention_days 1)"
	API_CALLER_POLICY_JSON="$(./scripts/ssm_config.sh get-or api_caller_policy_json '{}')"

	./scripts/write_tfvars.sh "$TF_ROOT" \
		"aws_region=${AWS_REGION}" \
		"image_tag=${IMAGE_TAG}" \
		"target_account_ids=${TARGET_ACCOUNT_IDS}" \
		"run_history_retention_days=${RUN_HISTORY_RETENTION_DAYS}" \
		"tmp_lifecycle_days=${TMP_LIFECYCLE_DAYS}" \
		"package_lifecycle_days=${PACKAGE_LIFECYCLE_DAYS}" \
		"done_lifecycle_days=${DONE_LIFECYCLE_DAYS}" \
		"plan_retention_days=${PLAN_RETENTION_DAYS}" \
		"api_caller_policy_json=${API_CALLER_POLICY_JSON}" \
		"provision_legacy_executor_local=${PROVISION_VALUE}"
	./scripts/generate_backend.sh "$BUCKET" "$STATE_KEY" "$AWS_REGION" "$TF_ROOT" "$LOCK_TABLE"
	terraform -chdir="$TF_ROOT" init -reconfigure -input=false
	if ! terraform -chdir="$TF_ROOT" apply -input=false -auto-approve; then
		apply_failed
	fi
	;;
remote)
	SSM_KEY="provision_legacy_executor_remote"
	TF_ROOT="infra/target-connect"
	STATE_KEY="target-connect"
	LOCK_TABLE="${PROJECT}-tf-locks"

	[[ -n "$HUB_ACCOUNT_ID" ]] || {
		echo "ERROR: --hub-account-id is required for remote lane" >&2
		exit 1
	}
	[[ "$HUB_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || {
		echo "ERROR: hub-account-id must be 12 digits" >&2
		exit 1
	}
	if [ "$HUB_ACCOUNT_ID" = "$ACCOUNT_ID" ]; then
		echo "ERROR: remote legacy retirement runs in the target account (hub=${HUB_ACCOUNT_ID}, caller=${ACCOUNT_ID})." >&2
		echo "ERROR: Hub legacy is retired with --lane local (just retire-legacy-executor-local)." >&2
		exit 1
	fi

	if [ -z "$STATE_BUCKET" ]; then
		STATE_BUCKET="${PROJECT}-state-${ACCOUNT_ID}"
	fi
	[[ "$STATE_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || {
		echo "ERROR: state bucket name must be valid S3 DNS name syntax: ${STATE_BUCKET}" >&2
		exit 1
	}

	HUB_ROLE_ARN="arn:aws:iam::${HUB_ACCOUNT_ID}:role/${PROJECT}-hub-lambda-exec"
	TARGET_STATE_ARN="arn:aws:s3:::${STATE_BUCKET}"

	./scripts/ssm_config.sh set "$SSM_KEY" "$PROVISION_VALUE"

	./scripts/write_tfvars.sh "$TF_ROOT" \
		"aws_region=${AWS_REGION}" \
		"hub_lambda_exec_role_arn=${HUB_ROLE_ARN}" \
		"state_bucket_arn=${TARGET_STATE_ARN}" \
		"provision_legacy_executor_remote=${PROVISION_VALUE}"
	./scripts/generate_backend.sh "$STATE_BUCKET" "$STATE_KEY" "$AWS_REGION" "$TF_ROOT" "$LOCK_TABLE"
	terraform -chdir="$TF_ROOT" init -reconfigure -input=false
	if ! terraform -chdir="$TF_ROOT" apply -input=false -auto-approve; then
		apply_failed
	fi
	;;
esac

if [ "$RESTORE" = true ]; then
	echo "legacy executor-${LANE} provisioning restored (${SSM_KEY}=${PROVISION_VALUE}) in account ${ACCOUNT_ID}"
else
	echo "legacy executor-${LANE} retired durably (${SSM_KEY}=${PROVISION_VALUE}) in account ${ACCOUNT_ID}"
fi
