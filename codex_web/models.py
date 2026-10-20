from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator, model_serializer

from codex_web.resource_context import ReadOnlyResourceIds, ReadOnlyResourceContext
from codex_web.ux_briefs import UxBriefSelection, UxBriefTurnBinding
from codex_web.notification_provenance import ProviderNotificationPin

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.definitions import DefinitionReference
from codex_web.owner_routing import OwnerRoutingBinding, OwnerRoutingPin, OwnerRoutingBlocker
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


class WorkItemArtifactRelation(BaseModel):
    """Provider-verified relation between an authoritative item and an artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: Literal["merge_request"] = "merge_request"
    relation: Literal["related"] = "related"
    source_type: str = Field(min_length=1)
    source_instance: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    url: str | None = None
    head_revision: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    state: Literal["opened", "closed", "merged"]


class GitHubTaskSourceSettings(BaseModel):
    """Project-scoped account projection configuration, never role authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    kind: Literal["github"] = "github"
    schema_version: Literal["1.0"] = "1.0"
    assignee_mapping: dict[str, str] = Field(default_factory=dict, max_length=200)

    @model_validator(mode="after")
    def validate_mapping(self) -> "GitHubTaskSourceSettings":
        import re
        for role, login in self.assignee_mapping.items():
            if not role.strip() or role != role.strip() or len(role) > 200:
                raise ValueError("assignee mapping requires exact non-empty role identifiers")
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login) or "--" in login:
                raise ValueError("assignee mapping requires valid GitHub login syntax")
        if len({v.casefold() for v in self.assignee_mapping.values()}) != len(self.assignee_mapping):
            raise ValueError("GitHub accounts must map unambiguously to one operational role")
        return self


class TaskSourceProjectionState(BaseModel):
    """Durable bounded evidence for one desired provider projection (v1.0)."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    desired_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: TaskSourceIdentity
    desired_owner: str | None = None
    desired_assignee: str | None = Field(default=None, max_length=39)
    desired_stage: WorkItemStage
    status: Literal["pending", "failed", "conflict", "succeeded"] = "pending"
    reason: str | None = Field(default=None, max_length=200)
    http_status: int | None = Field(default=None, ge=100, le=599)
    attempts: int = Field(default=0, ge=0)
    actor: str | None = None
    attempted_at: float | None = None
    observed_revision: str | None = None
    observed_state: str | None = None
    protected_source_state: str | None = None
    concurrency_guarantee: Literal["read_verify_no_state_cas"] | None = None
    mapping_revision: str | None = None
    retry_scope: Literal["provider_projection_only"] = "provider_projection_only"


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
    JiraTaskSourceSettings | ServiceNowTaskSourceSettings | GitHubTaskSourceSettings,
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
        elif source_type == "github":
            if self.provider_settings is not None and self.provider_settings.kind != "github":
                raise ValueError("task-source provider_settings kind must match source_type")
        elif self.provider_settings is not None:
            raise ValueError(
                "typed provider_settings are only supported for built-in jira/servicenow/github task sources"
            )
        return self


class ProjectDeliveryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    thread_id: str | None = None
    enabled: bool = True
    interval_seconds: int = Field(default=120, ge=30, le=3600)


class ProjectDeliveryConfiguration(ProjectDeliveryUpdate):
    actor_identity_id: str = Field(min_length=1)


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
    delivery_supervision: ProjectDeliveryConfiguration | None = None


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
    read_only_resource_ids: ReadOnlyResourceIds = ()
    resource_context_work_item_ref: str | None = None
    defer_start: bool = False
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

    memory_context_selection: UxBriefSelection | None = None

    @model_serializer(mode="wrap")
    def serialize_context(self, handler):
        result = handler(self)
        if getattr(self, "resource_context", None) is None:
            result.pop("resource_context", None)
        if not getattr(self, "read_only_resource_ids", ()):
            result.pop("read_only_resource_ids", None)
            result.pop("resource_context_work_item_ref", None)
        if self.memory_context_selection is None:
            result.pop("memory_context_selection", None)
        return result


class QueueOrderRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    expected_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=1000)


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
    resource_context: ReadOnlyResourceContext | None = None
    sandbox: SandboxMode | None = None
    approval_policy: ApprovalPolicy | None = None
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    developer_instructions: str | None = None
    repository_resource_id: str | None = None
    writable_repository_resource_ids: tuple[str, ...] = ()
    read_only_repository_resource_ids: tuple[str, ...] = ()
    execution_profile_id: str | None = None
    skill_refs: tuple[DefinitionReference, ...] | None = None


    @model_serializer(mode="wrap")
    def serialize_resource_context(self, handler):
        result=handler(self)
        if self.resource_context is None:
            result.pop("resource_context", None)
        return result


class WorkItemHandoffDelivery(BaseModel):
    """Qualified recipient start evidence; queue admission is not delivery."""
    status: Literal["awaiting_dispatch", "delivered"] = "awaiting_dispatch"
    started_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    thread_id: str | None = None
    turn_id: str | None = None
    execution_id: str | None = None
    scope_verified: bool = False
    execution_profile_id: str | None = None
    repository_resource_id: str | None = None
    writable_repository_resource_ids: tuple[str, ...] = ()
    read_only_repository_resource_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def qualified_delivery(self):
        evidence = (self.started_at, self.thread_id, self.turn_id, self.execution_id)
        if self.status == "delivered" and (self.started_at is None or not all(evidence[1:])):
            raise ValueError("delivered handoff requires actual start, thread, turn and execution evidence")
        if self.status == "awaiting_dispatch" and any(value is not None for value in evidence):
            raise ValueError("awaiting dispatch cannot contain delivered start evidence")
        if self.scope_verified and (self.status != "delivered" or not self.execution_profile_id):
            raise ValueError("verified delivery scope requires delivered execution profile evidence")
        if self.scope_verified:
            if self.execution_profile_id == "repository-write":
                if self.repository_resource_id not in self.writable_repository_resource_ids:
                    raise ValueError("verified repository delivery requires its admitted writable repository")
            elif self.execution_profile_id in {"orchestration-only", "orchestration-admin"}:
                if self.repository_resource_id or self.writable_repository_resource_ids or self.read_only_repository_resource_ids:
                    raise ValueError("coordination delivery cannot grant repository scope")
            else:
                raise ValueError("unsupported delivery execution profile")
        if self.status == "awaiting_dispatch" and (
            self.execution_profile_id or self.repository_resource_id
            or self.writable_repository_resource_ids or self.read_only_repository_resource_ids
        ):
            raise ValueError("awaiting dispatch cannot contain admitted repository scope")
        return self


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
    delivery: WorkItemHandoffDelivery | None = None
    owner_routing: OwnerRoutingBinding | None = None

    @model_validator(mode='after')
    def validate_owner_routing(self):
        from codex_web.owner_routing import normalized_owner
        if self.owner_routing is not None and self.owner_routing.configured_owner != normalized_owner(self.to_agent):
            raise ValueError('owner routing binding does not match original handoff target')
        return self

    @model_serializer(mode='wrap')
    def serialize_owner_routing(self, handler):
        result = handler(self)
        if self.owner_routing is None:
            result.pop('owner_routing', None)
        return result


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
    source_projection: TaskSourceProjectionState | None = None
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
    verified_artifact_relations: list[WorkItemArtifactRelation] | None = None
    notes: list[str] = Field(default_factory=list)
    closed_at: float | None = None
    updated_at: float
    created_at: float

    owner_routing_blocker: OwnerRoutingBlocker | None = None

    @model_serializer(mode='wrap')
    def serialize_owner_routing(self, handler):
        result = handler(self)
        if self.owner_routing_blocker is None:
            result.pop('owner_routing_blocker', None)
        return result


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
    expected_handoff_requested_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    accepted: bool = True
    next_action: str | None = None
    current_stage: WorkItemStage | None = None
    blocker: str | None = None
    blocking_findings: list[str] | None = None
    next_owner: str | None = None
    artifact_state: ArtifactState | None = None


class WorkItemProgressUpdate(BaseModel):
    actor: str | None = None
    expected_handoff_requested_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
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
    "queued",
    "admitting",
    "reconciliation_required",
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


class QueuedNativeStartCorrelation(BaseModel):
    """Metadata pinned before one assignment-bound native RPC is sent."""

    resource_context: ReadOnlyResourceContext | None = None

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    admission_claim_id: str = Field(min_length=1)
    requested_execution_id: str = Field(min_length=1)
    organization_id: str
    workspace_id: str
    project_id: str
    actor_identity_id: str = Field(min_length=1)
    agent_profile_actor_id: str | None = None
    agent_profile_id: str | None = None
    agent_profile_revision: int | None = Field(default=None, ge=1)
    execution_profile_id: str | None = None
    repository_resource_id: str | None = None
    writable_repository_resource_ids: tuple[str, ...] = ()
    read_only_repository_resource_ids: tuple[str, ...] = ()
    assignment_resource_ids: tuple[str, ...]
    assignment_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    fence: int = Field(ge=1)
    thread_id: str = Field(min_length=1)
    provider_native_session_id: str = Field(min_length=1, max_length=256)
    provider_id: Literal["openai"] = "openai"
    runtime_id: Literal["codex"] = "codex"
    runtime_generation: str = Field(min_length=1, max_length=128)
    rpc_request_id: str = Field(min_length=1, max_length=128)
    prepared_at: float


    @model_serializer(mode="wrap")
    def serialize_resource_context(self, handler):
        result=handler(self)
        if self.resource_context is None:
            result.pop("resource_context", None)
        return result


class QueuedNativeStartEvidence(BaseModel):
    """Allowlisted positive JSON-RPC result fields, never caller supplied proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    runtime_generation: str = Field(min_length=1, max_length=128)
    rpc_request_id: str = Field(min_length=1, max_length=128)
    provider_native_session_id: str = Field(min_length=1, max_length=256)
    provider_native_turn_id: str = Field(min_length=1, max_length=256)
    observed_at: float


class QueuedNativeResumeCorrelation(QueuedNativeStartCorrelation):
    """One pre-turn resume request; never interchangeable with turn/start."""

    rpc_method: Literal["thread/resume"] = "thread/resume"
    runtime_version: Literal["0.160.0"]


class QueuedNativeResumeEvidence(BaseModel):
    """Reader-observed allowlisted resume disposition; no raw provider text."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    runtime_generation: str = Field(min_length=1, max_length=128)
    rpc_request_id: str = Field(min_length=1, max_length=128)
    provider_native_session_id: str = Field(min_length=1, max_length=256)
    outcome: Literal["resumed", "archived_refusal"]
    observed_at: float


class QueuedNativeResumeObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    correlation: QueuedNativeResumeCorrelation
    evidence: QueuedNativeResumeEvidence


class QueuedNativeStartReceipt(BaseModel):
    """Actual provider start response, recorded before later bookkeeping."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    admission_claim_id: str = Field(min_length=1)
    requested_execution_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    provider_native_session_id: str = Field(min_length=1)
    provider_native_turn_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    observed_at: float


class QueuedSchedulingRetirement(BaseModel):
    """Durable proof that scheduling, not the native outcome, was retired."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["prepared", "retired"] = "prepared"
    native_outcome: Literal["unknown"] = "unknown"
    queued_turn_id: str = Field(min_length=1)
    queued_snapshot_sha256: str = Field(min_length=64, max_length=64)
    work_item_ref: str = Field(min_length=1)
    source_identity: TaskSourceIdentity
    source_state: str = Field(min_length=1)
    canonical_stage: Literal["closed"] = "closed"
    canonical_terminal_outcome: WorkItemTerminalOutcome | None = None
    retired_by: str = Field(min_length=1)
    prepared_at: float
    queue_removed_at: float | None = None
    audit_recorded_at: float | None = None

    @model_validator(mode="after")
    def validate_completion(self):
        if self.state == "retired" and self.queue_removed_at is None:
            raise ValueError("retired scheduling requires queue-removal evidence")
        if self.state == "prepared" and self.queue_removed_at is not None:
            raise ValueError("prepared scheduling retirement cannot claim queue removal")
        if self.audit_recorded_at is not None and self.state != "retired":
            raise ValueError("scheduling retirement cannot be audited before completion")
        return self


class QueuedBootstrapPrestartRefusal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    phase: Literal["before_provider_thread_start"] = "before_provider_thread_start"
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1000)
    observed_at: float


class QueuedBootstrapRecovery(BaseModel):
    """A fresh bootstrap intent, separate from the original native outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1.0"] = "1.0"
    id: str = Field(min_length=1)
    state: Literal["pending", "created", "refused"] = "pending"
    queued_turn_id: str = Field(min_length=1)
    queued_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_by: str = Field(min_length=1)
    requested_at: float
    reason: str = Field(min_length=1, max_length=1000)
    bootstrap_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    destination_thread_id: str | None = Field(default=None, min_length=1)
    prestart_refusal: QueuedBootstrapPrestartRefusal | None = None

    @model_validator(mode="after")
    def validate_destination(self):
        if (self.state == "created") != (self.destination_thread_id is not None):
            raise ValueError("created bootstrap recovery requires its canonical destination")
        if (self.state == "refused") != (self.prestart_refusal is not None):
            raise ValueError("refused bootstrap recovery requires controller pre-start proof")
        return self


class QueuedBootstrapRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    expected_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_source_thread_id: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)


class QueuedOperatorAdmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_queue_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=1000)


class QueuedOperatorAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"

    claim_id: str = Field(min_length=1)
    requested_by: str = Field(min_length=1)
    requested_at: float
    reason: str = Field(min_length=1, max_length=1000)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    queue_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutionPreflightAttempt(BaseModel):
    resource_context: ReadOnlyResourceContext | None = None
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    replacement_thread_id: str | None = Field(default=None, min_length=1)
    project_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    queued_turn_snapshot: dict[str, Any] | None = None
    queued_admission_claim_id: str | None = None
    operator_admission: QueuedOperatorAdmission | None = None
    queued_start_receipt: QueuedNativeStartReceipt | None = None
    queued_start_correlation: QueuedNativeStartCorrelation | None = None
    queued_start_evidence: QueuedNativeStartEvidence | None = None
    queued_resume_correlation: QueuedNativeResumeCorrelation | None = None
    queued_resume_evidence: QueuedNativeResumeEvidence | None = None
    queued_resume_history: tuple[QueuedNativeResumeObservation, ...] = Field(default=(), max_length=8)
    notification_batch_id: str | None = None
    scheduling_retirement: QueuedSchedulingRetirement | None = None
    bootstrap_recovery: QueuedBootstrapRecovery | None = None
    bootstrap_recovery_history: tuple[QueuedBootstrapRecovery, ...] = Field(default=(), max_length=8)
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

    @model_serializer(mode="wrap")
    def serialize_recovery_fields(self, handler):
        result = handler(self)
        if getattr(self, "resource_context", None) is None:
            result.pop("resource_context", None)
        for name in ('queued_start_correlation', 'queued_start_evidence',
                     'queued_resume_correlation', 'queued_resume_evidence'):
            if getattr(self, name) is None:
                result.pop(name, None)
        if not self.queued_resume_history:
            result.pop("queued_resume_history", None)
        if self.operator_admission is None:
            result.pop("operator_admission", None)
        if self.bootstrap_recovery is None:
            result.pop("bootstrap_recovery", None)
        if not self.bootstrap_recovery_history:
            result.pop("bootstrap_recovery_history", None)
        if self.notification_batch_id is None:
            result.pop("notification_batch_id", None)
        return result


class ActiveThreadTurn(BaseModel):
    resource_context: ReadOnlyResourceContext | None = None
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

    memory_context_binding: UxBriefTurnBinding | None = None

    @model_serializer(mode="wrap")
    def serialize_context(self, handler):
        result = handler(self)
        if getattr(self, "resource_context", None) is None:
            result.pop("resource_context", None)
        if self.memory_context_binding is None:
            result.pop("memory_context_binding", None)
        return result


class ProjectCoordinationDispatch(BaseModel):
    """Server-produced references, revalidated at admission; never human origin."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Legacy proofs remain readable in retained queues, but confer no authority.
    schema_version: Literal["1.0", "1.1"] = "1.0"
    delegation_revision: int | None = Field(default=None, ge=1)
    goal_binding_id: str = Field(min_length=1, max_length=200)
    assignment_id: str = Field(min_length=1, max_length=200)
    fence: int = Field(ge=1)


class OwnerLaneRecoveryContext(BaseModel):
    """Current-work intent for one new request; never authority to replay an input."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    bot_binding_id: str = Field(min_length=1)
    failed_assignment_id: str = Field(min_length=1)
    failed_fence: int = Field(ge=0)
    teardown_observed_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    observed_process_fence: int | None = Field(default=None, ge=0)
    work_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_owner: str = Field(min_length=1)
    goal_id: str | None = None


class QueuedTurn(BaseModel):
    owner_lane_recovery: OwnerLaneRecoveryContext | None = None
    resource_context: ReadOnlyResourceContext | None = None
    canonical_ingress_id: str | None = None
    ingress_execution_profile_definition: DefinitionReference | None = None
    id: str
    thread_id: str
    project_id: str
    message: str
    execution_id: str | None = None
    work_item_ref: str | None = None
    handoff_requested_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
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

    memory_context_binding: UxBriefTurnBinding | None = None
    provider_notification_pin: ProviderNotificationPin | None = None
    project_coordination_dispatch: ProjectCoordinationDispatch | None = None
    owner_routing_pin: OwnerRoutingPin | None = None

    @model_serializer(mode="wrap")
    def serialize_context(self, handler):
        result = handler(self)
        if self.owner_lane_recovery is None:
            result.pop("owner_lane_recovery", None)
        if getattr(self, "resource_context", None) is None:
            result.pop("resource_context", None)
        if self.memory_context_binding is None:
            result.pop("memory_context_binding", None)
        if self.provider_notification_pin is None:
            result.pop("provider_notification_pin", None)
        if self.project_coordination_dispatch is None:
            result.pop("project_coordination_dispatch", None)
        if self.owner_routing_pin is None:
            result.pop("owner_routing_pin", None)
        return result


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
    _ingress_authority: dict[str, Any] | None = PrivateAttr(default=None)
    _ingress_actor_id: str | None = PrivateAttr(default=None)
    canonical_ingress_id: str | None = None
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
