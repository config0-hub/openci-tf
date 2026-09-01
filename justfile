# PUBLIC kit-synced modules: UpAgent work dispatch, Herdr transport tools, and run control.
# Worktrees: imported modules live in gitignored .shared-llm; symlink before just recipes:
#   ln -sfn ../openci-tf/.shared-llm .shared-llm
import '.shared-llm/public/extensions/common/upagent/justfile'
import '.shared-llm/public/extensions/common/herdr/justfile'
import '.shared-llm/public/extensions/common/runner/justfile'

set positional-arguments

ENGINE_REPO_PATH := env_var_or_default("ENGINE_REPO_PATH", "../aws-execution-engine")
# ref 4353245 - openci-tf remote executor consistency naming
OPENCI_TF_PROJECT := env_var_or_default("OPENCI_TF_PROJECT", "openci-tf")
# ref 4353245 - openci-tf remote executor consistency naming
export TF_VAR_project_name := OPENCI_TF_PROJECT
export SSM_CONFIG_PROJECT := OPENCI_TF_PROJECT
OPENCI_TF_REGION := env_var_or_default("AWS_REGION", env_var_or_default("AWS_DEFAULT_REGION", "us-east-1"))
export AWS_REGION := OPENCI_TF_REGION

# --- install-time configuration (SSM Parameter Store SecureString) -----------

# Set or read /openci-tf/install/<project>/<key>. Non-secret values are passed as
# argv; set-stdin keeps secrets out of shell history and process arguments.
config action key value="":
    #!/usr/bin/env bash
    set -euo pipefail
    action="$1"; key="$2"; value="${3:-}"
    case "$action" in
    set) ./scripts/ssm_config.sh set "$key" "$value" ;;
    set-stdin) ./scripts/ssm_config.sh set-stdin "$key" ;;
    get) ./scripts/ssm_config.sh get "$key" ;;
    *) echo "Usage: just config set|set-stdin|get <key> [value]" >&2; exit 1 ;;
    esac

# --- component recipes (each: SSM -> tfvars -> init/apply -> source copy) ----

# State bucket. Chicken-and-egg: first run applies with LOCAL state (the
# backend bucket does not exist yet), then migrates state into it. Backend
# locking is the S3 native lock file (use_lockfile at init; tofu/terraform
# >= 1.10). No new DynamoDB lock table is provisioned; recovery validates a
# removed legacy table before allowing Terraform to migrate it away.
bootstrap:
    #!/usr/bin/env bash
    set -euo pipefail
    # shellcheck source=scripts/phase_timing.sh
    source ./scripts/phase_timing.sh
    ACCT="$(aws sts get-caller-identity --query Account --output text)"
    BUCKET="{{OPENCI_TF_PROJECT}}-state-${ACCT}"
    LEGACY_LOCK_TABLE="{{OPENCI_TF_PROJECT}}-tf-locks"
    bootstrap_terraform_apply() {
    ./scripts/write_tfvars.sh infra/bootstrap "aws_region={{OPENCI_TF_REGION}}"
    set +e; ./scripts/bucket_exists.sh "$BUCKET"; probe_rc=$?; set -e
    [ "$probe_rc" = 0 ] || [ "$probe_rc" = 1 ] || exit "$probe_rc"
    if [ -s infra/bootstrap/terraform.tfstate ]; then
        # Crash-window recovery: a previous run applied locally but never
        # finished migrating. Resume from LOCAL state ONLY if it provably
        # tracks OUR bucket and legacy table names (no foreign resource may be
        # reachable through this state), and every live tracked resource is
        # not foreign-owned.
        ./scripts/state_identity.sh infra/bootstrap/terraform.tfstate "$BUCKET" "$LEGACY_LOCK_TABLE" || exit 1
        if [ "$probe_rc" = 0 ]; then
            OWNER="$(./scripts/bucket_owner.sh "$BUCKET")"
            [ "$OWNER" = "openci-tf-bootstrap" ] || [ "$OWNER" = "untagged" ] || {
                echo "ERROR: bucket ${BUCKET} is owned by '${OWNER}'; refusing to adopt via local-state resume" >&2; exit 1; }
        fi
        if jq -e '.resources[]? | select(.mode == "managed" and .type == "aws_dynamodb_table" and .name == "locks")' infra/bootstrap/terraform.tfstate >/dev/null; then
            set +e; ./scripts/table_exists.sh "$LEGACY_LOCK_TABLE"; table_rc=$?; set -e
            [ "$table_rc" = 0 ] || [ "$table_rc" = 1 ] || exit "$table_rc"
            if [ "$table_rc" = 0 ]; then
                TABLE_OWNER="$(aws dynamodb list-tags-of-resource --resource-arn "arn:aws:dynamodb:{{OPENCI_TF_REGION}}:${ACCT}:table/${LEGACY_LOCK_TABLE}" --query "Tags[?Key=='ManagedBy'].Value" --output text)"
                [ "$TABLE_OWNER" = "openci-tf-bootstrap" ] || {
                    echo "ERROR: legacy lock table ${LEGACY_LOCK_TABLE} is owned by '${TABLE_OWNER:-untagged}'; refusing local-state migration" >&2; exit 1; }
            fi
        fi
        echo "local bootstrap state survives and tracks ${BUCKET}: resuming interrupted bootstrap"
        rm -f infra/bootstrap/backend.tf
        ./scripts/clear_stale_bootstrap_backend_cache.sh "$BUCKET"
        terraform -chdir=infra/bootstrap init -reconfigure -input=false
        terraform -chdir=infra/bootstrap apply -input=false -auto-approve
        ./scripts/generate_backend.sh "$BUCKET" bootstrap "{{OPENCI_TF_REGION}}" infra/bootstrap
        terraform -chdir=infra/bootstrap init -migrate-state -force-copy -input=false -backend-config=use_lockfile=true
        rm -f infra/bootstrap/terraform.tfstate infra/bootstrap/terraform.tfstate.backup
    elif [ "$probe_rc" = 0 ]; then
        # Existing bucket: only proceed against a bucket this installer owns.
        OWNER="$(./scripts/bucket_owner.sh "$BUCKET")"   # aborts on unreadable tags
        if [ "$OWNER" != "openci-tf-bootstrap" ]; then
            echo "ERROR: bucket ${BUCKET} exists but is not owned by openci-tf-bootstrap (owner: ${OWNER})." >&2
            echo "Refusing to use a foreign bucket as the state backend. Rename OPENCI_TF_PROJECT or free the name." >&2
            exit 1
        fi
        ./scripts/generate_backend.sh "$BUCKET" bootstrap "{{OPENCI_TF_REGION}}" infra/bootstrap
        ./scripts/clear_stale_bootstrap_backend_cache.sh "$BUCKET"
        terraform -chdir=infra/bootstrap init -reconfigure -input=false -backend-config=use_lockfile=true
        terraform -chdir=infra/bootstrap apply -input=false -auto-approve
    else
        rm -f infra/bootstrap/backend.tf
        ./scripts/clear_stale_bootstrap_backend_cache.sh "$BUCKET"
        terraform -chdir=infra/bootstrap init -reconfigure -input=false
        terraform -chdir=infra/bootstrap apply -input=false -auto-approve
        ./scripts/generate_backend.sh "$BUCKET" bootstrap "{{OPENCI_TF_REGION}}" infra/bootstrap
        terraform -chdir=infra/bootstrap init -migrate-state -force-copy -input=false -backend-config=use_lockfile=true
        rm -f infra/bootstrap/terraform.tfstate infra/bootstrap/terraform.tfstate.backup
    fi
    }
    phase_timing_run terraform-apply bootstrap_terraform_apply
    phase_timing_run upload-source ./scripts/upload_source.sh "$BUCKET" bootstrap . infra/bootstrap

# Destroys the state bucket (after emptying it). Handles partial
# first-install failures via local bootstrap state or remote state.
bootstrap-destroy:
    #!/usr/bin/env bash
    set -euo pipefail
    # shellcheck source=scripts/phase_timing.sh
    source ./scripts/phase_timing.sh
    ACCT="$(aws sts get-caller-identity --query Account --output text)"
    BUCKET="{{OPENCI_TF_PROJECT}}-state-${ACCT}"
    LEGACY_LOCK_TABLE="{{OPENCI_TF_PROJECT}}-tf-locks"
    bootstrap_destroy_terraform() {
    ./scripts/write_tfvars.sh infra/bootstrap "aws_region={{OPENCI_TF_REGION}}"
    set +e; ./scripts/bucket_exists.sh "$BUCKET"; bucket_rc=$?; set -e
    [ "$bucket_rc" = 0 ] || [ "$bucket_rc" = 1 ] || exit "$bucket_rc"
    if [ -s infra/bootstrap/terraform.tfstate ]; then
        # Crash-window recovery: local state is authoritative (a migrate never
        # finished). Destroy from it ONLY if it provably tracks OUR bucket,
        # and every live tracked resource passes ownership — ALL checks run
        # BEFORE any destructive side effect (empty/destroy).
        ./scripts/state_identity.sh infra/bootstrap/terraform.tfstate "$BUCKET" "$LEGACY_LOCK_TABLE" || exit 1
        if jq -e '.resources[]? | select(.mode == "managed" and .type == "aws_dynamodb_table" and .name == "locks")' infra/bootstrap/terraform.tfstate >/dev/null; then
            set +e; ./scripts/table_exists.sh "$LEGACY_LOCK_TABLE"; table_rc=$?; set -e
            [ "$table_rc" = 0 ] || [ "$table_rc" = 1 ] || exit "$table_rc"
            if [ "$table_rc" = 0 ]; then
                TABLE_OWNER="$(aws dynamodb list-tags-of-resource --resource-arn "arn:aws:dynamodb:{{OPENCI_TF_REGION}}:${ACCT}:table/${LEGACY_LOCK_TABLE}" --query "Tags[?Key=='ManagedBy'].Value" --output text)"
                [ "$TABLE_OWNER" = "openci-tf-bootstrap" ] || {
                    echo "ERROR: legacy lock table ${LEGACY_LOCK_TABLE} is owned by '${TABLE_OWNER:-untagged}'; refusing local-state destroy" >&2; exit 1; }
            fi
        fi
        if [ "$bucket_rc" = 0 ]; then
            OWNER="$(./scripts/bucket_owner.sh "$BUCKET")"
            [ "$OWNER" = "openci-tf-bootstrap" ] || [ "$OWNER" = "untagged" ] || { echo "ERROR: bucket ${BUCKET} owned by '${OWNER}', refusing to destroy" >&2; exit 1; }
            ./scripts/empty_bucket.sh "$BUCKET"
        fi
        rm -f infra/bootstrap/backend.tf
        terraform -chdir=infra/bootstrap init -reconfigure -input=false
        terraform -chdir=infra/bootstrap destroy -input=false -auto-approve
        rm -f infra/bootstrap/terraform.tfstate infra/bootstrap/terraform.tfstate.backup
    elif [ "$bucket_rc" = 0 ]; then
        # Destructive teardown only on resources this installer provably owns
        # (the bucket).
        OWNER="$(./scripts/bucket_owner.sh "$BUCKET")"   # aborts on unreadable tags
        if [ "$OWNER" != "openci-tf-bootstrap" ]; then
            echo "ERROR: bucket ${BUCKET} exists but is not owned by openci-tf-bootstrap (owner: ${OWNER})." >&2
            echo "Refusing to empty or destroy a bucket this installer cannot prove it created." >&2
            exit 1
        fi
        ./scripts/generate_backend.sh "$BUCKET" bootstrap "{{OPENCI_TF_REGION}}" infra/bootstrap
        terraform -chdir=infra/bootstrap init -reconfigure -input=false -backend-config=use_lockfile=true
        # Move state OUT of the bucket being destroyed, then empty and destroy.
        rm -f infra/bootstrap/backend.tf
        terraform -chdir=infra/bootstrap init -migrate-state -force-copy -input=false -backend-config=use_lockfile=true
        ./scripts/empty_bucket.sh "$BUCKET"
        terraform -chdir=infra/bootstrap destroy -input=false -auto-approve
        rm -f infra/bootstrap/terraform.tfstate infra/bootstrap/terraform.tfstate.backup
    else
        echo "no state bucket or local state; nothing to destroy"
    fi
    }
    phase_timing_run terraform-destroy bootstrap_destroy_terraform
    phase_timing_run operator-cleanup ./scripts/cleanup_operator_footprint.sh
    # Post-destroy verification on EVERY path: the bucket must be gone, and
    # an indeterminate probe (403/expired STS) must fail, not pass.
    set +e; ./scripts/bucket_exists.sh "$BUCKET"; post_bucket_rc=$?; set -e
    [ "$post_bucket_rc" = 1 ] || { echo "ERROR: ${BUCKET} still exists or is unverifiable (rc=${post_bucket_rc})" >&2; exit 1; }

foundation:
    #!/usr/bin/env bash
    set -euo pipefail
    # shellcheck source=scripts/phase_timing.sh
    source ./scripts/phase_timing.sh
    ACCT="$(aws sts get-caller-identity --query Account --output text)"
    BUCKET="{{OPENCI_TF_PROJECT}}-state-${ACCT}"
    foundation_apply() {
    TMP_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or tmp_lifecycle_days 3)"
    PACKAGE_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or package_lifecycle_days 30)"
    DONE_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or done_lifecycle_days 365)"
    PLAN_RETENTION_DAYS="$(./scripts/ssm_config.sh get-or plan_retention_days 1)"
    ./scripts/write_tfvars.sh infra/foundation "aws_region={{OPENCI_TF_REGION}}" "name_prefix={{OPENCI_TF_PROJECT}}" "tmp_lifecycle_days=${TMP_LIFECYCLE_DAYS}" "package_lifecycle_days=${PACKAGE_LIFECYCLE_DAYS}" "done_expiration_days=${DONE_LIFECYCLE_DAYS}" "plan_retention_days=${PLAN_RETENTION_DAYS}"
    ./scripts/generate_backend.sh "$BUCKET" foundation "{{OPENCI_TF_REGION}}" infra/foundation
    terraform -chdir=infra/foundation init -reconfigure -input=false -backend-config=use_lockfile=true
    terraform -chdir=infra/foundation apply -input=false -auto-approve
    }
    phase_timing_run terraform-apply foundation_apply
    phase_timing_run upload-source ./scripts/upload_source.sh "$BUCKET" foundation . infra/foundation

foundation-destroy:
    #!/usr/bin/env bash
    set -euo pipefail
    # shellcheck source=scripts/phase_timing.sh
    source ./scripts/phase_timing.sh
    ACCT="$(aws sts get-caller-identity --query Account --output text)"
    BUCKET="{{OPENCI_TF_PROJECT}}-state-${ACCT}"
    foundation_destroy() {
    TMP_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or tmp_lifecycle_days 3)"
    PACKAGE_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or package_lifecycle_days 30)"
    DONE_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or done_lifecycle_days 365)"
    PLAN_RETENTION_DAYS="$(./scripts/ssm_config.sh get-or plan_retention_days 1)"
    ./scripts/write_tfvars.sh infra/foundation "aws_region={{OPENCI_TF_REGION}}" "name_prefix={{OPENCI_TF_PROJECT}}" "tmp_lifecycle_days=${TMP_LIFECYCLE_DAYS}" "package_lifecycle_days=${PACKAGE_LIFECYCLE_DAYS}" "done_expiration_days=${DONE_LIFECYCLE_DAYS}" "plan_retention_days=${PLAN_RETENTION_DAYS}"
    ./scripts/generate_backend.sh "$BUCKET" foundation "{{OPENCI_TF_REGION}}" infra/foundation
    terraform -chdir=infra/foundation init -reconfigure -input=false -backend-config=use_lockfile=true
    terraform -chdir=infra/foundation destroy -input=false -auto-approve
    }
    phase_timing_run terraform-destroy foundation_destroy

engine:
    #!/usr/bin/env bash
    set -euo pipefail
    # shellcheck source=scripts/phase_timing.sh
    source ./scripts/phase_timing.sh
    engine_install() {
    ENGINE_ROOT="{{ENGINE_REPO_PATH}}"
    if [ -f "${ENGINE_ROOT}/justfile" ]; then
      ENGINE_PROJECT="{{OPENCI_TF_PROJECT}}" just --justfile "${ENGINE_ROOT}/justfile" --working-directory "${ENGINE_ROOT}" install
    else
      chmod +x ./scripts/engine_install.sh
      ./scripts/engine_install.sh "${ENGINE_ROOT}"
    fi
    }
    phase_timing_run engine-install engine_install

engine-destroy:
    #!/usr/bin/env bash
    set -euo pipefail
    # shellcheck source=scripts/phase_timing.sh
    source ./scripts/phase_timing.sh
    engine_uninstall() {
    ENGINE_ROOT="{{ENGINE_REPO_PATH}}"
    if [ -f "${ENGINE_ROOT}/justfile" ]; then
      ENGINE_PROJECT="{{OPENCI_TF_PROJECT}}" just --justfile "${ENGINE_ROOT}/justfile" --working-directory "${ENGINE_ROOT}" uninstall
    else
      chmod +x ./scripts/engine_uninstall.sh
      ./scripts/engine_uninstall.sh "${ENGINE_ROOT}"
    fi
    }
    phase_timing_run engine-uninstall engine_uninstall

# Build the openci-tf Lambda container image at the fixed IMAGE_VERSION and push it to ECR.
docker-push:
    #!/usr/bin/env bash
    set -euo pipefail
    ACCT="$(aws sts get-caller-identity --query Account --output text)"
    REPO="${ACCT}.dkr.ecr.{{OPENCI_TF_REGION}}.amazonaws.com/{{OPENCI_TF_PROJECT}}"
    IMAGE_TAG="$(./scripts/image_tag.sh)"
    aws ecr get-login-password --region "{{OPENCI_TF_REGION}}" | docker login --username AWS --password-stdin "${ACCT}.dkr.ecr.{{OPENCI_TF_REGION}}.amazonaws.com"
    # --provenance=false: Lambda cannot pull OCI attestation manifest lists.
    docker build --platform linux/amd64 --provenance=false --build-arg EXTRA_CA_CERT="{{env_var_or_default('EXTRA_CA_CERT', 'docker/certs/extra-ca.crt.optional')}}" -f docker/Dockerfile -t "${REPO}:${IMAGE_TAG}" .
    docker push "${REPO}:${IMAGE_TAG}"

deploy:
    #!/usr/bin/env bash
    set -euo pipefail
    # shellcheck source=scripts/phase_timing.sh
    source ./scripts/phase_timing.sh
    ACCT="$(aws sts get-caller-identity --query Account --output text)"
    BUCKET="{{OPENCI_TF_PROJECT}}-state-${ACCT}"
    TARGET_ACCOUNT_IDS="$(./scripts/ssm_config.sh get target_account_ids)" || { echo "ERROR: set target accounts first: just config set target_account_ids '[\"123456789012\"]'" >&2; exit 1; }
    IMAGE_TAG="$(./scripts/image_tag.sh)"
    RUN_HISTORY_RETENTION_DAYS="$(./scripts/ssm_config.sh get-or run_history_retention_days 90)"
    RUN_FOLDER_MAX_CONCURRENCY="$(./scripts/ssm_config.sh get-or run_folder_max_concurrency 40)"
    TMP_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or tmp_lifecycle_days 3)"
    PACKAGE_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or package_lifecycle_days 30)"
    DONE_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or done_lifecycle_days 365)"
    PLAN_RETENTION_DAYS="$(./scripts/ssm_config.sh get-or plan_retention_days 1)"
    API_CALLER_POLICY_JSON="$(./scripts/ssm_config.sh get-or api_caller_policy_json '{}')"
    ENABLE_APPLY="$(./scripts/ssm_config.sh get-or enable_apply false)"
    AWS_CONSOLE_START_URL="$(./scripts/ssm_config.sh get-or aws_console_start_url '')"
    AWS_CONSOLE_ROLE_NAME="$(./scripts/ssm_config.sh get-or aws_console_role_name '')"
    ./scripts/write_tfvars.sh infra/deploy "aws_region={{OPENCI_TF_REGION}}" "image_tag=${IMAGE_TAG}" "target_account_ids=${TARGET_ACCOUNT_IDS}" "run_history_retention_days=${RUN_HISTORY_RETENTION_DAYS}" "run_folder_max_concurrency=${RUN_FOLDER_MAX_CONCURRENCY}" "tmp_lifecycle_days=${TMP_LIFECYCLE_DAYS}" "package_lifecycle_days=${PACKAGE_LIFECYCLE_DAYS}" "done_lifecycle_days=${DONE_LIFECYCLE_DAYS}" "plan_retention_days=${PLAN_RETENTION_DAYS}" "api_caller_policy_json=${API_CALLER_POLICY_JSON}" "enable_apply=${ENABLE_APPLY}" "aws_console_start_url=${AWS_CONSOLE_START_URL}" "aws_console_role_name=${AWS_CONSOLE_ROLE_NAME}"
    deploy_terraform_init() {
    ./scripts/generate_backend.sh "$BUCKET" deploy "{{OPENCI_TF_REGION}}" infra/deploy
    terraform -chdir=infra/deploy init -reconfigure -input=false -backend-config=use_lockfile=true
    }
    phase_timing_run terraform-init deploy_terraform_init
    deploy_ecr_bootstrap() {
    # Fresh installs and interrupted deploys need module.ecr in state before push.
    # Upgrades skip the targeted apply when module.ecr already satisfies plan.
    set +e
    terraform -chdir=infra/deploy plan -input=false -target=module.ecr -target=module.hub_setup.aws_iam_role.executor_local -target=module.hub_setup.aws_iam_role_policy.executor_local -detailed-exitcode >/dev/null
    ECR_PLAN_RC=$?
    set -e
    case "$ECR_PLAN_RC" in
      0) echo "ECR module already satisfied; skipping bootstrap target apply" ;;
      2) terraform -chdir=infra/deploy apply -input=false -auto-approve -target=module.ecr -target=module.hub_setup.aws_iam_role.executor_local -target=module.hub_setup.aws_iam_role_policy.executor_local ;;
      *) echo "ERROR: terraform plan for module.ecr failed; aborting deploy" >&2; exit 1 ;;
    esac
    }
    phase_timing_run ecr-bootstrap deploy_ecr_bootstrap
    phase_timing_run engine-image just docker-push
    deploy_terraform_apply() {
    terraform -chdir=infra/deploy apply -input=false -auto-approve
    }
    phase_timing_run terraform-apply deploy_terraform_apply
    phase_timing_run upload-source ./scripts/upload_source.sh "$BUCKET" deploy . infra/deploy infra/modules/hub-setup

deploy-destroy:
    #!/usr/bin/env bash
    set -euo pipefail
    # shellcheck source=scripts/phase_timing.sh
    source ./scripts/phase_timing.sh
    ACCT="$(aws sts get-caller-identity --query Account --output text)"
    BUCKET="{{OPENCI_TF_PROJECT}}-state-${ACCT}"
    TARGET_ACCOUNT_IDS="$(./scripts/ssm_config.sh get-or target_account_ids '[]')"
    IMAGE_TAG="$(./scripts/image_tag.sh)"
    RUN_HISTORY_RETENTION_DAYS="$(./scripts/ssm_config.sh get-or run_history_retention_days 90)"
    RUN_FOLDER_MAX_CONCURRENCY="$(./scripts/ssm_config.sh get-or run_folder_max_concurrency 40)"
    TMP_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or tmp_lifecycle_days 3)"
    PACKAGE_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or package_lifecycle_days 30)"
    DONE_LIFECYCLE_DAYS="$(./scripts/ssm_config.sh get-or done_lifecycle_days 365)"
    PLAN_RETENTION_DAYS="$(./scripts/ssm_config.sh get-or plan_retention_days 1)"
    API_CALLER_POLICY_JSON="$(./scripts/ssm_config.sh get-or api_caller_policy_json '{}')"
    ENABLE_APPLY="$(./scripts/ssm_config.sh get-or enable_apply false)"
    AWS_CONSOLE_START_URL="$(./scripts/ssm_config.sh get-or aws_console_start_url '')"
    AWS_CONSOLE_ROLE_NAME="$(./scripts/ssm_config.sh get-or aws_console_role_name '')"
    deploy_destroy() {
    ./scripts/write_tfvars.sh infra/deploy "aws_region={{OPENCI_TF_REGION}}" "image_tag=${IMAGE_TAG}" "target_account_ids=${TARGET_ACCOUNT_IDS}" "run_history_retention_days=${RUN_HISTORY_RETENTION_DAYS}" "run_folder_max_concurrency=${RUN_FOLDER_MAX_CONCURRENCY}" "tmp_lifecycle_days=${TMP_LIFECYCLE_DAYS}" "package_lifecycle_days=${PACKAGE_LIFECYCLE_DAYS}" "done_lifecycle_days=${DONE_LIFECYCLE_DAYS}" "plan_retention_days=${PLAN_RETENTION_DAYS}" "api_caller_policy_json=${API_CALLER_POLICY_JSON}" "enable_apply=${ENABLE_APPLY}" "aws_console_start_url=${AWS_CONSOLE_START_URL}" "aws_console_role_name=${AWS_CONSOLE_ROLE_NAME}"
    ./scripts/generate_backend.sh "$BUCKET" deploy "{{OPENCI_TF_REGION}}" infra/deploy
    terraform -chdir=infra/deploy init -reconfigure -input=false -backend-config=use_lockfile=true
    ./scripts/terraform_unlock_stale_lock.sh infra/deploy "$BUCKET" deploy
    terraform -chdir=infra/deploy destroy -input=false -auto-approve
    }
    phase_timing_run terraform-destroy deploy_destroy

# Target account: provision or remove the executor-readonly role only.
target-create-aws-readonly hub_account_id state_bucket="":
    #!/usr/bin/env bash
    set -euo pipefail
    hub_account_id="${1:?Usage: just target-create-aws-readonly <hub_account_id> [state_bucket]}"
    state_bucket="${2:-}"
    args=(--action create --role readonly --hub-account-id "$hub_account_id")
    if [ -n "$state_bucket" ]; then args+=(--state-bucket "$state_bucket"); fi
    ./scripts/target_aws_role.sh "${args[@]}"

target-delete-aws-readonly hub_account_id state_bucket="":
    #!/usr/bin/env bash
    set -euo pipefail
    hub_account_id="${1:?Usage: just target-delete-aws-readonly <hub_account_id> [state_bucket]}"
    state_bucket="${2:-}"
    args=(--action destroy --role readonly --hub-account-id "$hub_account_id")
    if [ -n "$state_bucket" ]; then args+=(--state-bucket "$state_bucket"); fi
    ./scripts/target_aws_role.sh "${args[@]}"

# Target account: provision or remove the executor-poweruser role only (opt-in kill switch).
target-create-aws-poweruser hub_account_id state_bucket="":
    #!/usr/bin/env bash
    set -euo pipefail
    hub_account_id="${1:?Usage: just target-create-aws-poweruser <hub_account_id> [state_bucket]}"
    state_bucket="${2:-}"
    args=(--action create --role poweruser --hub-account-id "$hub_account_id")
    if [ -n "$state_bucket" ]; then args+=(--state-bucket "$state_bucket"); fi
    ./scripts/target_aws_role.sh "${args[@]}"

target-delete-aws-poweruser hub_account_id state_bucket="":
    #!/usr/bin/env bash
    set -euo pipefail
    hub_account_id="${1:?Usage: just target-delete-aws-poweruser <hub_account_id> [state_bucket]}"
    state_bucket="${2:-}"
    args=(--action destroy --role poweruser --hub-account-id "$hub_account_id")
    if [ -n "$state_bucket" ]; then args+=(--state-bucket "$state_bucket"); fi
    ./scripts/target_aws_role.sh "${args[@]}"

# Deprecated: use target-create-aws-readonly (readonly role only).
# Public Function URL with application-level bearer auth. The Lambda role must
# also be present in deploy's api_caller_policy_json before operators use it.
console:
    #!/usr/bin/env bash
    set -euo pipefail
    ACCT="$(aws sts get-caller-identity --query Account --output text)"
    BUCKET="{{OPENCI_TF_PROJECT}}-state-${ACCT}"
    ./scripts/ssm_config.sh get console_token >/dev/null || {
        echo "ERROR: set the console token first: just config set-stdin console_token" >&2
        exit 1
    }
    ./scripts/write_tfvars.sh infra/console "aws_region={{OPENCI_TF_REGION}}"
    ./scripts/generate_backend.sh "$BUCKET" console "{{OPENCI_TF_REGION}}" infra/console
    npm --prefix frontend ci
    npm --prefix frontend run package:lambda
    terraform -chdir=infra/console init -reconfigure -input=false -backend-config=use_lockfile=true
    terraform -chdir=infra/console apply -input=false -auto-approve
    ./scripts/upload_source.sh "$BUCKET" console . infra/console
    terraform -chdir=infra/console output -raw function_url
    echo

console-destroy:
    #!/usr/bin/env bash
    set -euo pipefail
    ACCT="$(aws sts get-caller-identity --query Account --output text)"
    BUCKET="{{OPENCI_TF_PROJECT}}-state-${ACCT}"
    ./scripts/ssm_config.sh get console_token >/dev/null || {
        echo "ERROR: console_token must remain configured until console-destroy completes" >&2
        exit 1
    }
    ./scripts/write_tfvars.sh infra/console "aws_region={{OPENCI_TF_REGION}}"
    ./scripts/generate_backend.sh "$BUCKET" console "{{OPENCI_TF_REGION}}" infra/console
    terraform -chdir=infra/console init -reconfigure -input=false -backend-config=use_lockfile=true
    terraform -chdir=infra/console destroy -input=false -auto-approve

# Same-account target connect; cross-account still uses the module with tfvars.
target-connect:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "DEPRECATED: target-connect is an alias for target-create-aws-readonly; use the explicit recipe instead." >&2
    HUB_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
    just target-create-aws-readonly "$HUB_ACCOUNT_ID"

target-connect-destroy:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "DEPRECATED: target-connect-destroy is an alias for target-delete-aws-readonly; use the explicit recipe instead." >&2
    HUB_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
    just target-delete-aws-readonly "$HUB_ACCOUNT_ID"

# --- journeys -----------------------------------------------------------------

# Full install. Default (standalone): bootstrap -> foundation -> engine -> deploy.
# `just install --mode config0-addon`: ecr stage -> GHCR image copy -> deploy stage
# -> repository registration, reusing the tenant engine and state bucket.
install *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    MODE="standalone"
    while [ $# -gt 0 ]; do
        case "$1" in
        --mode) MODE="${2:?--mode requires a value}"; shift 2 ;;
        *) echo "ERROR: unknown install argument: $1 (supported: --mode standalone|config0-addon)" >&2; exit 1 ;;
        esac
    done
    case "$MODE" in
    standalone) exec just install-standalone ;;
    config0-addon) exec just install-config0-addon ;;
    *) echo "ERROR: unknown install mode: $MODE (supported: standalone, config0-addon)" >&2; exit 1 ;;
    esac

install-standalone:
    #!/usr/bin/env bash
    set -euo pipefail
    # shellcheck source=scripts/phase_timing.sh
    source ./scripts/phase_timing.sh
    phase_timing_total_begin
    journey_rc=0
    phase_timing_run bootstrap just bootstrap || journey_rc=$?
    phase_timing_run foundation just foundation || journey_rc=$?
    phase_timing_run engine just engine || journey_rc=$?
    phase_timing_run deploy just deploy || journey_rc=$?
    phase_timing_total_end install "$journey_rc"
    [ "$journey_rc" -eq 0 ] || exit "$journey_rc"
    echo "install complete — hub readonly owned by deploy; run 'just verify'"

# config0-addon install into a tenant account. Reads its inputs from the SSM
# install namespace (just config set <key> <value>); no lock table anywhere —
# state locking is the S3 native lock file (tofu >= 1.10).
install-config0-addon:
    #!/usr/bin/env bash
    set -euo pipefail
    # shellcheck source=scripts/phase_timing.sh
    source ./scripts/phase_timing.sh
    STATE_BUCKET="$(./scripts/ssm_config.sh get state_bucket_name)" || { echo "ERROR: set the tenant state bucket first: just config set state_bucket_name <bucket>" >&2; exit 1; }
    ENGINE_NAME="$(./scripts/ssm_config.sh get engine_name)" || { echo "ERROR: set the tenant engine prefix first: just config set engine_name <name>" >&2; exit 1; }
    GHCR_IMAGE="$(./scripts/ssm_config.sh get ghcr_image)" || { echo "ERROR: set the released image first: just config set ghcr_image ghcr.io/<owner>/openci-tf@sha256:<digest>" >&2; exit 1; }
    GITOPS_REPO="$(./scripts/ssm_config.sh get gitops_repo)" || { echo "ERROR: set the repository first: just config set gitops_repo <owner/repo>" >&2; exit 1; }
    TRIGGER_ID="$(./scripts/ssm_config.sh get trigger_id)" || { echo "ERROR: set the trigger id first: just config set trigger_id <id>" >&2; exit 1; }
    ACCOUNT_ALIAS="$(./scripts/ssm_config.sh get account_alias)" || { echo "ERROR: set the hub account alias first: just config set account_alias <alias>" >&2; exit 1; }
    UPSTREAM_URLS_JSON="$(./scripts/ssm_config.sh get upstream_urls_json)" || { echo "ERROR: set the pinned runtime URLs first: just config set upstream_urls_json '{...}'" >&2; exit 1; }
    API_CALLER_ROLE_ARN="$(./scripts/ssm_config.sh get-or api_caller_role_arn '')"
    addon_args=(--region "{{OPENCI_TF_REGION}}" --project-name "{{OPENCI_TF_PROJECT}}" --state-bucket "$STATE_BUCKET" --engine-name "$ENGINE_NAME")
    deploy_args=("${addon_args[@]}")
    if [ -n "$API_CALLER_ROLE_ARN" ]; then
        deploy_args+=(--trigger-id "$TRIGGER_ID" --api-caller-role-arn "$API_CALLER_ROLE_ARN")
    fi
    register_repository() {
    WEBHOOK_URL="$(tofu -chdir=infra/deploy output -raw webhook_url)"
    python3 install/register_repo.py --repo "$GITOPS_REPO" --trigger-id "$TRIGGER_ID" \
        --account-alias "$ACCOUNT_ALIAS" --webhook-url "$WEBHOOK_URL" \
        --upstream-urls-json "$UPSTREAM_URLS_JSON" \
        --region "{{OPENCI_TF_REGION}}" --project-name "{{OPENCI_TF_PROJECT}}"
    }
    run_stage() {
        local stage="$1"
        shift
        local rc=0
        phase_timing_run "$stage" "$@" || rc=$?
        if [ "$rc" -ne 0 ]; then
            phase_timing_total_end install-config0-addon "$rc"
            echo "ERROR: install-config0-addon stopped at failed stage $stage (rc=$rc); later stages did not run" >&2
            exit "$rc"
        fi
    }
    phase_timing_total_begin
    run_stage addon-ecr python3 install/config0_addon.py --stage ecr "${addon_args[@]}"
    run_stage addon-image-copy ./scripts/copy_ghcr_image.sh --ghcr-image "$GHCR_IMAGE" --region "{{OPENCI_TF_REGION}}" --project "{{OPENCI_TF_PROJECT}}"
    run_stage addon-deploy python3 install/config0_addon.py --stage deploy "${deploy_args[@]}"
    run_stage addon-register register_repository
    phase_timing_total_end install-config0-addon 0
    echo "config0-addon install complete — webhook registered; see install/register_repo.py output for hook_id"

# Exact reverse of install. Set OPENCI_TF_KEEP_STATE=yes|no to skip the prompt.
uninstall:
    #!/usr/bin/env bash
    set -euo pipefail
    # shellcheck source=scripts/phase_timing.sh
    source ./scripts/phase_timing.sh
    phase_timing_total_begin
    journey_rc=0
    KEEP="${OPENCI_TF_KEEP_STATE:-}"
    if [ -z "$KEEP" ]; then
        if [ -t 0 ]; then
            read -r -p "Keep the state bucket + source copies as the surviving record? [yes/no] " KEEP
        else
            echo "ERROR: set OPENCI_TF_KEEP_STATE=yes|no for non-interactive uninstall" >&2
            exit 1
        fi
    fi
    case "$KEEP" in yes|no) ;; *) echo "ERROR: OPENCI_TF_KEEP_STATE must be yes or no" >&2; exit 1 ;; esac
    HUB_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
    set +e
    ./scripts/poweruser_needs_destroy.sh
    probe_rc=$?
    set -e
    case "$probe_rc" in
      0)
        phase_timing_run poweruser-delete just target-delete-aws-poweruser "$HUB_ACCOUNT_ID" || journey_rc=$?
        ;;
      1)
        ;;
      *)
        echo "ERROR: indeterminate probe for optional poweruser footprint; aborting uninstall" >&2
        exit 1
        ;;
    esac
    phase_timing_run deploy-destroy just deploy-destroy || journey_rc=$?
    phase_timing_run engine-destroy just engine-destroy || journey_rc=$?
    phase_timing_run foundation-destroy just foundation-destroy || journey_rc=$?
    if [ "$KEEP" = "yes" ]; then
        echo "keeping state bucket + source copies as the surviving record"
    else
        phase_timing_run bootstrap-destroy just bootstrap-destroy || journey_rc=$?
    fi
    ssm_cleanup() {
    ./scripts/ssm_config.sh delete-all
    SSM_CONFIG_PROJECT=engine ./scripts/ssm_config.sh delete-all
    }
    phase_timing_run ssm-cleanup ssm_cleanup || journey_rc=$?
    phase_timing_run operator-cleanup ./scripts/cleanup_operator_footprint.sh || journey_rc=$?
    phase_timing_total_end uninstall "$journey_rc"
    [ "$journey_rc" -eq 0 ] || exit "$journey_rc"
    echo "uninstall complete — run 'just verify-clean'"

verify:
    #!/usr/bin/env bash
    set -euo pipefail
    # shellcheck source=scripts/phase_timing.sh
    source ./scripts/phase_timing.sh
    phase_timing_run verify ./scripts/verify.sh present

verify-clean:
    #!/usr/bin/env bash
    set -euo pipefail
    # shellcheck source=scripts/phase_timing.sh
    source ./scripts/phase_timing.sh
    phase_timing_run verify-clean ./scripts/verify.sh clean

# --- operator utilities ---------------------------------------------------------

# Store a fine-grained GitHub control PAT from a file or stdin under /openci-tf/clone-token/<repo>-control.
install-github-control-token *ARGS:
    @if [ $# -gt 0 ]; then ./scripts/install_github_control_token.sh "$@"; else ./scripts/install_github_control_token.sh --help; fi

# Register a repository after read-only GitHub control-token capability verification.
register-repo *ARGS:
    @if [ $# -gt 0 ]; then ./scripts/register_repo.sh "$@"; else ./scripts/register_repo.sh --help; fi
register-account *ARGS:
    @if [ $# -gt 0 ]; then ./scripts/register_account.sh "$@"; else ./scripts/register_account.sh --help; fi

account-set-apply *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    alias="${1:?Usage: just account-set-apply <alias> true|false}"
    value="${2:?Usage: just account-set-apply <alias> true|false}"
    ./scripts/account_set_apply.sh --alias "$alias" --enable-apply "$value"

# Target account: verify identity, existing state bucket, SSM tfvars, then readonly role.
target-onboard *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    # shellcheck source=scripts/phase_timing.sh
    source ./scripts/phase_timing.sh
    hub_account_id="${1:?Usage: just target-onboard <hub_account_id> [state_bucket]}"
    state_bucket="${2:-}"
    args=(--hub-account-id "$hub_account_id")
    if [ -n "$state_bucket" ]; then args+=(--state-bucket "$state_bucket"); fi
    phase_timing_total_begin
    journey_rc=0
    phase_timing_run target-onboard ./scripts/target_onboard.sh "${args[@]}" || journey_rc=$?
    phase_timing_total_end target-onboard "$journey_rc"
    [ "$journey_rc" -eq 0 ] || exit "$journey_rc"

# Hub account: append target_account_ids, redeploy IAM, then register alias.
register-target *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    alias="${1:?Usage: just register-target <alias> <target_account_id>}"
    target_account_id="${2:?Usage: just register-target <alias> <target_account_id>}"
    ./scripts/register_target.sh --alias "$alias" --account-id "$target_account_id"
create-webhook *ARGS:
    @if [ $# -gt 0 ]; then ./scripts/create_webhook.sh "$@"; else ./scripts/create_webhook.sh --help; fi

# Store the Infracost API key in /openci-tf/infracost/api_key (stdin or env file; never argv).
configure-infracost env_file="/tmp/infracost/api.env":
    #!/usr/bin/env bash
    set -euo pipefail
    set -a
    # shellcheck disable=SC1090
    source "{{env_file}}"
    set +a
    : "${INFRACOST_API_KEY:?INFRACOST_API_KEY missing from env file}"
    printf '%s' "$INFRACOST_API_KEY" | ./scripts/configure_infracost.sh

# Install a dotenv file as a hub SSM SecureString under /openci-tf/env/.
# Example for the shared VPC module credential:
#   just install-ssm-env /openci-tf/env/github/example-org/private-module-repo ./github.env
install-ssm-env *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "$#" -ne 2 ]; then
        echo "Usage: just install-ssm-env <ssm_path> <dotenv_file>" >&2
        exit 1
    fi
    ssm_path="${1:?Usage: just install-ssm-env <ssm_path> <dotenv_file>}"
    dotenv_file="${2:?Usage: just install-ssm-env <ssm_path> <dotenv_file>}"
    ./scripts/install_ssm_env.sh "$ssm_path" "$dotenv_file"

# Trigger the registered smoke PR by posting `tf plan <folder>` and wait for the outer SFN.
# Set SSM_CONFIG_PROJECT to the smoke namespace (for example openci-tf-smoke-YYYYmmddHHMMSS).
smoke:
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/smoke.sh

# IAM-authenticated core API helpers (see docs/API.md). Never prints credentials.
api-create-run trigger_id sha idempotency_key folder="" pipeline="" action="plan":
    #!/usr/bin/env bash
    set -euo pipefail
    body="$(
    python3 - "{{trigger_id}}" "{{sha}}" "{{idempotency_key}}" "{{folder}}" "{{pipeline}}" "{{action}}" <<'PY'
    import json
    import sys
    trigger_id, sha, idempotency_key, folder, pipeline, action = sys.argv[1:]
    if bool(folder) == bool(pipeline):
        raise SystemExit("set exactly one of folder= or pipeline=")
    body = {
        "trigger_id": trigger_id,
        "commit_hash": sha,
        "action": action,
        "idempotency_key": idempotency_key,
        "notification_target": {"type": "registry"},
    }
    if folder:
        body["folder_mode"] = "explicit"
        body["folders"] = [folder]
    else:
        body["pipeline"] = pipeline
    print(json.dumps(body))
    PY
    )"
    ./scripts/api_invoke.sh POST "/runs" "$body"

api-get-run run_id:
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/api_invoke.sh GET "/runs/{{run_id}}"

api-list-runs trigger_id limit="25":
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/api_invoke.sh GET "/runs?trigger_id={{trigger_id}}&limit={{limit}}"

update: engine deploy
test:
    cp "{{ENGINE_REPO_PATH}}/aws_exe_sys/common/payload.py" docker/engine_ref/payload.py && docker build --build-arg EXTRA_CA_CERT="{{env_var_or_default('EXTRA_CA_CERT', 'docker/certs/extra-ca.crt.optional')}}" -f docker/Dockerfile.test -t openci-tf-test . && docker run --rm openci-tf-test tests/ -v --tb=short
