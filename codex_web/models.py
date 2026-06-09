from __future__ import annotations

from pydantic import BaseModel, Field


class Project(BaseModel):
    id: str
    name: str
    path: str
    model: str | None = None
    sandbox: str = "workspace-write"
    approval_policy: str = "on-request"


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    model: str | None = None
    sandbox: str = "workspace-write"
    approval_policy: str = "on-request"


class TurnCreate(BaseModel):
    message: str = Field(min_length=1)
    project_id: str | None = None
    model: str | None = None
    approval_policy: str | None = None
    sandbox: str | None = None


class ApprovalDecision(BaseModel):
    decision: str


class ThreadPrimaryUpdate(BaseModel):
    primary: bool = True
    project_id: str = "home"


class ThreadPrimaryChannelUpdate(BaseModel):
    project_id: str = "home"
    external_conversation_id: str | None = None
    provider: str = "slack"


class ThreadRunSettings(BaseModel):
    sandbox: str | None = None
    approval_policy: str | None = None


class BotReplyTarget(BaseModel):
    thread_id: str
    provider: str
    external_conversation_id: str
    external_thread_id: str | None = None
    message_id: str | None = None
    updated_at: float


class ActiveThreadTurn(BaseModel):
    thread_id: str
    turn_id: str | None = None
    project_id: str | None = None
    sandbox: str | None = None
    approval_policy: str | None = None
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
    sandbox: str | None = None
    approval_policy: str | None = None
    model: str | None = None
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
    provider: str
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
    provider: str = Field(min_length=1)
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
    provider: str
    external_conversation_id: str
    thread_id: str
    project_id: str = "home"
    external_name: str | None = None
    thread_name: str | None = None
    route_prefix: str | None = None
    is_master: bool = False
    is_primary_channel: bool = False
    post_in_thread: bool = False
    sandbox: str = "read-only"
    approval_policy: str = "on-request"
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
    provider: str | None = None
    external_conversation_id: str | None = None
    thread_id: str | None = None
    project_id: str = "home"
    external_name: str | None = None
    thread_name: str | None = None
    route_prefix: str | None = None
    is_master: bool = False
    is_primary_channel: bool = False
    post_in_thread: bool = False
    sandbox: str = "read-only"
    approval_policy: str = "on-request"


class BotInboundMessage(BaseModel):
    provider: str = Field(min_length=1)
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
    provider: str = "slack"
    external_conversation_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    project_id: str | None = None
    external_thread_id: str | None = None
    message_id: str | None = None
