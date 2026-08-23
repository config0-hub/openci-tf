DEFECTS: 1 blocker / 3 major / 4 minor

# Adversarial review: console server proxy and deploy infrastructure

## Findings

### BLOCKER — The deployed browser console cannot load

**Files:** `frontend/server/app.ts:49-57,65-83`; `docs/FRONTEND_BRIEF.md:147-152`; `docs/INSTALL.md:149-152`

**Failure scenario:** Open the Terraform output Function URL in a normal browser. The initial `GET /` cannot carry a caller-supplied `Authorization` header, so the global middleware returns 401 instead of `index.html`. The login prompt that is supposed to collect the token is inside that never-loaded SPA. Even if an extension injects a bearer header into the document navigation, ordinary script, CSS, font, and favicon subrequests do not inherit that header and are also rejected. The deployed product is therefore not usable through the documented browser workflow. The INSTALL guide and frontend README acknowledge this unresolved contradiction, while the brief says the identity decision is resolved and the recipe deploys anyway.

A direct app check confirmed `GET /` and an asset without a bearer token return 401; only explicitly adding the header to each request returns the file.

**Smallest honest fix:** Resolve the contract before calling this deployable. With the selected `sessionStorage` design, serve the static login shell/assets without auth and enforce the bearer token on every `/api/*` request, then update the “every request” wording. If static assets must also be private, add a real browser bootstrap that exchanges the bearer for a secure same-origin session cookie; the current SPA prompt cannot implement pre-document bearer auth.

### MAJOR — Non-default project installs write/read the token in the wrong SSM namespace

**Files:** `Justfile:10-12,25-27,278,297`; `scripts/ssm_config.sh:14,21-22`; `infra/console/data.tf:14-16`; `docs/INSTALL.md:129-130`

**Failure scenario:** Run `OPENCI_TF_PROJECT=acme just config set-stdin console_token`, then `OPENCI_TF_PROJECT=acme just console`. Just exports `TF_VAR_project_name=acme`, so Terraform looks up `/openci-tf/install/acme/console_token`. But neither `config` nor the console recipes export `SSM_CONFIG_PROJECT`; `ssm_config.sh` therefore writes and prechecks `/openci-tf/install/openci-tf/console_token`. The precheck can pass against the default project's token and the apply then fails because the `acme` parameter is absent. `console-destroy` has the same namespace split and can be blocked during cleanup. This directly contradicts INSTALL's non-default-project claim.

A mocked invocation with only `OPENCI_TF_PROJECT=acme` confirmed `ssm_config.sh get console_token` requests `/openci-tf/install/openci-tf/console_token`.

**Smallest honest fix:** Add `export SSM_CONFIG_PROJECT := OPENCI_TF_PROJECT` beside the existing `TF_VAR_project_name` export (preserving explicit per-command overrides such as the engine namespace), and add a non-default-project recipe test.

### MAJOR — The decrypted bearer token is persisted in versioned Terraform state

**Files:** `infra/console/data.tf:11-16`; `infra/console/main.tf:79-84`; `docs/INSTALL.md:43-45,132-134`

**Failure scenario:** `with_decryption = true` materializes the plaintext SecureString as a Terraform data-source value and interpolates it into the Lambda environment. Terraform's `sensitive` marker only redacts display; it does not omit the value from state. Consequently `console/terraform.tfstate` contains the bearer token. The S3 backend is versioned, so rotating the token leaves both old and new plaintext values in retained state versions. Anyone or any automation with state-read access gains the console credential, beyond the Lambda environment exposure that the design actually requires. Provider schema inspection confirmed `aws_ssm_parameter.value` is a computed sensitive string, not an ephemeral/write-only value.

**Smallest honest fix:** Put only the exact SSM parameter name in the Lambda environment and have the function fetch/decrypt it at cold start, granting the execution role `ssm:GetParameter` on that one parameter (and narrowly scoped KMS decrypt only if a customer-managed key requires it). Remove the decrypted value from Terraform evaluation/state.

### MAJOR — `set-stdin` still places the secret in the AWS CLI process arguments

**Files:** `scripts/ssm_config.sh:29-31,42-49`; `Justfile:18-19`; `docs/INSTALL.md:43-45,118-120`

**Failure scenario:** `printf '<token>' | just config set-stdin console_token` reads stdin into a shell variable, then executes `aws ssm put-parameter ... --value "$2"`. During that AWS call, the token is present in the child process's argument vector and can be observed through process inspection by another local process. The operator-facing comments and INSTALL text imply stdin avoids command-line exposure, but it only avoids shell-history exposure. A mocked `aws` invocation printed the actual argv and showed `--value <demonstration-secret>`.

**Smallest honest fix:** Feed an AWS CLI input document through stdin (for example, JSON generated without putting the value in argv and consumed with `--cli-input-json file:///dev/stdin`) or use a mode-0600 temporary file with guaranteed cleanup. Keep only the parameter name/path in argv.

### MINOR — The console source record does not pin the provider used to apply it

**Files:** `.gitignore:9-12`; `infra/console/versions.tf:1-7`; `scripts/upload_source.sh:69-80`

**Failure scenario:** `.terraform.lock.hcl` is globally ignored, while the AWS constraint is open-ended at `>= 5.0`. The local console root currently resolved AWS provider 6.61.0, but a fresh checkout or later disaster-recovery/destroy run can resolve a different future release for the same commit. `upload_source.sh` copies only Git-tracked files, so its versioned “complete” source record also omits the generated lock despite explicitly allowing tracked lock files. A future incompatible provider can make a same-source redeploy or destroy fail or behave differently.

**Smallest honest fix:** Stop ignoring provider lock files, commit `infra/console/.terraform.lock.hcl` with all supported-platform hashes, and ensure the source-copy check asserts that the lock was staged.

### MINOR — One malformed local request terminates the development server

**File:** `frontend/server/local.ts:13-24,31-33`

**Failure scenario:** Send a raw loopback request with `Host: [` to the documented local server. `new Request("http://[/", ...)` throws `ERR_INVALID_URL`; the `createServer` callback discards the `serve()` promise with `void`, so the rejection is unhandled and Node exits. This happens before Hono's auth middleware. I reproduced the process exit. The Lambda adapter instead catches request-construction errors and returns a bounded error, so local behavior is not identical as promised.

**Smallest honest fix:** Do not construct the internal URL from an untrusted Host header (a fixed loopback origin is sufficient), and attach a rejection handler that returns a bounded 400/500 without terminating the server.

### MINOR — Immutable one-year caching is applied to unhashed public assets

**File:** `frontend/server/app.ts:70-78`

**Failure scenario:** The Vite output includes stable URLs such as `/favicon.svg` and `/fonts/barlow-condensed-400.woff2`. The server gives every non-index file `public, max-age=31536000, immutable`, not just content-hashed Vite assets. After a console upgrade changes a favicon or font at the same URL, a browser that has loaded the old object is instructed not to revalidate it for a year and keeps stale UI media.

**Smallest honest fix:** Reserve one-year immutable caching for fingerprinted build assets (for example the hashed `/assets/*` files). Give stable public filenames `no-cache` or a short revalidating policy.

### MINOR — `proxy.test.ts` does not test the proxy or deployed auth boundary

**File:** `frontend/server/proxy.test.ts:1-22`

**Failure scenario:** The test file imports only `buildProxyUrl` and `hasValidBearerToken`; it never invokes `proxyToApi`, the Hono app, or the Lambda adapter. The suite therefore remains green if the proxy forwards the browser bearer/SigV4 headers, changes method/body bytes, follows redirects, mishandles a downstream response, or if static/API middleware wiring makes the deployed browser flow unusable. This is not hypothetical as a test-quality result: all eight current tests pass while the blocker above exists.

**Smallest honest fix:** Add boundary tests with a fake `AwsClient` asserting destination origin/path/query, stripped credentials and hop headers, method/body preservation, manual redirects, and response passthrough. Add app/Lambda event tests for unauthorized static/API requests, the resolved browser bootstrap contract, binary media, and SPA cache headers.

## Attempted, not confirmed

- **Bearer timing/leakage:** The comparison hashes both values to fixed-length SHA-256 digests before `timingSafeEqual`, so token length does not reach the compare. The console bearer header is deleted before proxy signing, and no server code logs it. I found no token echo in app errors or normal logs.
- **Proxy SSRF/path escape:** `buildProxyUrl` retains the configured API origin, and WHATWG URL parsing normalizes literal and percent-encoded dot segments before route/proxy handling. The current IAM policy and API caller policy provide a second boundary. I did not find an input that changes the downstream origin or reaches an unauthorized current API route.
- **Request smuggling:** The proxy does not explicitly remove every RFC hop-by-hop header, but the Function URL's normalization was not exercised against AWS, and Node/Undici rejects at least forbidden transfer-encoding rather than forwarding it. No demonstrable production smuggling path was confirmed without making an AWS request.
- **Mock mode:** Terraform does not set `CONSOLE_MOCK_API`, and the bearer middleware runs before the mock route. I found no production mock enablement or mock auth bypass through the reviewed deploy path.
- **Least privilege and binary plans:** The role policy grants only its own log stream/events and the current run/admin route ARNs. The documented caller-policy example has `binary_plan:false`; the core API enforces that before issuing a binary-plan URL. The frontend renders the manifest's S3 pointer and expiry rather than a binary download. I found no apply/destroy proxy permission or UI operation in the traced contract.
- **Source-copy token leakage:** The upload script stages only Git-tracked Terraform/lock files and explicitly rejects tfvars/state/plan files. The console token is not in console tfvars or outputs. The long-lived leak is Terraform backend state, described above, not the source-copy upload.
- **Lambda media handling:** Hono's AWS adapter base64-encodes non-text content types, so the reviewed Buffer response path preserves binary assets. `.woff2` falls back to `application/octet-stream`; I did not confirm a same-origin browser rejection and did not count it separately.
- **Lambda package determinism:** Two consecutive `npm run package:lambda` runs produced the same SHA-256 (`dae93efc8ec13c65930a7b62102ce3c3f82964e7028df2c4b9f2409b3f1468cd`). Cross-OS byte identity was not tested.
- **API discovery uniqueness:** `one(data.aws_apigatewayv2_apis.core.ids)` fails loud if duplicate APIs share the deterministic name. No AWS query was permitted, so I could not determine whether such duplicates exist in an installation.

## Checks run

- `npm --prefix frontend test` twice: 8/8 passed each run.
- `npm --prefix frontend run typecheck`: passed.
- `terraform -chdir=infra/console validate`: passed.
- `terraform -chdir=infra/console fmt -check -recursive`: passed.
- Lambda package built twice with matching SHA-256.
- Direct Hono checks covered authorized/unauthorized index, asset, and mock API responses.
- Raw local HTTP check reproduced the malformed-Host process termination.
- No Terraform/AWS operation was run. `just --dry-run` itself could not be used in this isolated worktree because its Git-ignored `.shared-llm` imports are not installed here; recipe namespace behavior was traced directly and confirmed with mocked `aws` invocations.
