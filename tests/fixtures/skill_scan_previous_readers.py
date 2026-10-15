"""Exact pre-scanner model readers, frozen from main 4ed031f260ae5b76be2f22144086e2c76a71f67e.

Only unchanged imports are shared. These readers intentionally reject new rows.
"""
from enum import StrEnum
import uuid
import time
from pydantic import BaseModel, ConfigDict, Field
from codex_web.skill_security import SkillScanMode, SkillScanStatus, SkillFindingSeverity, SkillSecurityFinding

class ExecutionSubjectKind(StrEnum):
    WORK_ITEM = "work_item"
    THREAD = "thread"
    THREAD_BOOTSTRAP = "thread_bootstrap"

class ExecutionSubject(BaseModel):
    """Canonical subject whose work is executed by a worker/workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: ExecutionSubjectKind
    ref: str = Field(min_length=1)

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.ref}"

class SkillScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"skill-scan-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    checksum: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=120)
    scanner_version: str = Field(min_length=1, max_length=120)
    mode: SkillScanMode
    status: SkillScanStatus
    risk_score: float = Field(ge=0, le=100)
    severity: SkillFindingSeverity
    findings: tuple[SkillSecurityFinding, ...] = Field(default=(), max_length=500)
    policy_result: str = Field(min_length=1, max_length=120)
    report_artifact_id: str | None = Field(default=None, max_length=300)
    scanned_at: float = Field(default_factory=time.time)
    error: str | None = Field(default=None, max_length=1000)
