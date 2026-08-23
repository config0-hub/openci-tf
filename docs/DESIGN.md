# Design

`openci-tf` is a safe-path GitHub PR automation service. Authentication requires a
collaborator with write or admin permission, a pinned PR head SHA, and a non-fork
pull request. The command surface is `tf plan|drift|report` with optional
`folder` or `all` targets; bare `tf plan` selects affected folders from the
pinned PR diff. Apply and destroy are rejected both by the resolver and the
inner state machine.

The outer state machine routes the safe verbs, resolves folders, renders in-progress
placeholders, applies locks, starts a bounded-concurrency Map, and renders final
comments. Each webhook invocation derives a unique `run_id` from GitHub delivery
or comment identity so synchronize replays and distinct comments never reuse S3
execution keys. `PollDone` rejects done markers that predate engine submission.

The inner run-folder state machine performs PrepareAndSubmit, PollDone, and Collect, with one credential-expiry
retry using a fresh immutable execution id.

Step Functions caps serialized state at 262,144 bytes. The outer post-Map soft
budget stays at 261,000 bytes, leaving 1,144 bytes of hard-limit headroom. A
256-character repository name serializes the fifty-folder worst failure shape to
261,706 bytes, so the repository-name cap is 251 characters. At 251 characters
the same shape serializes to 260,951 bytes; at 252 it serializes to 261,102
bytes and violates the soft budget.

Engine submissions use the eight-field SimplePayload contract: `trigger_id`,
`s3_package_uri`, `sops_type`, `sops_path`, `commands_b64`, `done_endpoint`,
`execution_target`, and `timeout_seconds`. The package contains only SOPS-encrypted `secrets.enc.json`;
responses are bounded summaries and S3 artifact pointers. Every terminal error
path uses one recursive redaction-and-bounding policy before manifest/registry
persistence, Step Functions output, or PR rendering. Raw permitted engine output
stays behind its S3 pointer; compact diagnostics are limited by string, field,
collection-count, and nesting-depth bounds.

For `plan` and `report`, the encrypted package also carries presigned PUT URLs
for an immutable binary plan object under the existing tmp bucket's `plans/`
prefix. Keys include repository, pinned SHA, account, folder, execution id, and
attempt; user-controlled path components are slugged plus SHA-256-suffixed so
path traversal and folder/repo slug collisions cannot merge prefixes. The plan
object, checksum sidecar, and bounded metadata sidecar use the bucket's existing
SSE-KMS encryption and the `plans/` lifecycle rule expires them after two days.
The renderer reads only the small metadata sidecar to show the controlled S3
pointer, expiry, and checksum. It never fetches the binary plan through the text
artifact reader or emits a presigned/public download URL. After expiry, an
operator must create a new plan before any later human-controlled apply outside
openci-tf; openci-tf itself still cannot apply.

Private Terraform module credentials use hub SSM dotenv parameters documented in
PRIVATE_MODULE_AUTH.md.

Settings store `repo` and `account` row types under `pk`/`sk`; locks use their own
TTL-backed table. See INSTALL.md and OPERATIONS.md for the canonical `just` recipes.
