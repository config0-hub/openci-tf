#!/usr/bin/env bash
set -euo pipefail

# Verify the installed footprint (mode=present) or that it is gone (mode=clean).
# Pure aws CLI reads; exits non-zero with a list of failures.
#
# Usage: verify.sh present|clean

MODE="${1:?Usage: verify.sh present|clean}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ref 4353245 - openci-tf remote executor consistency naming
PROJECT="${OPENCI_TF_PROJECT:-openci-tf}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
STATE_BUCKET="${PROJECT}-state-${ACCOUNT_ID}"
FAILURES=0

# Tri-state checks: "absent" is accepted ONLY when the API returns its exact
# not-found error. AccessDenied, ExpiredToken, throttling, and network errors
# are indeterminate and FAIL the check — a verifier must never mistake
# "cannot see it" for "it is gone".
NOT_FOUND_RE='404|Not Found|NoSuchBucket|NoSuchEntity|ResourceNotFoundException|StateMachineDoesNotExist|RepositoryNotFoundException|NotFoundException|ParameterNotFound'

check() { # <description> <want:0|1> <command...>
  local desc="$1" want="$2"
  shift 2
  local err got
  err="$(mktemp)"
  if "$@" >/dev/null 2>"$err"; then
    got=1
  elif grep -Eq "$NOT_FOUND_RE" "$err"; then
    got=0
  else
    got=err
  fi
  if [ "$got" = "err" ]; then
    echo "FAIL ${desc} (indeterminate — probe error, not a not-found):"
    sed 's/^/     /' "$err"
    FAILURES=$((FAILURES + 1))
  elif [ "$got" = "$want" ]; then
    echo "OK   ${desc}"
  else
    echo "FAIL ${desc} (expected $([ "$want" = 1 ] && echo present || echo absent))"
    FAILURES=$((FAILURES + 1))
  fi
  rm -f "$err"
}

check_boundary_policy() { # <description> <want:0|1> <policy_name>
  local desc="$1" want="$2" policy="$3"
  local got
  set +e
  boundary_policy_probe "$policy"
  local probe_rc=$?
  set -e
  case "$probe_rc" in
    0) got=1 ;;
    1) got=0 ;;
    *) got=err ;;
  esac
  if [ "$got" = "err" ]; then
    echo "FAIL ${desc} (indeterminate — IAM GetPolicy probe error, not NoSuchEntity):"
    set +e
    boundary_policy_probe "$policy" >/dev/null
    set -e
    FAILURES=$((FAILURES + 1))
  elif [ "$got" = "$want" ]; then
    echo "OK   ${desc}"
  else
    echo "FAIL ${desc} (expected $([ "$want" = 1 ] && echo present || echo absent))"
    FAILURES=$((FAILURES + 1))
  fi
}

readonly_boundary_policy_name() { echo "${1}-permissions-boundary"; }
readonly_boundary_policy_arn() { echo "arn:aws:iam::${ACCOUNT_ID}:policy/$(readonly_boundary_policy_name "$1")"; }

verify_readonly_footprint() {
  local role="${PROJECT}-executor-readonly"
  local boundary
  boundary="$(readonly_boundary_policy_name "$role")"
  local expected_boundary_arn
  expected_boundary_arn="$(readonly_boundary_policy_arn "$role")"
  local role_rc boundary_rc
  set +e
  role_probe "$role"
  role_rc=$?
  boundary_policy_probe "$boundary"
  boundary_rc=$?
  set -e

  if [ "$role_rc" -eq 2 ] || [ "$boundary_rc" -eq 2 ]; then
    echo "FAIL executor-readonly footprint (indeterminate — IAM probe error, not NoSuchEntity):"
    set +e
    [ "$role_rc" -eq 2 ] && role_probe "$role" >/dev/null
    [ "$boundary_rc" -eq 2 ] && boundary_policy_probe "$boundary" >/dev/null
    set -e
    FAILURES=$((FAILURES + 1))
    return
  fi

  if [ "$MODE" = "present" ]; then
    if [ "$role_rc" -ne 0 ]; then
      echo "FAIL role ${role} (expected present)"
      FAILURES=$((FAILURES + 1))
      return
    fi
    if [ "$boundary_rc" -ne 0 ]; then
      echo "FAIL executor-readonly role present but boundary policy ${boundary} is not present"
      FAILURES=$((FAILURES + 1))
      return
    fi
    local attached
    attached="$(aws iam get-role --role-name "$role" --query 'Role.PermissionsBoundary.PermissionsBoundaryArn' --output text 2>/dev/null || true)"
    if [ "$attached" != "$expected_boundary_arn" ]; then
      echo "FAIL executor-readonly role boundary mismatch (expected ${expected_boundary_arn}, got ${attached:-<none>})"
      FAILURES=$((FAILURES + 1))
      return
    fi
    echo "OK   role ${role} (present with matching boundary)"
    return
  fi

  if [ "$role_rc" -eq 0 ]; then
    echo "FAIL role ${role} (expected absent)"
    FAILURES=$((FAILURES + 1))
  else
    echo "OK   role ${role}"
  fi
  if [ "$boundary_rc" -eq 0 ]; then
    echo "FAIL executor-readonly boundary policy ${boundary} (expected absent)"
    FAILURES=$((FAILURES + 1))
  else
    echo "OK   executor-readonly boundary policy ${boundary}"
  fi
}

verify_poweruser_footprint() {
  local role="${PROJECT}-executor-poweruser"
  local boundary
  boundary="$(poweruser_boundary_policy_name "$role")"
  local role_rc boundary_rc
  set +e
  role_probe "$role"
  role_rc=$?
  boundary_policy_probe "$boundary"
  boundary_rc=$?
  set -e

  if [ "$role_rc" -eq 2 ] || [ "$boundary_rc" -eq 2 ]; then
    echo "FAIL optional poweruser footprint (indeterminate — IAM probe error, not NoSuchEntity):"
    set +e
    [ "$role_rc" -eq 2 ] && role_probe "$role" >/dev/null
    [ "$boundary_rc" -eq 2 ] && boundary_policy_probe "$boundary" >/dev/null
    set -e
    FAILURES=$((FAILURES + 1))
    return
  fi

  if [ "$MODE" = "present" ]; then
    if [ "$role_rc" -eq 0 ]; then
      if [ "$boundary_rc" -eq 0 ]; then
        echo "FAIL optional poweruser role has forbidden boundary policy ${boundary}"
        FAILURES=$((FAILURES + 1))
        return
      fi
      local attached
      attached="$(aws iam get-role --role-name "$role" --query 'Role.PermissionsBoundary.PermissionsBoundaryArn' --output text 2>/dev/null || true)"
      case "$attached" in
        ""|None|null) ;;
        *)
          echo "FAIL optional poweruser role has forbidden permissions boundary ${attached}"
          FAILURES=$((FAILURES + 1))
          return
          ;;
      esac
      echo "OK   optional poweruser role ${role} (present without boundary)"
      return
    fi
    if [ "$boundary_rc" -eq 0 ]; then
      echo "FAIL optional poweruser boundary policy ${boundary} present without role ${role}"
      FAILURES=$((FAILURES + 1))
      return
    fi
    echo "NOTE optional poweruser role ${role} absent (optional)"
    return
  fi

  if [ "$role_rc" -eq 0 ]; then
    echo "FAIL optional poweruser role ${role} (expected absent)"
    FAILURES=$((FAILURES + 1))
  else
    echo "OK   optional poweruser role ${role}"
  fi
  if [ "$boundary_rc" -eq 0 ]; then
    echo "FAIL optional poweruser boundary policy ${boundary} (expected absent)"
    FAILURES=$((FAILURES + 1))
  else
    echo "OK   optional poweruser boundary policy ${boundary}"
  fi
}

check_role() { # <description> <want:0|1> <role_name>
  local desc="$1" want="$2" role="$3"
  local got
  set +e
  role_probe "$role"
  local probe_rc=$?
  set -e
  case "$probe_rc" in
    0) got=1 ;;
    1) got=0 ;;
    *) got=err ;;
  esac
  if [ "$got" = "err" ]; then
    echo "FAIL ${desc} (indeterminate — IAM GetRole probe error, not NoSuchEntity):"
    set +e
    role_probe "$role" >/dev/null
    set -e
    FAILURES=$((FAILURES + 1))
  elif [ "$got" = "$want" ]; then
    echo "OK   ${desc}"
  else
    echo "FAIL ${desc} (expected $([ "$want" = 1 ] && echo present || echo absent))"
    FAILURES=$((FAILURES + 1))
  fi
}

bucket_exists() { aws s3api head-bucket --bucket "$1"; }
lambda_exists() { aws lambda get-function --function-name "$1"; }
role_probe() { "$SCRIPT_DIR/role_probe.sh" "$1"; }
boundary_policy_probe() { "$SCRIPT_DIR/boundary_policy_probe.sh" "$1" "$ACCOUNT_ID"; }
poweruser_boundary_policy_name() { echo "${1}-permissions-boundary"; }
sfn_exists() { aws stepfunctions describe-state-machine --state-machine-arn "arn:aws:states:${AWS_REGION:-us-east-1}:${ACCOUNT_ID}:stateMachine:$1"; }
kms_alias_exists() { aws kms describe-key --key-id "alias/$1"; }
table_exists() { aws dynamodb describe-table --table-name "$1"; }
ssm_params_exist() {
  # Install/uninstall only manage the openci-tf and engine namespaces; smoke and
  # other operator namespaces under /openci-tf/install must not affect verify.
  local n total=0
  for proj in openci-tf engine; do
    n="$(aws ssm get-parameters-by-path --path "/openci-tf/install/${proj}" --recursive --query 'length(Parameters)' --output text)" || return 2
    total=$((total + n))
  done
  [ "$total" != "0" ] || { echo "ParameterNotFound: no parameters under /openci-tf/install/{openci-tf,engine}" >&2; return 1; }
}
source_copy_exists() { aws s3api head-object --bucket "$STATE_BUCKET" --key "source/$1/manifest.json"; }
# batch-get-projects exits 0 even for missing projects (reported via
# projectsNotFound) — assert on the returned name, not the CLI status.
codebuild_exists() {
  local n
  n="$(aws codebuild batch-get-projects --names "$1" --query 'projects[0].name' --output text)" || return 2
  [ "$n" = "$1" ] || { echo "ResourceNotFoundException: codebuild project $1" >&2; return 1; }
}

WANT=1
[ "$MODE" = "clean" ] && WANT=0
if [ "$MODE" != "present" ] && [ "$MODE" != "clean" ]; then
  echo "ERROR: mode must be present or clean" >&2
  exit 1
fi

# Keep-state is an EXPLICIT operator decision (OPENCI_TF_KEEP_STATE=yes), never
# inferred from a bucket happening to survive: a failed teardown or a foreign
# bucket must fail the clean check, not be relabeled "kept".
STATE_KEPT=0
if [ "$MODE" = "clean" ] && [ "${OPENCI_TF_KEEP_STATE:-no}" = "yes" ]; then
  STATE_KEPT=1
  echo "NOTE state bucket ${STATE_BUCKET} intentionally kept (OPENCI_TF_KEEP_STATE=yes)"
fi

# Bootstrap
if [ "$STATE_KEPT" = 1 ]; then
  echo "OK   state bucket kept (skipping bucket/lock-table/source-copy absence checks)"
else
  check "state bucket ${STATE_BUCKET}" "$WANT" bucket_exists "$STATE_BUCKET"
  check "lock table ${PROJECT}-tf-locks" "$WANT" table_exists "${PROJECT}-tf-locks"
  # engine-00-bootstrap is skipped: in a combined install the engine adopts the
  # shared state bucket and never applies its own bootstrap root.
  for root in bootstrap foundation deploy target-connect engine; do
    check "source copy ${root}" "$WANT" source_copy_exists "$root"
  done
fi

# Foundation
for b in tmp package "done"; do
  check "foundation bucket ${PROJECT}-${b}-${ACCOUNT_ID}" "$WANT" bucket_exists "${PROJECT}-${b}-${ACCOUNT_ID}"
done
check "KMS alias ${PROJECT}-foundation" "$WANT" kms_alias_exists "${PROJECT}-foundation"

# Engine
for f in init-job worker finalizer; do
  check "engine lambda ${PROJECT}-${f}" "$WANT" lambda_exists "${PROJECT}-${f}"
done
for b in internal "done"; do
  check "engine bucket ${PROJECT}-${b}" "$WANT" bucket_exists "${PROJECT}-${b}"
done
check "engine codebuild project ${PROJECT}-worker" "$WANT" codebuild_exists "${PROJECT}-worker"
check "engine state machine ${PROJECT}-codebuild" "$WANT" sfn_exists "${PROJECT}-codebuild"

# Deploy (hub)
check "settings table ${PROJECT}-settings" "$WANT" table_exists "${PROJECT}-settings"
for machine in \
  "${PROJECT}" \
  "${PROJECT}-apply" \
  "${PROJECT}-destroy" \
  "${PROJECT}-run-folder" \
  "${PROJECT}-run-folder-apply" \
  "${PROJECT}-run-folder-destroy"; do
  check "state machine ${machine}" "$WANT" sfn_exists "$machine"
done
check_role "role ${PROJECT}-hub-lambda-exec" "$WANT" "${PROJECT}-hub-lambda-exec"
verify_readonly_footprint

optional_present() { # <description> <command...>
  local desc="$1"
  shift
  local err probe_rc
  err="$(mktemp)"
  set +e
  "$@" >/dev/null 2>"$err"
  probe_rc=$?
  set -e
  if [ "$probe_rc" -eq 0 ]; then
    echo "OK   ${desc} (present)"
  elif grep -Eq "$NOT_FOUND_RE" "$err"; then
    echo "NOTE ${desc} absent (optional)"
  else
    echo "FAIL ${desc} (indeterminate — probe error, not optional absence):"
    sed 's/^/     /' "$err"
    FAILURES=$((FAILURES + 1))
  fi
  rm -f "$err"
}

optional_present_role() { # <description> <role_name>
  local desc="$1" role="$2"
  set +e
  role_probe "$role"
  local probe_rc=$?
  set -e
  case "$probe_rc" in
    0) echo "OK   ${desc} (present)" ;;
    1) echo "NOTE ${desc} absent (optional)" ;;
    *)
      echo "FAIL ${desc} (indeterminate — IAM GetRole probe error, not NoSuchEntity):"
      set +e
      role_probe "$role" >/dev/null
      set -e
      FAILURES=$((FAILURES + 1))
      ;;
  esac
}

if [ "$MODE" = "present" ]; then
  for r in executor-local executor-remote; do
    optional_present_role "legacy role ${PROJECT}-${r}" "${PROJECT}-${r}"
  done
fi

verify_poweruser_footprint

check "ECR repository ${PROJECT}" "$WANT" aws ecr describe-repositories --repository-names "$PROJECT"

# SSM install config
check "SSM install parameters" "$WANT" ssm_params_exist

# KMS keys cannot be deleted immediately: terraform destroy only SCHEDULES
# deletion (default 30 days). In clean mode, report such residuals explicitly
# instead of pretending the account has zero footprint.
if [ "$MODE" = "clean" ]; then
  PENDING="$(aws kms list-keys --query 'Keys[].KeyId' --output text | tr '\t' '\n' | while read -r kid; do
    aws kms describe-key --key-id "$kid" \
      --query "KeyMetadata.[KeyState,DeletionDate,Description]" --output text 2>/dev/null |
      awk -v id="$kid" -F'\t' '$1=="PendingDeletion" && $3 ~ /openci-tf/ {print id" (deletion "$2")"}'
  done)"
  if [ -n "$PENDING" ]; then
    echo "RESIDUAL: KMS key(s) scheduled for deletion (unavoidable AWS behavior):"
    echo "$PENDING" | sed 's/^/  /'
  fi
fi

if [ "$FAILURES" -gt 0 ]; then
  echo "verify ${MODE}: ${FAILURES} failure(s)"
  exit 1
fi
if [ "$MODE" = "clean" ] && [ -n "${PENDING:-}" ]; then
  echo "verify clean: all checks passed (with KMS pending-deletion residuals listed above)"
else
  echo "verify ${MODE}: all checks passed"
fi
