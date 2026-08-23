# GitHub webhook

Store the webhook HMAC secret in SSM, then use `just create-webhook` (which
invokes `scripts/create_webhook.sh`) with SSM paths for both the webhook secret
and the GitHub control token. The endpoint is the deploy output plus
`/webhook/<trigger-id>`. Events are restricted to PR/comment activity and webhook
signatures are verified. Raw Terraform is fallback; the `just` recipe is
canonical.

The webhook secret is separate from the GitHub control PAT and any private-module
execution token:

- webhook HMAC secret: `/openci-tf/install/<project>/webhook_secret`
- GitHub control PAT: `/openci-tf/clone-token/<repo-token-name>`
- private-module dotenv token: `/openci-tf/env/github/<owner>/<repo>`

See [docs/GITHUB_TOKEN.md](GITHUB_TOKEN.md) for the required repository-scoped
fine-grained PAT permissions and the read-only registration verifier.
