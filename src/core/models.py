"""Core data models for openci-tf."""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------

@dataclass
class Command:
    """Parsed PR comment command."""

    action: str
    folders: list[str] = field(default_factory=list)
    all_flag: bool = False
    affected_flag: bool = False
    destroy_flag: bool = False
    confirm_token: str | None = None
    pipeline: str | None = None
    pipeline_step: int | None = None

    def __post_init__(self):
        valid_actions = {"plan", "drift", "report", "apply", "destroy"}
        if self.action not in valid_actions:
            raise ValueError(f"Invalid action: {self.action!r}")
        if self.pipeline is not None and (self.folders or self.all_flag or self.affected_flag):
            raise ValueError("pipeline is mutually exclusive with folder targets")
        if self.pipeline_step is not None and self.pipeline is None:
            raise ValueError("pipeline_step requires pipeline")
        if self.pipeline_step is not None and self.pipeline_step < 1:
            raise ValueError("pipeline_step must be at least 1")

    @property
    def effective_action(self) -> str:
        if self.action == "plan" and self.destroy_flag:
            return "plan_destroy"
        return self.action

    @property
    def is_confirm(self) -> bool:
        return self.confirm_token is not None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_APPLY_GRACE_SECONDS = 15
DEFAULT_DESTROY_GRACE_SECONDS = 60
MAX_MUTATION_GRACE_SECONDS = 3600


@dataclass
class MutationVerbConfig:
    """Per-verb mutation gate and grace period for apply or destroy."""

    allow: bool = False
    grace_seconds: int = DEFAULT_APPLY_GRACE_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.grace_seconds, int) or isinstance(self.grace_seconds, bool):
            raise ValueError("grace_seconds must be an integer")
        if not 0 <= self.grace_seconds <= MAX_MUTATION_GRACE_SECONDS:
            raise ValueError(
                f"grace_seconds must be between 0 and {MAX_MUTATION_GRACE_SECONDS}"
            )


@dataclass
class FolderConfig:
    """Per-folder .openci_tf/config.yaml."""

    version: int = 1
    timeout: int = 300
    tf_runtime: str = "tofu:1.8.0"
    account_alias: str = ""
    execution_target: str = "lambda"
    extra_flags: tuple[str, ...] = ()
    ssm_env_paths: tuple[str, ...] = ()
    apply: MutationVerbConfig = field(
        default_factory=lambda: MutationVerbConfig(grace_seconds=DEFAULT_APPLY_GRACE_SECONDS)
    )
    destroy: MutationVerbConfig = field(
        default_factory=lambda: MutationVerbConfig(grace_seconds=DEFAULT_DESTROY_GRACE_SECONDS)
    )

    def __post_init__(self) -> None:
        if isinstance(self.apply, bool):
            self.apply = MutationVerbConfig(
                allow=self.apply,
                grace_seconds=DEFAULT_APPLY_GRACE_SECONDS,
            )
        elif isinstance(self.apply, dict):
            self.apply = MutationVerbConfig(**self.apply)
        if isinstance(self.destroy, bool):
            self.destroy = MutationVerbConfig(
                allow=self.destroy,
                grace_seconds=DEFAULT_DESTROY_GRACE_SECONDS,
            )
        elif isinstance(self.destroy, dict):
            self.destroy = MutationVerbConfig(**self.destroy)

    @property
    def binary(self) -> str:
        return self.tf_runtime.split(":")[0]

    @property
    def runtime_version(self) -> str:
        return self.tf_runtime.split(":")[1]

    def resolved_grace_seconds(self, action: str) -> int:
        """Return orchestration grace seconds for apply or destroy."""
        if action == "apply":
            return self.apply.grace_seconds
        if action == "destroy":
            return self.destroy.grace_seconds
        raise ValueError(f"unsupported mutation action: {action}")


@dataclass
class GlobalSettings:
    """The settings block within global config."""

    destroy_wait_seconds: int = 120
    apply_wait_seconds: int = 60
    default_timeout: int = 300
    job_timeout: int = 1800
    poll_interval: int = 30
    tf_runtime: str = "tofu:1.8.0"


@dataclass
class CostReportFilter:
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class CostReportConfig:
    filter: CostReportFilter = field(default_factory=CostReportFilter)
    group_by: list[dict[str, str]] = field(default_factory=list)


@dataclass
class GlobalConfig:
    """Repo-root .openci_tf/config.yaml."""

    version: int = 1
    settings: GlobalSettings = field(default_factory=GlobalSettings)
    pipelines: dict[str, list[str]] = field(default_factory=dict)
    reports: dict[str, dict[str, CostReportConfig]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Webhook info
# ---------------------------------------------------------------------------

@dataclass
class WebhookInfo:
    """Normalized webhook event data passed through the Step Function."""

    event_type: str  # issue_comment, pull_request, issues
    action: str  # created, opened, synchronize, etc.
    repo_name: str  # org/repo
    pr_number: int | None = None
    issue_number: int | None = None
    comment_body: str | None = None
    username: str = ""
    commit_hash: str | None = None
    trigger_id: str = ""
    pr_api_url: str | None = None
    head_repo_name: str | None = None
    base_repo_name: str | None = None
    delivery_id: str | None = None
    comment_id: int | None = None

    @property
    def target_number(self) -> int:
        """PR or issue number."""
        if self.pr_number:
            return self.pr_number
        if self.issue_number:
            return self.issue_number
        raise ValueError("No pr_number or issue_number")


# ---------------------------------------------------------------------------
# Repo settings (from DynamoDB)
# ---------------------------------------------------------------------------

@dataclass
class RepoSettings:
    """Row from openci-tf-settings DynamoDB table."""

    trigger_id: str
    repo_name: str
    git_url: str
    ssh_url: str = ""
    ssm_ssh_key: str = ""
    ssm_openci_tf_github_token: str = ""
    s3_bucket_tmp: str = ""
    remote_stateful_bucket: str = ""
    secret: str = ""
    aws_default_region: str = "us-east-1"
    engine_api_url: str = ""
    engine_webhook_secret: str = ""
    assume_role_arn: str = ""
    jwt_secret_ssm_path: str = ""
    ssm_infracost_api_key: str = ""
    upstream_urls: dict[str, str] = field(default_factory=dict)
    require_approval: bool = False
