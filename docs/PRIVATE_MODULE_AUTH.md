# Private Terraform module authentication

Folder runs can fetch hub-owned SSM SecureString parameters that contain complete
dotenv files. Parsed variables are merged into `secrets.enc.json` before packaging.
The execution engine never reads SSM directly; only the hub prepare Lambda does.

## SSM path and dotenv shape

Store execution environment variables at absolute paths under `/openci-tf/env/`.
Each parameter value is a complete dotenv file, for example:

```dotenv
GITHUB_TOKEN=<fine-grained read-only token>
```

Never commit real token values. A raw token string without `KEY=value` form is
invalid.

Example parameter path for a private module credential:

`/openci-tf/env/github/example-org/private-module-repo`

## Folder configuration

Reference one or more hub parameters from `.openci_tf/config.yaml`:

```yaml
account_alias: staging
ssm_env_paths:
  - /openci-tf/env/github/example-org/private-module-repo
```

`ssm_env_paths` is always a YAML list, even for a single path. Scalar strings are
rejected. Duplicate paths, wildcards, and paths outside `/openci-tf/env/` are
rejected.

## Hub-only resolution and encrypted transport

`prepare-and-submit` fetches every configured path from the hub account with
`WithDecryption=True`, parses each value as dotenv text, rejects duplicate keys
across parameters, and merges the result into the existing secrets map before
SOPS encryption. Collisions with artifact, cache, upstream, or credential keys
fail loudly. Values are never logged.

Target-account roles are not granted SSM access for these parameters.

## Git authentication in the execution script

When decrypted `secrets.enc.json` provides `GITHUB_TOKEN`, the generated runner:

- rewrites `git@github.com:` to `https://github.com/` using environment-only Git
  config (`GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0`, `GIT_CONFIG_VALUE_0`)
- sets `GIT_TERMINAL_PROMPT=0`
- creates a temporary executable `GIT_ASKPASS` helper that returns
  `x-access-token` for username prompts and reads `GITHUB_TOKEN` from the
  inherited environment for password prompts

The helper path, generated script, argv, command echo, and artifacts never
contain the token value. Cleanup runs on success and failure before artifact
upload via the shared exit trap.

Runs without `GITHUB_TOKEN` are unchanged. Existing SSH module sources such as
`git::git@github.com:example-org/private-module-repo.git//vpc?ref=v1.11.1`
continue to work when the token is present.

## Rotation

Update the SecureString value with the Just recipe (no openci-tf redeploy required):

```bash
# file contains: GITHUB_TOKEN=...
just install-ssm-env /openci-tf/env/github/example-org/private-module-repo ./github.env
```

To retire a path, delete the exact parameter with the AWS CLI (`aws ssm delete-parameter --name <path>`).

## Token scope note

A classic GitHub personal access token is broader than a fine-grained repository
token. Prefer a fine-grained token limited to the private module repository with
repository contents read-only. Protect and rotate any PAT because it can outlive a single
folder or repository configuration change.
