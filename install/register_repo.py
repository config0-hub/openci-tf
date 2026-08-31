# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repository registration for config0-addon installs.

Converging registration flow (safe to re-run):

  1. Comment probe: a throwaway branch and pull request prove the control
     token can push contents, open PRs, and comment; both are removed again
     even when the probe fails.
  2. Generate the webhook secret (reused when it already exists in SSM).
  3. Apply the repository settings item to the <project>-settings table.
  4. Create or reconcile the GitHub webhook; the hook id is recorded in SSM
     at /openci-tf/install/<project>/webhook_hook_id and printed.

Every failure is fatal. The probe runs before any activation write, so a
repository is never activated with a token that cannot write. A failure
after partial activation rolls the activation back (a newly created hook is
deleted; the settings row is removed or restored) before the error is
re-raised.
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

from src.domain.cmd_builder.installers import PINNED_UPSTREAM_URLS  # noqa: E402
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

    def exists(self, path: str) -> dict | list | None:
        """GET that returns None on 404 instead of failing."""
        try:
            return self.request("GET", path)
        except RegistrationError as error:
            if "HTTP 404" in str(error):
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


def reconcile_webhook(github: GitHub, args: argparse.Namespace, secret: str) -> tuple[int, bool]:
    """Create or reconcile the webhook; returns (hook_id, created_new_hook)."""
    full_url = f"{args.webhook_url.rstrip('/')}/{args.trigger_id}"
    config = {"url": full_url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}
    hooks = github.request("GET", f"/repos/{args.repo}/hooks?per_page=100")
    matches = [hook for hook in hooks if hook.get("config", {}).get("url") == full_url]
    if matches:
        hook_id = matches[0]["id"]
        github.request(
            "PATCH",
            f"/repos/{args.repo}/hooks/{hook_id}",
            {"active": True, "events": WEBHOOK_EVENTS, "config": config},
        )
        print(f"reconciled existing webhook hook_id={hook_id} for {full_url}")
        return hook_id, False
    created = github.request(
        "POST",
        f"/repos/{args.repo}/hooks",
        {"name": "web", "active": True, "events": WEBHOOK_EVENTS, "config": config},
    )
    hook_id = created["id"]
    print(f"created webhook hook_id={hook_id} for {full_url}")
    return hook_id, True


def record_hook_id(ssm, args: argparse.Namespace, hook_id: int) -> str:
    path = f"/openci-tf/install/{args.project_name}/webhook_hook_id"
    ssm.put_parameter(Name=path, Value=str(hook_id), Type="SecureString", Overwrite=True)
    print(f"recorded hook_id={hook_id} at {path}")
    return path


def comment_probe(github: GitHub, args: argparse.Namespace) -> None:
    """Prove the token can push a branch, open a PR, and comment; then clean up."""
    repo = args.repo
    branch = f"openci-tf-register-probe-{args.trigger_id}"
    ref_path = f"/repos/{repo}/git/refs/heads/{branch}"
    default_branch = github.request("GET", f"/repos/{repo}")["default_branch"]
    base_sha = github.request("GET", f"/repos/{repo}/git/ref/heads/{default_branch}")["object"]["sha"]

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
    github: GitHub, ssm, dynamodb, args: argparse.Namespace, secret: str, webhook_secret_ssm: str
) -> int:
    """Write the settings row and webhook; roll back both on a late failure.

    Runs only after the comment probe has proven the token. If the webhook
    create/reconcile or the hook-id record fails after the settings row was
    written, the partial activation is reconciled back (a newly created hook
    is deleted; the settings row is removed, or restored to its prior item)
    and the original error is re-raised.
    """
    key = {"pk": {"S": "repo"}, "sk": {"S": args.trigger_id}}
    prior_item = dynamodb.get_item(TableName=args.table, Key=key).get("Item")
    apply_repo_settings(dynamodb, args, webhook_secret_ssm)
    hook_id: int | None = None
    hook_created = False
    try:
        hook_id, hook_created = reconcile_webhook(github, args, secret)
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
        try:
            if prior_item is None:
                dynamodb.delete_item(TableName=args.table, Key=key)
                print(f"rolled back settings row for trigger_id={args.trigger_id} after failed registration", file=sys.stderr)
            else:
                dynamodb.put_item(TableName=args.table, Item=prior_item)
                print(f"restored prior settings row for trigger_id={args.trigger_id} after failed registration", file=sys.stderr)
        except Exception as cleanup_error:
            print(f"WARNING: could not restore settings row: {cleanup_error}", file=sys.stderr)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="register_repo.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", required=True, help="Repository as owner/repo")
    parser.add_argument("--trigger-id", required=True, help="Webhook trigger id registered for this repository")
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

    token = ssm.get_parameter(Name=args.github_token_ssm, WithDecryption=True)["Parameter"]["Value"].strip()
    if not token:
        raise RegistrationError(f"GitHub control token at {args.github_token_ssm} is empty")
    github = GitHub(token)

    comment_probe(github, args)

    webhook_secret_ssm = f"/openci-tf/install/{args.project_name}/webhook_secret"
    secret = get_or_create_secret(ssm, webhook_secret_ssm)
    hook_id = activate_registration(github, ssm, dynamodb, args, secret, webhook_secret_ssm)
    print(f"registered {args.repo} (trigger_id={args.trigger_id}, hook_id={hook_id})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RegistrationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
