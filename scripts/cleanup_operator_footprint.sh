#!/usr/bin/env bash
set -euo pipefail

# Remove operator-managed resources that survive Terraform destroy:
# - CloudWatch log groups for product Lambdas, CodeBuild, and Step Functions
# - SSM parameters outside /openci-tf/install/ (clone tokens, env dotenv, infracost, webhook)
# - Legacy executor-local and executor-remote IAM roles
#
# Usage: cleanup_operator_footprint.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=product_log_groups.sh
source "$SCRIPT_DIR/product_log_groups.sh"
PROJECT="${OPENCI_TF_PROJECT:-openci-tf}"
NOT_FOUND_RE='404|Not Found|ResourceNotFoundException|ParameterNotFound|NoSuchEntity'

delete_log_group_if_exists() {
  local name="$1" err
  err="$(mktemp)"
  if aws logs delete-log-group --log-group-name "$name" >/dev/null 2>"$err"; then
    echo "deleted log group ${name}"
  elif grep -Eq "$NOT_FOUND_RE" "$err"; then
    :
  else
    echo "ERROR: could not delete log group ${name}:" >&2
    sed 's/^/     /' "$err" >&2
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
}

delete_ssm_prefix() {
  local prefix="$1" names name err
  err="$(mktemp)"
  if ! names="$(aws ssm get-parameters-by-path --path "$prefix" --recursive \
    --query 'Parameters[].Name' --output text 2>"$err")"; then
    if grep -Eq "$NOT_FOUND_RE" "$err"; then
      rm -f "$err"
      return 0
    fi
    echo "ERROR: could not list SSM parameters under ${prefix}:" >&2
    sed 's/^/     /' "$err" >&2
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
  if [ -z "$names" ] || [ "$names" = "None" ]; then
    return 0
  fi
  for name in $names; do
    aws ssm delete-parameter --name "$name"
    echo "deleted ${name}"
  done
}

delete_legacy_role_if_exists() {
  local role="$1" probe_rc
  set +e
  "$SCRIPT_DIR/role_probe.sh" "$role"
  probe_rc=$?
  set -e
  case "$probe_rc" in
    1) return 0 ;;
    2)
      echo "ERROR: indeterminate IAM probe for legacy role ${role}; aborting cleanup" >&2
      return 1
      ;;
  esac

  local policy_arn policies inline
  policies="$(aws iam list-attached-role-policies --role-name "$role" --query 'AttachedPolicies[].PolicyArn' --output text)"
  for policy_arn in $policies; do
    [ -n "$policy_arn" ] && [ "$policy_arn" != "None" ] || continue
    aws iam detach-role-policy --role-name "$role" --policy-arn "$policy_arn"
    echo "detached ${policy_arn} from ${role}"
  done
  inline="$(aws iam list-role-policies --role-name "$role" --query 'PolicyNames[]' --output text)"
  for inline in $inline; do
    [ -n "$inline" ] && [ "$inline" != "None" ] || continue
    aws iam delete-role-policy --role-name "$role" --policy-name "$inline"
    echo "deleted inline policy ${inline} from ${role}"
  done
  aws iam delete-role --role-name "$role"
  echo "deleted legacy role ${role}"
}

while IFS= read -r log_group; do
  [ -n "$log_group" ] || continue
  delete_log_group_if_exists "$log_group"
done < <(product_log_group_names "$PROJECT")

for prefix in \
  /openci-tf/clone-token \
  /openci-tf/env \
  /openci-tf/infracost \
  /openci-tf/webhook; do
  delete_ssm_prefix "$prefix"
done

for legacy in executor-local executor-remote; do
  delete_legacy_role_if_exists "${PROJECT}-${legacy}"
done

echo "operator footprint cleanup complete"
