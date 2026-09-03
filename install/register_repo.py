# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repository registration for config0-addon installs.

Converging registration flow (safe to re-run):

  1. Refuse a different repository while any repository registration remains.
  2. Initialize an empty repository (only when the default branch has no
     commit yet), then use a throwaway branch and pull request to prove the
     control token can push contents, open PRs, and comment; the probe
     artifacts are removed even when the probe fails.
  3. Generate the webhook secret (reused when it already exists in SSM).
  4. Apply repository and hub-account-alias settings to <project>-settings.
  5. Create or reconcile the GitHub webhook. An existing hook is found by the
     hook id recorded in SSM, then by its config.url (also matching a URL
     that differs only by doubled slashes); it is patched in place, never
     duplicated. The hook id is recorded in SSM at
     /openci-tf/install/<project>/webhook_hook_id and printed.

Every failure is fatal. The probe runs before any activation write, so a
repository is never activated with a token that cannot write. A failure
after partial activation rolls the activation back (a newly created hook is
deleted; a pre-existing hook is patched back to its prior state; the
settings rows are removed or restored) before the error is re-raised.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.domain.accounts.external_id import derive_external_id  # noqa: E402
from src.domain.cmd_builder.installers import PINNED_UPSTREAM_URLS  # noqa: E402
from src.domain.engine.artifact_limits import MAX_ACCOUNT_ALIAS_CHARS  # noqa: E402
from src.platform.aws.clone_token import validate_clone_token_path  # noqa: E402
from src.platform.git.origin import validate_clone_source  # noqa: E402

GITHUB_API = "https://api.github.com"
WEBHOOK_EVENTS = ["issue_comment", "pull_request"]

_REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TOKENISH = re.compile(r"(gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)")


class RegistrationError(RuntimeError):
    """Raised when repository registration cannot converge."""


def _redact(text: str, token: str) -> str:
    return _TOKENISH.sub("<redacted>", text.replace(token, "<redacted>"))


class GitHub:
    """Minimal stdlib GitHub API client for the registration flow."""

    def __init__(self, token: str) -> None:
        self._token = token

    def request(self, method: str, path: str, body: dict | None = None, *, ok_status: tuple[int, ...] = (200, 201)) -> dict | list | None:
        request = urllib.request.Request(
            f"{GITHUB_API}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                status = response.status
                payload = response.read().decode() or "null"
        except urllib.error.HTTPError as error:
            detail = _redact(error.read().decode(errors="replace")[:500], self._token)
            raise RegistrationError(
                f"GitHub {method} {path} failed with HTTP {error.code}: {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise RegistrationError(f"GitHub {method} {path} failed: {error.reason}") from error
        if status not in ok_status:
            raise RegistrationError(f"GitHub {method} {path} returned unexpected HTTP {status}")
        return json.loads(payload)

    def exists(self, path: str, *, missing: tuple[int, ...] = (404,)) -> dict | list | None:
        """GET that returns None on a missing-resource status instead of failing.

        GitHub answers git-data reads on a repository without any commit with
        HTTP 409 ("Git Repository is empty"); callers that probe for that
        state pass missing=(404, 409).
        """
        try:
            return self.request("GET", path)
        except RegistrationError as error:
            if any(f"HTTP {code}" in str(error) for code in missing):
                return None
            raise


def get_or_create_secret(ssm, path: str) -> str:
    try:
        return ssm.get_parameter(Name=path, WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        value = secrets.token_hex(32)
        ssm.put_parameter(Name=path, Value=value, Type="SecureString", Overwrite=False)
        print(f"generated webhook secret at {path}")
        return value


def require_repo_compatible(dynamodb, table: str, repo: str) -> None:
    """Refuse to register a second repository over a live installation."""
    response = dynamodb.query(
        TableName=table,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": "repo"}},
        ProjectionExpression="sk, repo_name",
    )
    rows = response.get("Items", [])
    if not isinstance(rows, list):
        raise RegistrationError("repository settings query Items must be a list")
    registered = sorted(
        {
            item.get("repo_name", {}).get("S", "")
            for item in rows
            if item.get("repo_name", {}).get("S")
        }
    )
    conflicts = [name for name in registered if name != repo]
    if conflicts:
        raise RegistrationError(
            f"openci-tf is already registered to {conflicts}; remove that add-on "
            f"before installing repository {repo!r}"
        )


def account_alias_item(args: argparse.Namespace, account_id: str) -> dict:
    """Build the exact low-level DynamoDB row consumed by load_account_alias."""
    return {
        "pk": {"S": "account"},
        "sk": {"S": args.account_alias},
        "account_id": {"S": account_id},
        "role_name": {"S": f"{args.project_name}-executor-readonly"},
        "poweruser_role_name": {"S": f"{args.project_name}-executor-poweruser"},
        "external_id": {"S": derive_external_id(account_id, account_id)},
        "enable_apply": {"BOOL": True},
    }


def apply_repo_settings(dynamodb, args: argparse.Namespace, webhook_secret_ssm: str) -> None:
    upstream_urls = json.loads(args.upstream_urls_json)
    if not isinstance(upstream_urls, dict) or not upstream_urls:
        raise RegistrationError("--upstream-urls-json must be a non-empty JSON object")
    for key, value in upstream_urls.items():
        if key not in PINNED_UPSTREAM_URLS:
            supported = ", ".join(sorted(PINNED_UPSTREAM_URLS))
            raise RegistrationError(
                f"upstream-urls-json key {key!r} is not a pinned binary:version key; supported keys: {supported}"
            )
        if not isinstance(value, str) or not value.startswith("https://"):
            raise RegistrationError(f"upstream-urls-json[{key!r}] must be an https URL")
    item = {
        "pk": {"S": "repo"},
        "sk": {"S": args.trigger_id},
        "repo_name": {"S": args.repo},
        "git_url": {"S": args.git_url},
        "webhook_secret_ssm": {"S": webhook_secret_ssm},
        "ssm_openci_tf_github_token": {"S": args.github_token_ssm},
        "aws_default_region": {"S": args.region},
        "upstream_urls": {"M": {key: {"S": value} for key, value in upstream_urls.items()}},
    }
    if args.infracost_api_key_ssm:
        item["ssm_infracost_api_key"] = {"S": args.infracost_api_key_ssm}
    if args.require_approval:
        item["require_approval"] = {"BOOL": True}
    dynamodb.put_item(TableName=args.table, Item=item)
    print(f"applied repo settings for {args.repo} (trigger_id={args.trigger_id}) to {args.table}")


def apply_account_alias(dynamodb, args: argparse.Namespace, account_id: str) -> None:
    dynamodb.put_item(TableName=args.table, Item=account_alias_item(args, account_id))
    print(
        f"applied hub account alias {args.account_alias} ({account_id}) to {args.table}"
    )


def _normalize_hook_url(url: object) -> str:
    """Collapse doubled path slashes so a hook written by an older release
    (which joined the webhook URL as ``//webhook``) still matches."""
    if not isinstance(url, str):
        return ""
    return re.sub(r"(?<!:)/{2,}", "/", url).rstrip("/")


def find_existing_hook(hooks: list, full_url: str, stored_hook_id: int | None) -> dict | None:
    """Pick the hook a previous registration created, if any.

    Match order: the hook id recorded in SSM, an exact config.url match, then
    a config.url that differs from full_url only by doubled slashes.
    """
    if not isinstance(hooks, list):
        raise RegistrationError("GitHub hook listing must be a list")
    if stored_hook_id is not None:
        for hook in hooks:
            if hook.get("id") == stored_hook_id:
                return hook
    for hook in hooks:
        if hook.get("config", {}).get("url") == full_url:
            return hook
    wanted = _normalize_hook_url(full_url)
    for hook in hooks:
        if _normalize_hook_url(hook.get("config", {}).get("url")) == wanted:
            return hook
    return None


def stored_hook_id(ssm, args: argparse.Namespace) -> int | None:
    """Return the hook id a previous registration recorded in SSM, if any."""
    path = f"/openci-tf/install/{args.project_name}/webhook_hook_id"
    try:
        value = ssm.get_parameter(Name=path, WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None
    if not isinstance(value, str) or not value.strip().isdigit():
        raise RegistrationError(f"recorded webhook hook id at {path} is not an integer: {value!r}")
    return int(value.strip())


def reconcile_webhook(
    github: GitHub,
    args: argparse.Namespace,
    secret: str,
    *,
    stored_hook_id: int | None = None,
) -> tuple[int, bool, dict | None]:
    """Create or reconcile the webhook.

    Returns (hook_id, created_new_hook, prior_hook_state). For a pre-existing
    hook, prior_hook_state snapshots the fields the PATCH changes (active,
    events, config) so a late registration failure can restore them; for a
    newly created hook it is None. A hook found via find_existing_hook is
    patched to the current URL, events, and secret; a second hook is never
    created for the same trigger.
    """
    full_url = f"{args.webhook_url.rstrip('/')}/{args.trigger_id}"
    config = {"url": full_url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}
    hooks = github.request("GET", f"/repos/{args.repo}/hooks?per_page=100")
    hook = find_existing_hook(hooks, full_url, stored_hook_id)
    if hook is not None:
        hook_id = hook["id"]
        prior_state = {
            "active": hook.get("active"),
            "events": hook.get("events"),
            "config": hook.get("config", {}),
        }
        github.request(
            "PATCH",
            f"/repos/{args.repo}/hooks/{hook_id}",
            {"active": True, "events": WEBHOOK_EVENTS, "config": config},
        )
        print(f"reconciled existing webhook hook_id={hook_id} for {full_url}")
        return hook_id, False, prior_state
    created = github.request(
        "POST",
        f"/repos/{args.repo}/hooks",
        {"name": "web", "active": True, "events": WEBHOOK_EVENTS, "config": config},
    )
    hook_id = created["id"]
    print(f"created webhook hook_id={hook_id} for {full_url}")
    return hook_id, True, None


def record_hook_id(ssm, args: argparse.Namespace, hook_id: int) -> str:
    path = f"/openci-tf/install/{args.project_name}/webhook_hook_id"
    ssm.put_parameter(Name=path, Value=str(hook_id), Type="SecureString", Overwrite=True)
    print(f"recorded hook_id={hook_id} at {path}")
    return path


def default_branch_sha(github: GitHub, repo: str, default_branch: str) -> str | None:
    """Return the default branch head SHA, or None when the repository has no commit.

    The repository ``size`` field is not used: it is a cached KB figure that
    stays 0 for a repository holding only the bootstrap ``.openci_tf/.gitkeep``,
    which made every re-registration repeat the bootstrap PUT and fail with
    HTTP 422 because the existing blob sha was not supplied.
    """
    ref = github.exists(f"/repos/{repo}/git/ref/heads/{default_branch}", missing=(404, 409))
    if ref is None:
        return None
    if not isinstance(ref, dict):
        raise RegistrationError("GitHub default branch ref must be an object")
    base_sha = (ref.get("object") or {}).get("sha")
    if not isinstance(base_sha, str) or not base_sha:
        raise RegistrationError("GitHub default branch ref has no commit SHA")
    return base_sha


def repository_base(github: GitHub, repo: str) -> tuple[str, str]:
    """Return the default branch and base SHA, initializing an empty repo once.

    A repository whose default branch already has a commit (including one
    whose only commit is the bootstrap ``.openci_tf/.gitkeep`` from an earlier
    registration) is left untouched.
    """
    metadata = github.request("GET", f"/repos/{repo}")
    if not isinstance(metadata, dict):
        raise RegistrationError("GitHub repository metadata must be an object")
    default_branch = metadata.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise RegistrationError("GitHub repository has no default branch name")
    base_sha = default_branch_sha(github, repo, default_branch)
    if base_sha is not None:
        return default_branch, base_sha
    if github.exists(f"/repos/{repo}/contents/.openci_tf/.gitkeep") is not None:
        raise RegistrationError(
            f"repository {repo} has .openci_tf/.gitkeep but no readable {default_branch} head; "
            "refusing to re-initialize"
        )
    created = github.request(
        "PUT",
        f"/repos/{repo}/contents/.openci_tf/.gitkeep",
        {
            "message": "Initialize repository for openci-tf registration",
            "content": base64.b64encode(b"openci-tf\n").decode(),
        },
    )
    if not isinstance(created, dict):
        raise RegistrationError("GitHub initial commit response must be an object")
    base_sha = (created.get("commit") or {}).get("sha")
    if not isinstance(base_sha, str) or not base_sha:
        raise RegistrationError("GitHub initial commit response has no commit SHA")
    print(
        f"initialized empty repository {repo} on {default_branch} at "
        ".openci_tf/.gitkeep"
    )
    return default_branch, base_sha


def comment_probe(github: GitHub, args: argparse.Namespace) -> None:
    """Prove the token can push a branch, open a PR, and comment; then clean up."""
    repo = args.repo
    branch = f"openci-tf-register-probe-{args.trigger_id}"
    ref_path = f"/repos/{repo}/git/refs/heads/{branch}"
    default_branch, base_sha = repository_base(github, repo)

    if github.exists(f"/repos/{repo}/git/ref/heads/{branch}") is not None:
        github.request("DELETE", ref_path, ok_status=(204,))
        print(f"removed leftover probe branch {branch}")
    github.request("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})

    number: int | None = None
    try:
        file_path = f".openci_tf/register-probe-{args.trigger_id}.txt"
        existing = github.exists(f"/repos/{repo}/contents/{file_path}?ref={branch}")
        content_body = {
            "message": "openci-tf registration comment probe",
            "content": base64.b64encode(f"probe {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n".encode()).decode(),
            "branch": branch,
        }
        if isinstance(existing, dict) and existing.get("sha"):
            content_body["sha"] = existing["sha"]
        github.request("PUT", f"/repos/{repo}/contents/{file_path}", content_body)

        pull = github.request(
            "POST",
            f"/repos/{repo}/pulls",
            {
                "title": "openci-tf registration probe (auto-closed)",
                "head": branch,
                "base": default_branch,
                "body": "Throwaway registration probe; closed and deleted by install/register_repo.py.",
            },
        )
        number = pull["number"]
        comment = github.request(
            "POST",
            f"/repos/{repo}/issues/{number}/comments",
            {"body": "openci-tf registration comment probe: the control token can comment."},
        )
        print(f"comment probe passed: PR #{number}, comment id {comment['id']}")
    except BaseException:
        _cleanup_probe(github, repo, ref_path, branch, number, raise_errors=False)
        raise
    _cleanup_probe(github, repo, ref_path, branch, number, raise_errors=True)


def _cleanup_probe(
    github: GitHub, repo: str, ref_path: str, branch: str, number: int | None, *, raise_errors: bool
) -> None:
    """Close the throwaway probe PR and delete its branch.

    With raise_errors=False (used while a probe error is already propagating)
    each cleanup step is attempted and failures are printed as warnings so
    the original error stays the one raised.
    """
    steps = []
    if number is not None:
        steps.append(("close probe PR", "PATCH", f"/repos/{repo}/pulls/{number}", {"state": "closed"}, (200, 201)))
    steps.append(("delete probe branch", "DELETE", ref_path, None, (204,)))
    clean = True
    for label, method, path, body, ok_status in steps:
        try:
            github.request(method, path, body, ok_status=ok_status)
        except Exception as cleanup_error:
            if raise_errors:
                raise
            clean = False
            print(f"WARNING: probe cleanup step '{label}' failed: {cleanup_error}", file=sys.stderr)
    if clean:
        pr_note = f"PR #{number} closed, " if number is not None else ""
        print(f"probe cleanup complete: {pr_note}branch {branch} deleted")


def activate_registration(
    github: GitHub,
    ssm,
    dynamodb,
    args: argparse.Namespace,
    secret: str,
    webhook_secret_ssm: str,
    account_id: str,
) -> int:
    """Write repo plus hub-alias settings and webhook as one rollback unit."""
    repo_key = {"pk": {"S": "repo"}, "sk": {"S": args.trigger_id}}
    account_key = {"pk": {"S": "account"}, "sk": {"S": args.account_alias}}
    prior_items = {
        "repo": dynamodb.get_item(TableName=args.table, Key=repo_key).get("Item"),
        "account": dynamodb.get_item(TableName=args.table, Key=account_key).get("Item"),
    }
    apply_repo_settings(dynamodb, args, webhook_secret_ssm)
    apply_account_alias(dynamodb, args, account_id)
    hook_id: int | None = None
    hook_created = False
    prior_hook_state: dict | None = None
    try:
        hook_id, hook_created, prior_hook_state = reconcile_webhook(
            github, args, secret, stored_hook_id=stored_hook_id(ssm, args)
        )
        record_hook_id(ssm, args, hook_id)
        return hook_id
    except BaseException:
        if hook_created:
            try:
                github.request("DELETE", f"/repos/{args.repo}/hooks/{hook_id}", ok_status=(204,))
                print(f"rolled back newly created webhook hook_id={hook_id} after failed registration", file=sys.stderr)
            except Exception as cleanup_error:
                print(
                    f"WARNING: could not roll back webhook hook_id={hook_id}: {cleanup_error}",
                    file=sys.stderr,
                )
        elif hook_id is not None and prior_hook_state is not None:
            try:
                github.request("PATCH", f"/repos/{args.repo}/hooks/{hook_id}", prior_hook_state)
                print(
                    f"restored pre-existing webhook hook_id={hook_id} to its prior state after failed registration",
                    file=sys.stderr,
                )
            except Exception as cleanup_error:
                print(
                    f"WARNING: could not restore pre-existing webhook hook_id={hook_id}: {cleanup_error}",
                    file=sys.stderr,
                )
        try:
            for name, key in (("repo", repo_key), ("account", account_key)):
                prior_item = prior_items[name]
                if prior_item is None:
                    dynamodb.delete_item(TableName=args.table, Key=key)
                else:
                    dynamodb.put_item(TableName=args.table, Item=prior_item)
            print("restored repository and hub-alias settings after failed registration", file=sys.stderr)
        except Exception as cleanup_error:
            print(f"WARNING: could not restore registration settings: {cleanup_error}", file=sys.stderr)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="register_repo.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", required=True, help="Repository as owner/repo")
    parser.add_argument("--trigger-id", required=True, help="Webhook trigger id registered for this repository")
    parser.add_argument(
        "--account-alias",
        required=True,
        help="Hub account alias used by folder configuration",
    )
    parser.add_argument("--git-url", default="", help="Canonical HTTPS clone URL (default: https://github.com/<repo>.git)")
    parser.add_argument("--webhook-url", required=True, help="Deploy output webhook_url; the trigger id is appended")
    parser.add_argument(
        "--github-token-ssm",
        default="",
        help="SSM path of the GitHub control token (default: /openci-tf/clone-token/<owner>-<repo>-control)",
    )
    parser.add_argument("--upstream-urls-json", required=True, help="JSON object of pinned binary:version=https:// download URLs")
    parser.add_argument("--infracost-api-key-ssm", default="", help="Optional SSM path of the Infracost API key")
    parser.add_argument("--require-approval", action="store_true", help="Require PR approval before runs")
    parser.add_argument("--region", required=True, help="AWS region of the hub install")
    parser.add_argument("--project-name", default="openci-tf", help="Install name prefix (default: openci-tf)")
    parser.add_argument("--table", default="", help="Settings table name (default: <project-name>-settings)")
    args = parser.parse_args(argv)
    if not _REPO_NAME.fullmatch(args.repo) or ".." in args.repo:
        parser.error("--repo must be exactly owner/repo")
    if (
        not args.account_alias.strip()
        or len(args.account_alias) > MAX_ACCOUNT_ALIAS_CHARS
    ):
        parser.error(
            f"--account-alias must be non-blank and at most {MAX_ACCOUNT_ALIAS_CHARS} characters"
        )
    if not args.git_url:
        args.git_url = f"https://github.com/{args.repo}.git"
    if not args.github_token_ssm:
        args.github_token_ssm = f"/openci-tf/clone-token/{args.repo.replace('/', '-')}-control"
    if not args.table:
        args.table = f"{args.project_name}-settings"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    validate_clone_token_path(args.github_token_ssm)
    validate_clone_source(args.git_url, args.repo)

    import boto3

    ssm = boto3.client("ssm", region_name=args.region)
    dynamodb = boto3.client("dynamodb", region_name=args.region)
    sts = boto3.client("sts", region_name=args.region)

    require_repo_compatible(dynamodb, args.table, args.repo)
    account_id = sts.get_caller_identity()["Account"]
    if not isinstance(account_id, str) or not re.fullmatch(r"\d{12}", account_id):
        raise RegistrationError("STS caller identity has no valid 12-digit account id")

    token = ssm.get_parameter(Name=args.github_token_ssm, WithDecryption=True)["Parameter"]["Value"].strip()
    if not token:
        raise RegistrationError(f"GitHub control token at {args.github_token_ssm} is empty")
    github = GitHub(token)

    comment_probe(github, args)

    webhook_secret_ssm = f"/openci-tf/install/{args.project_name}/webhook_secret"
    secret = get_or_create_secret(ssm, webhook_secret_ssm)
    hook_id = activate_registration(
        github,
        ssm,
        dynamodb,
        args,
        secret,
        webhook_secret_ssm,
        account_id,
    )
    print(f"registered {args.repo} (trigger_id={args.trigger_id}, hook_id={hook_id})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RegistrationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
