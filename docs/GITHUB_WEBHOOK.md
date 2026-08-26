# GitHub webhook

Store the webhook HMAC secret in SSM, then use `just create-webhook` (which
invokes `scripts/create_webhook.sh`) with SSM paths for both the webhook secret
and the GitHub control token. The endpoint is the deploy output plus
`/webhook/<trigger-id>`. Events are restricted to PR/comment activity and webhook
signatures are verified. Raw Terraform is fallback; the `just` recipe is
canonical.

## Command set

Only these issue comments start work on an open, non-fork pull request from a
collaborator with write or admin permission:

- `tf plan <folder-or-csv>` and `tf plan --destroy <folder-or-csv>`
- `tf plan pipeline <name>` and `tf plan --destroy pipeline <name>`
- `tf drift pipeline <name>`
- `tf report` (all folders; folder targets are rejected)
- `tf apply <folder-or-csv>`, `tf apply pipeline <name> [step <n>]`,
  `tf destroy <folder-or-csv>`, and their `confirm <token>` forms (see
  [docs/APPLY.md](APPLY.md))

Bare `tf plan`, `tf plan all`, bare or folder-targeted `tf drift`, and
`tf report <folder>` are rejected. PR `opened` and `synchronize` events start
nothing and return `200 {"reason": "pull_request_event"}`. Comments that do not
start with `tf` are ignored with no PR comment.

## Durable audit comment

Every `tf` comment from an authorized user is recorded in one bot comment per
pull request before anything else happens. The comment carries the marker
`comment_object_id: <repo>:::pr-<n>::commands-run`, a usage line, and a table
of `| Time | Command | Status |` rows. Status is `accepted` or `not supported`.
Each row ends with a hidden `<!-- d:<github-delivery-id> -->` so a redelivered
webhook does not append a second row. Command cells are truncated at 200
characters with a `[truncated sha256:<prefix>]` suffix; the table keeps at most
200 rows and the body stays under 60,000 characters by dropping the oldest rows.
Confirm tokens are written as `confirm <redacted>`.

Writes to the comment are serialized per PR with a 60 second DynamoDB lock in
the locks table (`audit_lock` in `src/platform/aws`); contention retries for
about five seconds. If the `accepted` row cannot be written, the webhook returns
`502` and starts no run. `not supported` rows are best effort.

## Rejected commands and closed or unreadable pull requests

A `tf` comment with invalid syntax gets a `not supported` row and a transient
help comment listing the accepted forms. After 10 seconds the help comment and
the user's comment are deleted; the response is `200 {"reason": "invalid_command"}`.

A `tf` comment on a closed or merged pull request, on a pull request whose state
is missing, or on one GitHub answers with `403` or `404` gets a `not supported`
row, a short "ignored" comment, and the user's comment is deleted. The response
is `200 {"reason": "pull_request_not_open"}`. Other GitHub or SSM errors while
reading the pull request return `502`.

## Command comment cleanup

For read-lane commands the user's comment is deleted right after the audit row
exists. For `apply` and `destroy` the request, intent, and confirmation comments
stay on the PR until the terminal apply/destroy comment is posted; the render
handler then deletes those exact comment ids and sweeps only bot-authored
comments that still contain the spent `confirm <token>`. Human comments are
never deleted by content match. Status comments include a command context block
naming the triggering command with the token redacted.

The webhook secret is separate from the GitHub control PAT and any private-module
execution token:

- webhook HMAC secret: `/openci-tf/install/<project>/webhook_secret`
- GitHub control PAT: `/openci-tf/clone-token/<repo-token-name>`
- private-module dotenv token: `/openci-tf/env/github/<owner>/<repo>`

See [docs/GITHUB_TOKEN.md](GITHUB_TOKEN.md) for the required repository-scoped
fine-grained PAT permissions and the read-only registration verifier.

## Private repositories only

openci-tf is meant for private repositories. On a public repository, anyone can
comment on a pull request. The webhook handler still enforces several checks
before it starts a run:

- Webhook signatures are verified (`verify_signature` in
  `src/services/webhook/validate.py`; rejected with `401` when invalid).
- Pull requests from forks are refused (`403` when the head repo differs from
  the base repo).
- Commands from users without sufficient collaborator permission are refused.
  The handler calls GitHub's collaborator permission API, then
  `can_trigger` in `src/domain/authorization.py`, which allows only
  `write` or `admin` permission levels.

Those gates limit who can trigger work, but they do not stop drive-by comments
on a public repo. Like Atlantis, openci-tf should not be pointed at a public
repository.
