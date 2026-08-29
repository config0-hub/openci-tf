# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate the sole command executed by the engine."""
from __future__ import annotations

import shlex
from dataclasses import dataclass

from src.domain.cmd_builder.installers import (
    bin_dir,
    render_installer,
    require_pinned_installer,
)

_SAFE_VERBS = frozenset({"plan", "report", "drift", "plan_destroy", "apply", "destroy"})

_GIT_AUTH_SETUP = """
_on_exit() {
  if [ -n "${_OPENCI_TF_GIT_ASKPASS:-}" ] && [ -f "${_OPENCI_TF_GIT_ASKPASS}" ]; then
    rm -f "${_OPENCI_TF_GIT_ASKPASS}"
  fi
  upload_artifacts
}
trap _on_exit EXIT
if [ -n "${GITHUB_TOKEN:-}" ]; then
  export GIT_TERMINAL_PROMPT=0
  export GIT_CONFIG_COUNT=2
  export GIT_CONFIG_KEY_0=url.https://github.com/.insteadOf
  export GIT_CONFIG_VALUE_0=git@github.com:
  export GIT_CONFIG_KEY_1=url.https://github.com/.insteadOf
  export GIT_CONFIG_VALUE_1=ssh://git@github.com/
  _OPENCI_TF_GIT_ASKPASS="$(mktemp)"
  chmod 700 "${_OPENCI_TF_GIT_ASKPASS}"
  cat > "${_OPENCI_TF_GIT_ASKPASS}" <<'OPENCI_TF_ASKPASS_EOF'
#!/usr/bin/env bash
case "$1" in
  *Username*) printf '%s' 'x-access-token' ;;
  *Password*) printf '%s' "${GITHUB_TOKEN}" ;;
  *) exit 1 ;;
esac
OPENCI_TF_ASKPASS_EOF
  chmod +x "${_OPENCI_TF_GIT_ASKPASS}"
  export GIT_ASKPASS="${_OPENCI_TF_GIT_ASKPASS}"
fi
""".strip()

_PLAN_ARTIFACT_HELPER = r'''
upload_plan_binary_artifact() {
  plan_file="${ARTIFACTS_DIR:-/tmp}/binary-plan/plan.tfplan"
  sha_file="${ARTIFACTS_DIR:-/tmp}/binary-plan/plan.tfplan.sha256"
  metadata_file="${ARTIFACTS_DIR:-/tmp}/binary-plan/plan-metadata.json"
  [ -s "$plan_file" ] || { echo "Error: missing binary plan artifact" >&2; exit 20; }
  python3 - "$plan_file" "$sha_file" "$metadata_file" <<'OPENCI_TF_PLAN_META_PY'
import datetime
import hashlib
import json
import os
import re
import sys

plan_path, sha_path, metadata_path = sys.argv[1:4]
required = (
    "PLAN_BINARY_PUT_URL",
    "PLAN_SHA256_PUT_URL",
    "PLAN_METADATA_PUT_URL",
    "OPENCI_TF_PLAN_S3_URI",
    "OPENCI_TF_PLAN_SHA256_S3_URI",
    "OPENCI_TF_PLAN_METADATA_S3_URI",
    "OPENCI_TF_PLAN_EXPIRES_AFTER_DAYS",
    "OPENCI_TF_REPO_NAME",
    "OPENCI_TF_RUN_ID",
    "OPENCI_TF_PINNED_SHA",
    "OPENCI_TF_ACCOUNT_ID",
    "OPENCI_TF_FOLDER",
    "OPENCI_TF_ACTION",
    "OPENCI_TF_TF_RUNTIME",
)
missing = [name for name in required if not os.environ.get(name)]
if missing:
    raise SystemExit("missing plan artifact metadata: " + ", ".join(missing))
sha256 = hashlib.sha256()
with open(plan_path, "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        sha256.update(chunk)
checksum = sha256.hexdigest()
with open(sha_path, "w", encoding="utf-8") as handle:
    handle.write(checksum + "\n")
created_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
expires_after_days = int(os.environ["OPENCI_TF_PLAN_EXPIRES_AFTER_DAYS"])
expires_at = created_at + datetime.timedelta(days=expires_after_days)
metadata = {
    "repo": os.environ["OPENCI_TF_REPO_NAME"],
    "run_id": os.environ["OPENCI_TF_RUN_ID"],
    "pinned_sha": os.environ["OPENCI_TF_PINNED_SHA"],
    "account_id": os.environ["OPENCI_TF_ACCOUNT_ID"],
    "folder": os.environ["OPENCI_TF_FOLDER"],
    "action": os.environ["OPENCI_TF_ACTION"],
    "opentofu_runtime": os.environ["OPENCI_TF_TF_RUNTIME"],
    "created_at": created_at.isoformat().replace("+00:00", "Z"),
    "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    "expires_after_days": expires_after_days,
    "plan_s3_uri": os.environ["OPENCI_TF_PLAN_S3_URI"],
    "sha256_s3_uri": os.environ["OPENCI_TF_PLAN_SHA256_S3_URI"],
    "metadata_s3_uri": os.environ["OPENCI_TF_PLAN_METADATA_S3_URI"],
    "sha256": checksum,
}
if not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"]):
    raise SystemExit("invalid plan checksum")
encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
if len(encoded) > 4096:
    raise SystemExit("plan artifact metadata exceeds 4096 bytes")
with open(metadata_path, "wb") as handle:
    handle.write(encoded + b"\n")
OPENCI_TF_PLAN_META_PY
  curl -sS --fail-with-body --retry 3 -H 'Content-Type: application/octet-stream' --upload-file "$plan_file" "$PLAN_BINARY_PUT_URL" || { status=$?; echo "Error: upload failed for plan.tfplan" >&2; exit "$status"; }
  curl -sS --fail-with-body --retry 3 -H 'Content-Type: text/plain' --upload-file "$sha_file" "$PLAN_SHA256_PUT_URL" || { status=$?; echo "Error: upload failed for plan.tfplan.sha256" >&2; exit "$status"; }
  curl -sS --fail-with-body --retry 3 -H 'Content-Type: application/json' --upload-file "$metadata_file" "$PLAN_METADATA_PUT_URL" || { status=$?; echo "Error: upload failed for plan-metadata.json" >&2; exit "$status"; }
}
'''.strip()

_DESTROY_PLAN_ARTIFACT_HELPER = r'''
upload_destroy_plan_binary_artifact() {
  plan_file="${ARTIFACTS_DIR:-/tmp}/binary-plan/destroy.plan.tfplan"
  sha_file="${ARTIFACTS_DIR:-/tmp}/binary-plan/destroy.plan.tfplan.sha256"
  metadata_file="${ARTIFACTS_DIR:-/tmp}/binary-plan/destroy-plan-metadata.json"
  [ -s "$plan_file" ] || { echo "Error: missing destroy plan artifact" >&2; exit 20; }
  python3 - "$plan_file" "$sha_file" "$metadata_file" <<'OPENCI_TF_DESTROY_PLAN_META_PY'
import datetime
import hashlib
import json
import os
import re
import sys

plan_path, sha_path, metadata_path = sys.argv[1:4]
required = (
    "DESTROY_PLAN_BINARY_PUT_URL",
    "DESTROY_PLAN_SHA256_PUT_URL",
    "DESTROY_PLAN_METADATA_PUT_URL",
    "OPENCI_TF_DESTROY_PLAN_S3_URI",
    "OPENCI_TF_DESTROY_PLAN_SHA256_S3_URI",
    "OPENCI_TF_DESTROY_PLAN_METADATA_S3_URI",
    "OPENCI_TF_PLAN_EXPIRES_AFTER_DAYS",
    "OPENCI_TF_REPO_NAME",
    "OPENCI_TF_RUN_ID",
    "OPENCI_TF_PINNED_SHA",
    "OPENCI_TF_ACCOUNT_ID",
    "OPENCI_TF_FOLDER",
    "OPENCI_TF_ACTION",
    "OPENCI_TF_TF_RUNTIME",
)
missing = [name for name in required if not os.environ.get(name)]
if missing:
    raise SystemExit("missing destroy plan artifact metadata: " + ", ".join(missing))
sha256 = hashlib.sha256()
with open(plan_path, "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        sha256.update(chunk)
checksum = sha256.hexdigest()
with open(sha_path, "w", encoding="utf-8") as handle:
    handle.write(checksum + "\n")
created_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
expires_after_days = int(os.environ["OPENCI_TF_PLAN_EXPIRES_AFTER_DAYS"])
expires_at = created_at + datetime.timedelta(days=expires_after_days)
metadata = {
    "repo": os.environ["OPENCI_TF_REPO_NAME"],
    "run_id": os.environ["OPENCI_TF_RUN_ID"],
    "pinned_sha": os.environ["OPENCI_TF_PINNED_SHA"],
    "account_id": os.environ["OPENCI_TF_ACCOUNT_ID"],
    "folder": os.environ["OPENCI_TF_FOLDER"],
    "action": os.environ["OPENCI_TF_ACTION"],
    "opentofu_runtime": os.environ["OPENCI_TF_TF_RUNTIME"],
    "created_at": created_at.isoformat().replace("+00:00", "Z"),
    "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    "expires_after_days": expires_after_days,
    "plan_s3_uri": os.environ["OPENCI_TF_DESTROY_PLAN_S3_URI"],
    "sha256_s3_uri": os.environ["OPENCI_TF_DESTROY_PLAN_SHA256_S3_URI"],
    "metadata_s3_uri": os.environ["OPENCI_TF_DESTROY_PLAN_METADATA_S3_URI"],
    "sha256": checksum,
}
if not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"]):
    raise SystemExit("invalid destroy plan checksum")
encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
if len(encoded) > 4096:
    raise SystemExit("destroy plan artifact metadata exceeds 4096 bytes")
with open(metadata_path, "wb") as handle:
    handle.write(encoded + b"\n")
OPENCI_TF_DESTROY_PLAN_META_PY
  curl -sS --fail-with-body --retry 3 -H 'Content-Type: application/octet-stream' --upload-file "$plan_file" "$DESTROY_PLAN_BINARY_PUT_URL" || { status=$?; echo "Error: upload failed for destroy.plan.tfplan" >&2; exit "$status"; }
  curl -sS --fail-with-body --retry 3 -H 'Content-Type: text/plain' --upload-file "$sha_file" "$DESTROY_PLAN_SHA256_PUT_URL" || { status=$?; echo "Error: upload failed for destroy.plan.tfplan.sha256" >&2; exit "$status"; }
  curl -sS --fail-with-body --retry 3 -H 'Content-Type: application/json' --upload-file "$metadata_file" "$DESTROY_PLAN_METADATA_PUT_URL" || { status=$?; echo "Error: upload failed for destroy-plan-metadata.json" >&2; exit "$status"; }
}
'''.strip()

_APPLY_PLAN_HELPER = r'''
download_and_verify_pinned_plan() {
  plan_file="${ARTIFACTS_DIR:-/tmp}/binary-plan/pinned.plan.tfplan"
  mkdir -p "${ARTIFACTS_DIR:-/tmp}/binary-plan"
  expected_name="${OPENCI_TF_PLAN_ARTIFACT_NAME:?missing OPENCI_TF_PLAN_ARTIFACT_NAME}"
  if [ "$expected_name" != "plan.tfplan" ] && [ "$expected_name" != "destroy.plan.tfplan" ]; then
    echo "Error: unsupported pinned plan artifact ${expected_name}" >&2
    exit 21
  fi
  curl -sS --fail-with-body --retry 3 -o "$plan_file" "$PINNED_PLAN_GET_URL" || { status=$?; echo "Error: failed to download pinned plan" >&2; exit "$status"; }
  actual="$(python3 - <<'OPENCI_TF_VERIFY_SHA_PY'
import hashlib
import os
import sys
path = os.environ["PLAN_FILE"]
expected = os.environ["OPENCI_TF_PINNED_PLAN_SHA256"]
sha256 = hashlib.sha256()
with open(path, "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        sha256.update(chunk)
actual = sha256.hexdigest()
if actual != expected:
    raise SystemExit(f"pinned plan sha256 mismatch: expected {expected}, got {actual}")
print(actual)
OPENCI_TF_VERIFY_SHA_PY
)"
  export PINNED_PLAN_FILE="$plan_file"
}
'''.strip()


@dataclass(frozen=True)
class ScriptParams:
    verb: str
    execution_target: str
    binary: str = "tofu"
    binary_version: str = "1.8.0"
    folder: str = "."
    normalize_drift: bool = False
    extra_flags: tuple[str, ...] = ()


def _installer_specs(params: ScriptParams) -> tuple[tuple[str, str], ...]:
    specs = ((params.binary, params.binary_version),)
    if params.verb in {"plan", "report"}:
        return (*specs, ("tfsec", "1.28.10"), ("infracost", "0.10.39"))
    return specs


def _artifact_names(verb: str) -> tuple[str, ...]:
    if verb == "plan_destroy":
        return ("init.out", "validate.out", "destroy.plan.out")
    if verb == "apply":
        return ("init.out", "validate.out", "plan-show.out", "apply.out")
    if verb == "destroy":
        return ("init.out", "validate.out", "plan-show.out", "destroy.out")
    names = ("init.out", "validate.out", "tf/plan.out", "drift.json")
    if verb in {"plan", "report"}:
        return (*names, "tfsec.json", "tfsec.output", "infracost.json", "infracost.output")
    return names


def _render_plan_like(params: ScriptParams) -> str:
    command = "plan" if params.verb in {"report", "drift"} else params.verb
    plan_enabled = params.verb in {"plan", "report"}
    destroy_plan = params.verb == "plan_destroy"
    if (plan_enabled or destroy_plan) and any(flag == "-out" or flag.startswith("-out=") for flag in params.extra_flags):
        raise ValueError("extra_flags may not override the managed plan -out path")
    drift = params.normalize_drift or params.verb == "drift"
    detailed = " -detailed-exitcode" if drift else ""
    flags = " ".join(shlex.quote(flag) for flag in params.extra_flags)
    tool, workdir, directory = (shlex.quote(params.binary), shlex.quote(params.folder), shlex.quote(bin_dir(params.execution_target)))
    installers = "\n".join(
        render_installer(binary, version, params.execution_target, require_pinned_installer(binary, version).sha256)
        for binary, version in _installer_specs(params)
    )
    artifact_names = " ".join(_artifact_names(params.verb))
    if destroy_plan:
        plan_artifact_helper = _DESTROY_PLAN_ARTIFACT_HELPER
        plan_setup = 'plan_dir="${ARTIFACTS_DIR:-/tmp}/binary-plan"\nmkdir -p "$plan_dir"\nplan_file="$plan_dir/destroy.plan.tfplan"'
        out_flag = ' -destroy -out="$plan_file"'
        plan_upload = "\nupload_destroy_plan_binary_artifact"
        output_path = "destroy.plan.out"
        command = "plan"
    elif plan_enabled:
        plan_artifact_helper = _PLAN_ARTIFACT_HELPER
        plan_setup = 'plan_dir="${ARTIFACTS_DIR:-/tmp}/binary-plan"\nmkdir -p "$plan_dir"\nplan_file="$plan_dir/plan.tfplan"'
        out_flag = ' -out="$plan_file"'
        plan_upload = "\nupload_plan_binary_artifact"
        output_path = "tf/plan.out"
    else:
        plan_artifact_helper = ""
        plan_setup = ""
        out_flag = ""
        plan_upload = ""
        output_path = "tf/plan.out"
    scan_block = """
set +e
tfsec . --format json --soft-fail --out "${ARTIFACTS_DIR:-/tmp}/tfsec.json"
tfsec_status=$?
set -e
if [ "$tfsec_status" -ne 0 ]; then
  echo "Error: tfsec failed with exit code ${tfsec_status}" >&2
  exit "$tfsec_status"
fi
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "${ARTIFACTS_DIR:-/tmp}/tfsec.json" || { echo "Error: tfsec produced invalid JSON" >&2; exit 11; }
set +e
tfsec . --soft-fail --no-color > "${ARTIFACTS_DIR:-/tmp}/tfsec.output" 2>&1
tfsec_output_status=$?
set -e
if [ "$tfsec_output_status" -ne 0 ]; then
  echo "Error: tfsec native output failed with exit code ${tfsec_output_status}" >&2
  exit "$tfsec_output_status"
fi
if [ -n "${INFRACOST_API_KEY:-}" ]; then
  set +e
  infracost breakdown --path . --format json --out-file "${ARTIFACTS_DIR:-/tmp}/infracost.json"
  infracost_status=$?
  set -e
  if [ "$infracost_status" -ne 0 ]; then
    echo "Error: infracost failed with exit code ${infracost_status}" >&2
    exit "$infracost_status"
  fi
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "${ARTIFACTS_DIR:-/tmp}/infracost.json" || { echo "Error: infracost produced invalid JSON" >&2; exit 12; }
  set +e
  infracost output --path "${ARTIFACTS_DIR:-/tmp}/infracost.json" --format table > "${ARTIFACTS_DIR:-/tmp}/infracost.output" 2>&1
  infracost_output_status=$?
  set -e
  if [ "$infracost_output_status" -ne 0 ]; then
    echo "Error: infracost native output failed with exit code ${infracost_output_status}" >&2
    exit "$infracost_output_status"
  fi
else
  printf '%s\\n' '{"skipped":true,"reason":"not configured"}' > "${ARTIFACTS_DIR:-/tmp}/infracost.json"
fi""" if plan_enabled else ""
    return f'''#!/usr/bin/env bash
set -euo pipefail
upload_artifacts() {{
  for name in {artifact_names}; do
    artifact="${{ARTIFACTS_DIR:-/tmp}}/$name"
    [ -f "$artifact" ] || continue
    upper=$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]' | tr -c '[:alnum:]_' '_')
    variable="ARTIFACT_PUT_URL_${{upper}}"
    url="${{!variable:-}}"
    [ -n "$url" ] || continue
    case "$name" in
      *.out) ctype="text/plain" ;;
      *.output) ctype="text/plain" ;;
      *.json) ctype="application/json" ;;
      *) ctype="application/octet-stream" ;;
    esac
    set +e
    curl -sS --fail-with-body --retry 3 -H "Content-Type: $ctype" --upload-file "$artifact" "$url"
    upload_status=$?
    set -e
    if [ "$upload_status" -ne 0 ]; then
      echo "Warning: upload failed for $name (exit $upload_status)" >&2
    fi
  done
}}
{plan_artifact_helper}
{_GIT_AUTH_SETUP}
{installers}
export PATH={directory}:$PATH
cd {workdir}
mkdir -p "${{ARTIFACTS_DIR:-/tmp}}"
mkdir -p "${{ARTIFACTS_DIR:-/tmp}}/tf"
{plan_setup}
{tool} init -no-color 2>&1 | tee "${{ARTIFACTS_DIR:-/tmp}}/init.out"
{tool} validate -no-color 2>&1 | tee "${{ARTIFACTS_DIR:-/tmp}}/validate.out"
set +e
{tool} {command}{out_flag} -no-color{detailed} {flags} 2>&1 | tee "${{ARTIFACTS_DIR:-/tmp}}/{output_path}"
status=$?
set -e
if [ {str(drift).lower()} = true ]; then
  if [ "$status" -eq 2 ]; then
    printf '%s\\n' '{{"drift":true}}' > "${{ARTIFACTS_DIR:-/tmp}}/drift.json"
  elif [ "$status" -eq 0 ]; then
    printf '%s\\n' '{{"drift":false}}' > "${{ARTIFACTS_DIR:-/tmp}}/drift.json"
  else
    exit "$status"
  fi
elif [ "$status" -ne 0 ]; then
  exit "$status"
fi{plan_upload}{scan_block}
'''


def _render_apply_like(params: ScriptParams) -> str:
    output_name = "apply.out" if params.verb == "apply" else "destroy.out"
    tool, workdir, directory = (shlex.quote(params.binary), shlex.quote(params.folder), shlex.quote(bin_dir(params.execution_target)))
    installers = "\n".join(
        render_installer(binary, version, params.execution_target, require_pinned_installer(binary, version).sha256)
        for binary, version in _installer_specs(params)
    )
    artifact_names = " ".join(_artifact_names(params.verb))
    return f'''#!/usr/bin/env bash
set -euo pipefail
upload_artifacts() {{
  for name in {artifact_names}; do
    artifact="${{ARTIFACTS_DIR:-/tmp}}/$name"
    [ -f "$artifact" ] || continue
    upper=$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]' | tr -c '[:alnum:]_' '_')
    variable="ARTIFACT_PUT_URL_${{upper}}"
    url="${{!variable:-}}"
    [ -n "$url" ] || continue
    case "$name" in
      *.out) ctype="text/plain" ;;
      *.output) ctype="text/plain" ;;
      *.json) ctype="application/json" ;;
      *) ctype="application/octet-stream" ;;
    esac
    set +e
    curl -sS --fail-with-body --retry 3 -H "Content-Type: $ctype" --upload-file "$artifact" "$url"
    upload_status=$?
    set -e
    if [ "$upload_status" -ne 0 ]; then
      echo "Warning: upload failed for $name (exit $upload_status)" >&2
    fi
  done
}}
{_APPLY_PLAN_HELPER}
{_GIT_AUTH_SETUP}
{installers}
export PATH={directory}:$PATH
cd {workdir}
mkdir -p "${{ARTIFACTS_DIR:-/tmp}}"
{tool} init -no-color 2>&1 | tee "${{ARTIFACTS_DIR:-/tmp}}/init.out"
{tool} validate -no-color 2>&1 | tee "${{ARTIFACTS_DIR:-/tmp}}/validate.out"
export PLAN_FILE="${{ARTIFACTS_DIR:-/tmp}}/binary-plan/pinned.plan.tfplan"
download_and_verify_pinned_plan
set +e
{tool} show -no-color "$PINNED_PLAN_FILE" 2>&1 | tee "${{ARTIFACTS_DIR:-/tmp}}/plan-show.out"
show_status=$?
set -e
if [ "$show_status" -ne 0 ]; then
  exit "$show_status"
fi
set +e
{tool} apply -no-color "$PINNED_PLAN_FILE" 2>&1 | tee "${{ARTIFACTS_DIR:-/tmp}}/{output_name}"
status=$?
set -e
if [ "$status" -ne 0 ]; then
  exit "$status"
fi
'''


def render(params: ScriptParams) -> str:
    """Render the self-contained per-folder runner."""
    if params.verb not in _SAFE_VERBS:
        raise ValueError(f"unsafe verb: {params.verb}")
    if params.verb in {"apply", "destroy"}:
        return _render_apply_like(params)
    return _render_plan_like(params)
