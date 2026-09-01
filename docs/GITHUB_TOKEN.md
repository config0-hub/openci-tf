# GitHub control token

The openci-tf GitHub control credential is a **fine-grained personal access token
(PAT)** stored in SSM Parameter Store as a KMS-encrypted SecureString. Use a token
whose repository access is limited to **Only selected repositories**, selecting
only each repository registered with openci-tf. Do not use a broad classic `repo`
token for the control plane when fine-grained PATs support these endpoints.

## Exact minimum fine-grained repository permissions

Select these repository permissions for every registered repository:

| Fine-grained permission | Access | Why openci-tf needs it |
| --- | --- | --- |
| Metadata | Read | Repository metadata, PR/repository checks, and collaborator permission lookup. |
| Contents | Read and write | Pinned cloning and root-content checks; config0 add-on registration also writes a file on a throwaway probe branch. The repository must have initial content/default branch. |
| Pull requests | Read and write | PR list, metadata, and changed-files reads; config0 add-on registration also creates and closes a throwaway probe PR. |
| Issues | Read and write | Repository-wide and PR issue-comment list/create/update/delete. |

Do **not** select Administration unless GitHub changes the official endpoint
requirements. Current GitHub REST docs for "Get repository permissions for a
user" require repository Metadata read for fine-grained tokens; Administration
read is not required for openci-tf's collaborator-permission check.

Relevant GitHub REST permission references:

- Get a repository — Metadata read:
  <https://docs.github.com/rest/repos/repos#get-a-repository>
- Get and create or update repository content - Contents read/write:
  <https://docs.github.com/rest/repos/contents#get-repository-content> and
  <https://docs.github.com/rest/repos/contents#create-or-update-file-contents>
- List, get, create, and update pull requests, and list pull request files - Pull requests read/write:
  <https://docs.github.com/rest/pulls/pulls#list-pull-requests>,
  <https://docs.github.com/rest/pulls/pulls#get-a-pull-request>,
  <https://docs.github.com/rest/pulls/pulls#create-a-pull-request>,
  <https://docs.github.com/rest/pulls/pulls#update-a-pull-request>, and
  <https://docs.github.com/rest/pulls/pulls#list-pull-requests-files>
- List repository issue comments and list/create/update/delete issue comments — Issues read/write:
  <https://docs.github.com/rest/issues/comments>
- Get repository permissions for a user — Metadata read; no Administration
  permission requested:
  <https://docs.github.com/rest/collaborators/collaborators#get-repository-permissions-for-a-user>

## Credential boundaries

Keep these as three separate SSM/KMS-backed credentials:

1. **Webhook HMAC secret** — `/openci-tf/install/<project>/webhook_secret`; used
   only to verify GitHub webhook signatures.
2. **GitHub control token** — `/openci-tf/clone-token/<repo-token-name>`; used for
   GitHub repository/PR/comment APIs and pinned cloning for the registered repo.
3. **Private Terraform module token** — `/openci-tf/env/github/<owner>/<repo>`;
   injected only into encrypted `secrets.enc.json` for engine execution. This is
   separate from the control token and is not registered as
   `ssm_openci_tf_github_token`.

## Installation and registration

1. Create the fine-grained PAT in GitHub with **Only selected repositories** and
   the permission table above.
2. Store it from a file or stdin; never pass the token value as an argv argument:

   ```sh
   just install-github-control-token --repo ORG/REPO --token-file ./github-control-token.txt
   # or: pass token on stdin
   printf '%s' "$GITHUB_CONTROL_TOKEN" | just install-github-control-token --repo ORG/REPO --token-file -
   ```

3. Register the repo through the path used by the installation:

   - `just install --mode config0-addon` reads the token from the default
     `/openci-tf/clone-token/ORG-REPO-control` path. Before activating settings
     or a webhook, it creates a throwaway branch and file, opens and closes a
     throwaway PR, posts a comment, then deletes the branch. This mandatory
     mutation probe verifies the Contents, Pull requests, and Issues write
     permissions in the table above.
   - The standalone `just register-repo` command is the legacy registration
     path. It runs the bounded read-only capability verifier below and writes
     the DynamoDB row only if that verifier passes:

   ```sh
   just register-repo \
     --trigger-id my-repo \
     --repo-name ORG/REPO \
     --git-url https://github.com/ORG/REPO.git \
     --webhook-secret-ssm /openci-tf/install/openci-tf/webhook_secret \
     --github-token-ssm /openci-tf/clone-token/ORG-REPO-control \
     --github-capability-collaborator KNOWN-DIRECT-COLLABORATOR \
     --github-capability-pr-number 123 \
     --upstream-urls-json '{"terraform:1.10.5":"https://releases.hashicorp.com/terraform/1.10.5/terraform_1.10.5_linux_amd64.zip","tofu:1.10.6":"https://github.com/opentofu/opentofu/releases/download/v1.10.6/tofu_1.10.6_linux_amd64.tar.gz","tfsec:1.28.10":"https://github.com/aquasecurity/tfsec/releases/download/v1.28.10/tfsec_1.28.10_linux_amd64.tar.gz","infracost:0.10.39":"https://github.com/infracost/infracost/releases/download/v0.10.39/infracost-linux-amd64.tar.gz"}'
   ```

`upstream_urls` keys are exact pinned `binary:version` values. Include every
runtime version your repo uses (for example both `terraform:1.10.5` and
`terraform:1.12.2` when folders use both). Existing rows with a bare `terraform`
or `tofu` key are accepted only when resolving one version of that binary.

The standalone verifier uses only `GET` endpoints. Required registration checks validate
authenticated access (`/user`), repository metadata, root contents, repository PR listing
(`state=all&per_page=1`), repository-wide issue comments (`per_page=1`), and
collaborator-permission lookup. A 404 from the contents endpoint is fail-loud;
initialize the repository/default branch before registration. A 404 from
collaborator permission lookup is also fail-loud. By default the collaborator
check uses the authenticated token owner's login; if the token owner is not a
direct collaborator, pass one known direct collaborator with
`--github-capability-collaborator`. The username is validated before URL path
interpolation.

`--github-capability-pr-number` is optional because repository registration often
precedes PRs. When supplied, it additionally verifies exact PR metadata,
changed-files, and that PR's issue comments. It is not required for normal registration.
The standalone verifier never creates, updates, deletes comments, or changes
repository settings. Therefore it cannot prove the write permissions required by
the config0 add-on registration probe; use the full permission table for add-on
installs. On the standalone path, a missing Issues write permission fails loud on
the first real PR comment attempt with a bounded non-secret GitHub API error.
There is no public skip or break-glass flag for either registration check.

## Rotation

1. Create a replacement fine-grained PAT with the same selected repository and
   permissions.
2. Overwrite the same SSM SecureString path using `just install-github-control-token`.
3. Re-run `just register-repo ... --github-capability-pr-number <existing-pr>` to
   validate read capabilities before the next PR comment.
4. Revoke the old PAT in GitHub.

Token values must not appear in shell tracing, Terraform state, generated source,
logs, PR comments, or command lines. Runtime GitHub failures are intentionally
fail-loud and bounded; authorization headers are not included in user-visible
errors.
