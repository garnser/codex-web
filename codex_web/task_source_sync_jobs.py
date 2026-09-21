from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class GitLabSyncJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GitLabSyncJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        default_factory=lambda: f"gitlab-sync-{uuid.uuid4().hex}"
    )
    correlation_id: str = Field(
        default_factory=lambda: f"sync-{uuid.uuid4().hex}"
    )
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    scope_key: str = Field(min_length=1)
    status: GitLabSyncJobStatus = GitLabSyncJobStatus.QUEUED
    discovered: int = 0
    processed: int = 0
    synced: int = 0
    unique_refs: int = 0
    current_external_id: str | None = None
    cancel_requested: bool = False
    started_at: float | None = None
    completed_at: float | None = None
    updated_at: float = Field(default_factory=time.time)
    last_error: str | None = None


class GitLabSyncJobState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    jobs: list[GitLabSyncJob] = Field(default_factory=list)
