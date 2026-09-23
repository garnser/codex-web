from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.work_item_execution_models import WorkItemExecutionLifecycle


SandboxMode: TypeAlias = Literal["workspace-write", "read-only", "danger-full-access"]
ApprovalPolicy: TypeAlias = Literal["on-request", "untrusted", "never"]
RepositorySelectionPolicy: TypeAlias = Literal[
    "deterministic",
    "explicit",
    "coordinated",
]
ApprovalDecisionValue: TypeAlias = Literal[
    "accept",
    "acceptForSession",
    "approved",
    "approved_for_session",
    "decline",
]
ReasoningEffort: TypeAlias = Literal["", "none", "minimal", "low", "medium", "high", "xhigh"]
BotProvider: TypeAlias = Literal["slack", "telegram", "teams"]
WorkItemStage: TypeAlias = Literal[
    "implementation_active",
    "ready_for_validation",
    "validation_running",
    "failed_with_action_owner",
    "ready_to_close",
    "closed",
]
WorkItemTerminalOutcome: TypeAlias = Literal["completed", "cancelled", "failed"]
ArtifactState: TypeAlias = Literal["branch", "merge_request", "merged_main", "tag_pipeline"]
HandoffStatus: TypeAlias = Literal["pending", "accepted", "rejected", "superseded"]


class TaskSourceIdentity(BaseModel):
    """Provider-neutral authoritative external identity/provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_type: str = Field(min_length=1)
    source_instance: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    external_url: str | None = None
    revision: str | None = None
    event_cursor: str | None = None


class JiraTaskSourceSettings(BaseModel):
    """Typed non-secret Jira adapter settings."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: Literal["jira"] = "jira"
    username: str | None = None


class ServiceNowFieldSettings(BaseModel):
    """Allowlisted ServiceNow Task field names used only by its adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    number: str = "number"
    title: str = "short_description"
    body: str = "description"
    state: str = "state"
    assignee: str = "assigned_to"
    priority: str = "priority"
    category: str = "category"
    parent: str = "parent"
    updated: str = "sys_updated_on"
    sys_id: str = "sys_id"
    comment: str = "comments"


class ServiceNowTaskSourceSettings(BaseModel):
    """Typed non-secret ServiceNow adapter settings."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: Literal["servicenow"] = "servicenow"
    table: str = Field(default="task", min_length=1)
    fields: ServiceNowFieldSettings = Field(default_factory=ServiceNowFieldSettings)
    canonical_state_values: dict[WorkItemStage, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_state_values(self) -> "ServiceNowTaskSourceSettings":
        normalized = {
            stage: str(value or "").strip()
            for stage, value in self.canonical_state_values.items()
            if str(value or "").strip()
        }
        object.__setattr__(self, "canonical_state_values", normalized)
        return self


TaskSourceProviderSettings: TypeAlias = Annotated[
    JiraTaskSourceSettings | ServiceNowTaskSourceSettings,
    Field(discriminator="kind"),
]


class TaskSourceConfiguration(BaseModel):
    """Exactly one provider-neutral authoritative source binding for a project.

    Secret material never lives here. Built-in providers may reference one
    canonical SecretReference and carry only typed, non-secret adapter settings.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_type: str = Field(min_length=1)
    source_instance: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    credential_secret_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    provider_settings: TaskSourceProviderSettings | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_builtin_provider_binding(self) -> "TaskSourceConfiguration":
        source_type = self.source_type.casefold()
        if source_type in {"jira", "servicenow"}:
            if not self.credential_secret_id:
                raise ValueError(
                    f"{source_type} task source requires credential_secret_id"
                )
            if self.provider_settings is None:
                raise ValueError(
                    f"{source_type} task source requires typed provider_settings"
                )
            if self.provider_settings.kind != source_type:
                raise ValueError(
                    "task-source provider_settings kind must match source_type"
                )
        elif self.provider_settings is not None:
            raise ValueError(
                "typed provider_settings are only supported for built-in jira/servicenow task sources"
            )
        return self


class Project(BaseModel):
    id: str
    organization_id: str = "local"
    workspace_id: str = "default"
    name: str
    path: str
    model: str | None = None
    sandbox: SandboxMode = "workspace-write"
    approval_policy: ApprovalPolicy = "on-request"
    repository_selection_policy: RepositorySelectionPolicy = "deterministic"
    authoritative_task_source: TaskSourceConfiguration | None = None


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    model: str | None = None
    sandbox: SandboxMode = "workspace-write"
    approval_policy: ApprovalPolicy = "on-request"
    repository_selection_policy: RepositorySelectionPolicy = "deterministic"
    authoritative_task_source: TaskSourceConfiguration | None = None


class ProjectRepositorySelectionUpdate(BaseModel):
    repository_selection_policy: RepositorySelectionPolicy


class TurnCreate(BaseModel):
    message: str = Field(min_length=1)
    project_id: str | None = None
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    approval_policy: ApprovalPolicy | None = None
    sandbox: SandboxMode | None = None
    repository_resource_id: str | None = None
    writable_repository_resource_ids: tuple[str, ...] = ()
    read_only_repository_resource_ids: tuple[str, ...] = ()
    execution_profile_id: str | None = None
    agent_profile_id: str | None = None
    agent_profile_revision: int | None = Field(default=None, ge=1)


class ApprovalDecision(BaseModel):
    decision: ApprovalDecisionValue


class ThreadPrimaryUpdate(BaseModel):
    primary: bool = True
    project_id: str = "home"


class ThreadPrimaryChannelUpdate(BaseModel):
    project_id: str = "home"
    external_conversation_id: str | None = None
    provider: BotProvider = "slack"


class ThreadRunSettings(BaseModel):
    sandbox: SandboxMode | None = None
    approval_policy: ApprovalPolicy | None = None
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    developer_instructions: str | None = None
    repository_resource_id: str | None = None
    read_only_repository_resource_ids: tuple[str, ...] = ()
    execution_profile_id: str | None = None


class WorkItemHandoff(BaseModel):
    from_agent: str
    to_agent: str
    reason: str | None = None
    expected_action: str | None = None
    requested_at: float
    acknowledged_at: float | None = None
    status: HandoffStatus = "pending"
    reason_code: str | None = None
    artifact_state: ArtifactState | None = None
    stage: WorkItemStage | None = None


class WorkItemState(BaseModel):
    ref: str
    organization_id: str = "local"
    workspace_id: str = "default"
    project_id: str | None = None
    goal_id: str | None = None
    decision_id: str | None = None
    originating_action_intent_id: str | None = None
    resource_ids: list[str] = Field(default_factory=list)
    project_path: str | None = None
    source_identity: TaskSourceIdentity | None = None
    title: str | None = None
    url: str | None = None
    kind: str | None = None
    priority: str | None = None
    current_owner: str | None = None
    current_stage: WorkItemStage = "implementation_active"
    terminal_outcome: WorkItemTerminalOutcome | None = None
    implementation_owner: str | None = None
    validation_owner: str | None = None
    release_owner: str | None = None
    artifact_state: ArtifactState = "branch"
    handoff: WorkItemHandoff | None = None
    handoff_history: list[WorkItemHandoff] = Field(default_factory=list)
    execution: WorkItemExecutionLifecycle = Field(default_factory=WorkItemExecutionLifecycle)
    last_meaningful_update_at: float
    last_owner_activity_at: float | None = None
    last_gitlab_event_at: float | None = None
    blocker: str | None = None
    blocking_findings: list[str] = Field(default_factory=list)
    next_action: str | None = None
    next_owner: str | None = None
    release_gate: bool = False
    status_label: str | None = None
    labels: list[str] = Field(default_factory=list)
    mr_refs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    closed_at: float | None = None
    updated_at: float
    created_at: float


class WorkItemEvent(BaseModel):
    ref: str
    event_type: str
    created_at: float
    actor: str | None = None
    source: str | None = None
    reason: str | None = None
    payload: dict[str, str | int | float | bool | None | list[str] | dict[str, str]] = Field(default_factory=dict)


class WorkItemHandoffCreate(BaseModel):
    from_agent: str = Field(min_length=1)
    to_agent: str = Field(min_length=1)
    reason: str | None = None
    expected_action: str | None = None
    current_owner: str | None = None
    current_stage: WorkItemStage | None = None
    next_action: str | None = None
    next_owner: str | None = None
    blocker: str | None = None
    blocking_findings: list[str] | None = None
    artifact_state: ArtifactState | None = None


class WorkItemAckCreate(BaseModel):
    actor: str = Field(min_length=1)
    accepted: bool = True
    next_action: str | None = None
    current_stage: WorkItemStage | None = None
    blocker: str | None = None
    blocking_findings: list[str] | None = None
    next_owner: str | None = None
    artifact_state: ArtifactState | None = None


class WorkItemProgressUpdate(BaseModel):
    actor: str | None = None
    current_owner: str | None = None
    current_stage: WorkItemStage | None = None
    next_action: str | None = None
    next_owner: str | None = None
    blocker: str | None = None
    blocking_findings: list[str] | None = None
    release_gate: bool | None = None
    note: str | None = None
    status_label: str | None = None
    artifact_state: ArtifactState | None = None


class BotReplyTarget(BaseModel):
    thread_id: str
    provider: BotProvider
    external_conversation_id: str
    external_thread_id: str | None = None
    message_id: str | None = None
    updated_at: float


ExecutionPreflightStatus: TypeAlias = Literal[
    "blocked",
    "retrying",
    "started",
    "failed",
]


class ExecutionPreflightBlocker(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False
    remediation: str | None = None
    remediation_route: str | None = None
    target_type: str | None = None
    target_id: str | None = None


class ExecutionPreflightBlockerSnapshot(BaseModel):
    blockers: tuple[ExecutionPreflightBlocker, ...]
    recorded_at: float
    attempt_number: int = Field(ge=1)


class ExecutionPreflightAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    sandbox: SandboxMode | None = None
    approval_policy: ApprovalPolicy | None = None
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    repository_resource_id: str | None = None
    writable_repository_resource_ids: tuple[str, ...] = ()
    read_only_repository_resource_ids: tuple[str, ...] = ()
    execution_profile_id: str | None = None
    agent_profile_id: str | None = None
    agent_profile_revision: int | None = Field(default=None, ge=1)
    agent_profile_actor_id: str | None = None
    source: str = "web"
    status: ExecutionPreflightStatus = "blocked"
    blockers: tuple[ExecutionPreflightBlocker, ...] = ()
    blocker_history: tuple[ExecutionPreflightBlockerSnapshot, ...] = ()
    attempt_number: int = Field(default=1, ge=1)
    retry_claim_id: str | None = None
    retry_started_at: float | None = None
    started_at: float | None = None
    last_error: str | None = None
    created_at: float
    updated_at: float


class ActiveThreadTurn(BaseModel):
    thread_id: str
    turn_id: str | None = None
    project_id: str | None = None
    sandbox: SandboxMode | None = None
    approval_policy: ApprovalPolicy | None = None
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    source: str | None = None
    reply_target: BotReplyTarget | None = None
    execution_id: str | None = None
    assignment_id: str | None = None
    execution_workspace_id: str | None = None
    worker_id: str | None = None
    fence: int | None = None
    repository_resource_id: str | None = None
    writable_repository_resource_ids: tuple[str, ...] = ()
    execution_profile_id: str | None = None
    agent_profile: AgentProfileExecutionBinding | None = None
    agent_profile_actor_id: str | None = None
    started_at: float
    updated_at: float
    resume_attempts: int = 0
    last_resume_at: float | None = None


class QueuedTurn(BaseModel):
    id: str
    thread_id: str
    project_id: str
    message: str
    execution_id: str | None = None
    work_item_ref: str | None = None
    sandbox: SandboxMode | None = None
    approval_policy: ApprovalPolicy | None = None
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    repository_resource_id: str | None = None
    writable_repository_resource_ids: tuple[str, ...] = ()
    read_only_repository_resource_ids: tuple[str, ...] = ()
    execution_profile_id: str | None = None
    agent_profile_id: str | None = None
    agent_profile_revision: int | None = Field(default=None, ge=1)
    agent_profile_actor_id: str | None = None
    source: str = "web"
    reply_target: BotReplyTarget | None = None
    attempts: int = 0
    created_at: float


class ThreadRename(BaseModel):
    name: str = Field(min_length=1)


class IndexedThread(BaseModel):
    id: str
    name: str
    cwd: str | None = None
    path: str | None = None
    updatedAt: float | None = None
    preview: str | None = None
    model: str | None = None
    project_id: str | None = None
    archived: bool = False


class BotConnection(BaseModel):
    id: str
    provider: BotProvider
    name: str
    project_id: str = "home"
    bot_token: str | None = None
    bot_token_secret_id: str | None = None
    slack_app_token: str | None = None
    slack_app_token_secret_id: str | None = None
    signing_secret: str | None = None
    signing_secret_secret_id: str | None = None
    webhook_secret: str | None = None
    webhook_secret_secret_id: str | None = None
    default_external_conversation_id: str | None = None
    default_external_name: str | None = None
    telegram_update_offset: int | None = None
    created_at: float
    updated_at: float


class BotConnectionCreate(BaseModel):
    id: str | None = None
    provider: BotProvider
    name: str = Field(min_length=1)
    project_id: str = "home"
    bot_token: str | None = None
    bot_token_secret_id: str | None = None
    slack_app_token: str | None = None
    slack_app_token_secret_id: str | None = None
    signing_secret: str | None = None
    signing_secret_secret_id: str | None = None
    webhook_secret: str | None = None
    webhook_secret_secret_id: str | None = None
    default_external_conversation_id: str | None = None
    default_external_name: str | None = None


class BotBinding(BaseModel):
    id: str
    connection_id: str | None = None
    provider: BotProvider
    external_conversation_id: str
    thread_id: str
    project_id: str = "home"
    external_name: str | None = None
    thread_name: str | None = None
    route_prefix: str | None = None
    is_master: bool = False
    is_primary_channel: bool = False
    post_in_thread: bool = False
    sandbox: SandboxMode = "read-only"
    approval_policy: ApprovalPolicy = "on-request"
    created_at: float
    updated_at: float


class BotThreadDetail(BaseModel):
    thread_id: str
    item_type: str
    title: str
    text: str
    created_at: float


class ApprovalSlackMessage(BaseModel):
    request_id: str
    connection_id: str
    channel: str
    message_ts: str
    context: str
    thread_id: str | None = None
    created_at: float


class BotBindingCreate(BaseModel):
    connection_id: str | None = None
    provider: BotProvider | None = None
    external_conversation_id: str | None = None
    thread_id: str | None = None
    project_id: str = "home"
    external_name: str | None = None
    thread_name: str | None = None
    route_prefix: str | None = None
    is_master: bool = False
    is_primary_channel: bool = False
    post_in_thread: bool = False
    sandbox: SandboxMode = "read-only"
    approval_policy: ApprovalPolicy = "on-request"


class BotInboundMessage(BaseModel):
    provider: BotProvider
    external_conversation_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    connection_id: str | None = None
    sender_id: str | None = None
    sender_name: str | None = None
    project_id: str | None = None
    external_name: str | None = None
    external_thread_id: str | None = None
    message_id: str | None = None


class BotRouteTest(BaseModel):
    provider: BotProvider = "slack"
    external_conversation_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    project_id: str | None = None
    external_thread_id: str | None = None
    message_id: str | None = None


class AgentChannelPresenceProjectSettings(BaseModel):
    agent_channels: dict[str, list[str]] = Field(default_factory=dict)


class AgentChannelPresenceSettings(BaseModel):
    projects: dict[str, AgentChannelPresenceProjectSettings] = Field(default_factory=dict)


class GitLabProjectRoutingSettings(BaseModel):
    enabled: bool = True
    channel_ids: list[str] = Field(default_factory=list)
    route_agents: list[str] = Field(default_factory=list)
    project_paths: list[str] = Field(default_factory=list)
    fallback_agents_by_kind: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "build": ["quinn"],
            "merge_request": ["quinn"],
            "pipeline": ["quinn"],
        }
    )


class GitLabRoutingSettings(BaseModel):
    enabled: bool = True
    ignored_event_kinds: list[str] = Field(default_factory=lambda: ["note", "wiki_page"])
    projects: dict[str, GitLabProjectRoutingSettings] = Field(default_factory=dict)
