# GitHub webhook

Store the webhook HMAC secret in SSM, then use `just create-webhook` (which
invokes `scripts/create_webhook.sh`) with SSM paths for both the webhook secret
and the GitHub control token. The endpoint is the deploy output plus
`/webhook/<trigger-id>`. Events are restricted to PR/comment activity and webhook
signatures are verified. Raw Terraform is fallback; the `just` recipe is
canonical.

Issue comments that start with the `tf` command prefix but name an unknown verb
receive one refusal comment listing the accepted verbs. No run is started. Comments
that do not use the `tf` prefix, or that use a known verb with invalid syntax,
are ignored with `200 {"message": "Event ignored"}` and no PR comment.

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
