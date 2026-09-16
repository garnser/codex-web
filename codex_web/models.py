from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, Field


SandboxMode: TypeAlias = Literal["workspace-write", "read-only", "danger-full-access"]
ApprovalPolicy: TypeAlias = Literal["on-request", "untrusted", "never"]
ReasoningEffort: TypeAlias = Literal["", "none", "minimal", "low", "medium", "high", "xhigh"]
BotProvider: TypeAlias = Literal["slack", "telegram"]


class Project(BaseModel):
    id: str
    name: str
    path: str
    model: str | None = None
    sandbox: SandboxMode = "workspace-write"
    approval_policy: ApprovalPolicy = "on-request"


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    model: str | None = None
    sandbox: SandboxMode = "workspace-write"
    approval_policy: ApprovalPolicy = "on-request"


class TurnCreate(BaseModel):
    message: str = Field(min_length=1)
    project_id: str | None = None
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    approval_policy: ApprovalPolicy | None = None
    sandbox: SandboxMode | None = None


class ApprovalDecision(BaseModel):
    decision: str


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


class WorkItemHandoff(BaseModel):
    from_agent: str
    to_agent: str
    reason: str | None = None
    expected_action: str | None = None
    requested_at: float
    acknowledged_at: float | None = None
    status: str = "pending"
    reason_code: str | None = None
    artifact_state: str | None = None
    stage: str | None = None


class WorkItemState(BaseModel):
    ref: str
    project_id: str | None = None
    project_path: str | None = None
    title: str | None = None
    url: str | None = None
    kind: str | None = None
    priority: str | None = None
    current_owner: str | None = None
    current_stage: str = "implementation_active"
    implementation_owner: str | None = None
    validation_owner: str | None = None
    release_owner: str | None = None
    artifact_state: str = "branch"
    handoff: WorkItemHandoff | None = None
    handoff_history: list[WorkItemHandoff] = Field(default_factory=list)
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
    payload: dict[str, str | int | float | bool | None | list[str] | dict[str, str]] = Field(default_factory=dict)


class WorkItemHandoffCreate(BaseModel):
    from_agent: str = Field(min_length=1)
    to_agent: str = Field(min_length=1)
    reason: str | None = None
    expected_action: str | None = None
    current_owner: str | None = None
    current_stage: str | None = None
    next_action: str | None = None
    next_owner: str | None = None
    blocker: str | None = None
    blocking_findings: list[str] | None = None
    artifact_state: str | None = None


class WorkItemAckCreate(BaseModel):
    actor: str = Field(min_length=1)
    accepted: bool = True
    next_action: str | None = None
    current_stage: str | None = None
    blocker: str | None = None
    blocking_findings: list[str] | None = None
    next_owner: str | None = None
    artifact_state: str | None = None


class WorkItemProgressUpdate(BaseModel):
    actor: str | None = None
    current_owner: str | None = None
    current_stage: str | None = None
    next_action: str | None = None
    next_owner: str | None = None
    blocker: str | None = None
    blocking_findings: list[str] | None = None
    release_gate: bool | None = None
    note: str | None = None
    status_label: str | None = None
    artifact_state: str | None = None


class BotReplyTarget(BaseModel):
    thread_id: str
    provider: BotProvider
    external_conversation_id: str
    external_thread_id: str | None = None
    message_id: str | None = None
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
    started_at: float
    updated_at: float
    resume_attempts: int = 0
    last_resume_at: float | None = None


class QueuedTurn(BaseModel):
    id: str
    thread_id: str
    project_id: str
    message: str
    sandbox: SandboxMode | None = None
    approval_policy: ApprovalPolicy | None = None
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None
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


class BotConnection(BaseModel):
    id: str
    provider: BotProvider
    name: str
    project_id: str = "home"
    bot_token: str | None = None
    slack_app_token: str | None = None
    signing_secret: str | None = None
    webhook_secret: str | None = None
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
    slack_app_token: str | None = None
    signing_secret: str | None = None
    webhook_secret: str | None = None
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
