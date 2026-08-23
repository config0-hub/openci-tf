"""Domain policy for untrusted execution artifacts before PR rendering."""

MAX_MANIFEST_BYTES = 65_536
"""Bounded manifest JSON document size."""

MAX_RAW_ARTIFACT_BYTES = 262_144
"""256 KiB per raw text/json execution artifact."""

MAX_DONE_MARKER_BYTES = MAX_RAW_ARTIFACT_BYTES
"""Unified done-marker bound for ProbeDone reads, Collect, checksums, and API."""

STEP_FUNCTIONS_STATE_LIMIT = 262_144
"""Standard Step Functions maximum serialized state/result size."""

MAX_INNER_STATE_BYTES = 230_000
"""Inner run-folder state budget with headroom below the Step Functions 256 KiB limit."""

MAX_POLL_DONE_RESULT_BYTES = 32_768
"""ProbeDone Lambda return budget; must fit remaining inner-state headroom."""

MAX_EXTRA_FLAG_CHARS = 512
"""Maximum characters in a single repository extra_flags entry."""

MAX_EXTRA_FLAGS_COUNT = 3
"""Maximum number of repository extra_flags entries."""

MAX_EXTRA_FLAGS_SERIALIZED_BYTES = 1_024
"""Maximum serialized size of repository extra_flags in folder configuration."""

MAX_OUTER_POST_MAP_STATE_BYTES = 261_000
"""Complete outer state budget after Map ``ResultPath`` with fifty maximum outcomes.

Rebrand arithmetic: the longer ``openci-tf`` artifact prefixes add bytes to
worst-case post-map output. Keeping the original 261,000-byte soft budget
preserves 1,144 bytes below the Step Functions 262,144-byte hard limit. With
that budget, the largest accepted repository name is 251 characters: 252 chars
serializes the fifty-folder worst failure shape to 261,102 bytes, while 251
chars serializes to 260,951 bytes.
"""

MAX_OUTER_MAP_AGGREGATE_OUTCOMES_BYTES = 215_000
"""Fifty maximum child outcomes at max deployment dimensions and worst-case printable JSON escaping."""

MAX_DEPLOYMENT_NAME_PREFIX_CHARS = 42
"""Maximum supported foundation/deploy project name prefix for outer-state budgeting."""

MAX_OUTER_VALIDATE_BYTES = 209_000
"""ValidateAndResolve output must leave room for worst-case Map accumulation."""

MAX_OUTER_STATE_BYTES = MAX_OUTER_VALIDATE_BYTES
"""Backward-compatible alias for validate-stage outer budgeting."""

MAX_OUTER_FOLDER_CONFIG_SERIALIZED_BYTES = 3_584
"""Per-folder config budget so fifty maximum items fit the outer aggregate."""

MAX_OUTER_CHILD_SUMMARY_BYTES = 1_024
"""Legacy summary-size documentation; complete child output is validated after manifest commit."""

MAX_OUTER_MAP_OUTCOME_BYTES = 4_352
"""Complete outer Map item at max dimensions with run-scoped openci-tf artifact URIs."""

MAX_OUTER_CHILD_ERROR_CHARS = 256
"""Maximum code points retained when their JSON representation is also within budget."""

MAX_OUTER_CHILD_ERROR_JSON_BYTES = 260
"""Maximum compact JSON bytes for one retained error string, including quotes."""

MAX_SSM_ENV_PATH_CHARS = 256
"""Maximum SSM env path length retained in folder configuration for outer state."""

MIN_ENGINE_EXIT_CODE = -128
"""Minimum signed subprocess return code accepted from the unmodified engine."""

MAX_ENGINE_EXIT_CODE = 255
"""Maximum non-bool integer exit code accepted from the unmodified engine."""

MAX_STEP_METADATA_STRING_CHARS = 256
"""Maximum length for done-marker step metadata strings retained in SFN state."""

MAX_DONE_MARKER_ERROR_CHARS = 2_000
"""Maximum top-level done-marker error string accepted before sanitization."""

MAX_INNER_STEP_COUNT = 1
"""Unmodified engine executes exactly one command per folder run."""

MAX_UPSTREAM_URL_CHARS = 2_048
"""Maximum installer upstream URL length carried in inner state."""

MAX_GIT_URL_CHARS = 2_048
"""Maximum git clone URL length carried in inner state."""

MAX_REPO_NAME_CHARS = 251
"""Maximum repository name length carried in inner state."""

MAX_ACCOUNT_ALIAS_CHARS = 128
"""Maximum account alias length in folder configuration."""

MAX_SSM_SETTING_PATH_CHARS = 512
"""Maximum SSM parameter path length carried in inner state."""

MAX_PACKAGE_BYTES = 52_428_800
"""50 MiB execution package ZIP bound for upload, Collect, and API metadata."""

MAX_BINARY_PLAN_BYTES = 16_777_216
"""16 MiB binary plan bound."""

MAX_CHECKSUM_SIDECAR_BYTES = 128
"""Plan checksum sidecar is a single hex digest line."""

MAX_PLAN_METADATA_BYTES = 4096
"""Bounded plan metadata JSON sidecar."""

MAX_ARTIFACT_BYTES = MAX_RAW_ARTIFACT_BYTES
"""Backward-compatible alias for inline API artifact reads."""

ALLOWED_ARTIFACT_CONTENT_TYPES = frozenset(
    {"text/plain", "application/json", "application/octet-stream", "binary/octet-stream"}
)
"""The tmp-bucket contract is text logs and JSON reports; curl presigned PUTs default to octet-stream."""
