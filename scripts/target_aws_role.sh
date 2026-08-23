#!/usr/bin/env bash
# Provision or destroy one target-account executor role root (readonly or poweruser).
# Remote target accounts only — refuses same-account/hub mode (hub readonly is deploy-owned).
set -euo pipefail

PROJECT="${OPENCI_TF_PROJECT:-openci-tf}"
ACTION=""
ROLE_KIND=""
HUB_ACCOUNT_ID=""
STATE_BUCKET=""
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

usage() {
	echo 'Usage: target_aws_role.sh --action create|destroy --role readonly|poweruser --hub-account-id 12_DIGITS [--state-bucket NAME]' >&2
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

validate_state_bucket() {
	[[ "$STATE_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || {
		echo "ERROR: state bucket name must be valid S3 DNS name syntax: ${STATE_BUCKET}" >&2
		exit 1
	}
}

while [[ $# -gt 0 ]]; do
	case "$1" in
	--help | -h) usage ;;
	--action)
		ACTION="$2"
		shift 2
		;;
	--role)
		ROLE_KIND="$2"
		shift 2
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

case "$ACTION" in
create | destroy) ;;
*)
	echo "ERROR: --action must be create or destroy" >&2
	exit 1
	;;
esac

case "$ROLE_KIND" in
readonly | poweruser) ;;
*)
	echo "ERROR: --role must be readonly or poweruser" >&2
	exit 1
	;;
esac

[[ -n "$HUB_ACCOUNT_ID" ]] || {
	echo "ERROR: --hub-account-id is required" >&2
	exit 1
}
[[ "$HUB_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || {
	echo "ERROR: hub-account-id must be 12 digits" >&2
	exit 1
}

validate_project_name
validate_region

TARGET_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
[[ "$TARGET_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || {
	echo "ERROR: could not resolve a valid target account id from caller identity" >&2
	exit 1
}

if [ "$ROLE_KIND" = "readonly" ] && [ "$HUB_ACCOUNT_ID" = "$TARGET_ACCOUNT_ID" ]; then
	echo "ERROR: target-create-aws-readonly refuses same-account/hub mode (hub=${HUB_ACCOUNT_ID}, target=${TARGET_ACCOUNT_ID})." >&2
	echo "ERROR: Hub readonly is owned by hub-setup deploy (just deploy). See docs/EXECUTOR_ROLES.md." >&2
	exit 1
fi

if [ -z "$STATE_BUCKET" ]; then
	STATE_BUCKET="${PROJECT}-state-${TARGET_ACCOUNT_ID}"
fi
validate_state_bucket

HUB_ROLE_ARN="arn:aws:iam::${HUB_ACCOUNT_ID}:role/${PROJECT}-hub-lambda-exec"
TARGET_STATE_ARN="arn:aws:s3:::${STATE_BUCKET}"
LOCK_TABLE="${PROJECT}-tf-locks"
PROVISION_LEGACY="$(./scripts/ssm_config.sh get-or provision_legacy_executor_remote true)"
ENABLE_APPLY="$(./scripts/ssm_config.sh get-or enable_apply false)"

if [ "$ACTION" = "create" ]; then
	set +e
	./scripts/bucket_exists.sh "$STATE_BUCKET"
	probe_rc=$?
	set -e
	case "$probe_rc" in
	0) ;;
	1)
		echo "ERROR: state bucket ${STATE_BUCKET} does not exist in account ${TARGET_ACCOUNT_ID}" >&2
		exit 1
		;;
	*)
		exit "$probe_rc"
		;;
	esac

	set +e
	LOCK_STATUS="$(aws dynamodb describe-table --table-name "$LOCK_TABLE" --query 'Table.TableStatus' --output text)"
	lock_probe_rc=$?
	set -e
	if [ "$lock_probe_rc" -ne 0 ]; then
		echo "ERROR: target lock table ${LOCK_TABLE} does not exist or is unreadable in account ${TARGET_ACCOUNT_ID}" >&2
		exit "$lock_probe_rc"
	fi
	if [ "$LOCK_STATUS" != "ACTIVE" ]; then
		echo "ERROR: target lock table ${LOCK_TABLE} is not ACTIVE (status=${LOCK_STATUS})" >&2
		exit 1
	fi

	./scripts/ssm_config.sh set hub_lambda_exec_role_arn "$HUB_ROLE_ARN"
	./scripts/ssm_config.sh set target_state_bucket_arn "$TARGET_STATE_ARN"
fi

case "$ROLE_KIND" in
readonly)
	TF_ROOT="infra/target-connect"
	STATE_KEY="target-connect"
	;;
poweruser)
	TF_ROOT="infra/target-connect-poweruser"
	STATE_KEY="target-connect-poweruser"
	;;
esac

# Always derive backend/tfvars from validated explicit arguments — never stale SSM on destroy.
TFVARS=(
	"aws_region=${AWS_REGION}"
	"hub_lambda_exec_role_arn=${HUB_ROLE_ARN}"
	"state_bucket_arn=${TARGET_STATE_ARN}"
)
if [ "$ROLE_KIND" = "readonly" ]; then
	TFVARS+=("provision_legacy_executor_remote=${PROVISION_LEGACY}")
	TFVARS+=("enable_apply=${ENABLE_APPLY}")
fi
./scripts/write_tfvars.sh "$TF_ROOT" "${TFVARS[@]}"
./scripts/generate_backend.sh "$STATE_BUCKET" "$STATE_KEY" "$AWS_REGION" "$TF_ROOT" "$LOCK_TABLE"
terraform -chdir="$TF_ROOT" init -reconfigure -input=false
if [ "$ACTION" = "create" ]; then
	terraform -chdir="$TF_ROOT" apply -input=false -auto-approve
	if [ "$ROLE_KIND" = "readonly" ]; then
		./scripts/upload_source.sh "$STATE_BUCKET" "$STATE_KEY" . "$TF_ROOT" infra/modules/executor-readonly
	else
		./scripts/upload_source.sh "$STATE_BUCKET" "$STATE_KEY" . "$TF_ROOT" infra/modules/executor-poweruser
	fi
	echo "target ${ROLE_KIND} role provisioned in account ${TARGET_ACCOUNT_ID} (hub=${HUB_ACCOUNT_ID}, bucket=${STATE_BUCKET})"
else
	if [ "$ROLE_KIND" = "readonly" ]; then
		echo "WARNING: targeted destroy for readonly role only; legacy executor-remote in this state root is preserved." >&2
		echo "WARNING: -target is exceptional — verify no unintended resources remain after destroy." >&2
		terraform -chdir="$TF_ROOT" destroy \
			-target=module.executor_readonly \
			-input=false -auto-approve
	else
		terraform -chdir="$TF_ROOT" destroy -input=false -auto-approve
	fi
	echo "target ${ROLE_KIND} role removed from account ${TARGET_ACCOUNT_ID} (hub=${HUB_ACCOUNT_ID}, bucket=${STATE_BUCKET})"
fi
