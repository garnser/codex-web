from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


SKILL_SECURITY_POLICY_KIND = "security.skill-policy"
SKILL_SECURITY_POLICY_SCHEMA_VERSION = "1.0"
SKILL_SECURITY_POLICY_ID = "default"


class SkillScanMode(StrEnum):
    STATIC = "static"
    SEMANTIC = "semantic"


class SkillScanStatus(StrEnum):
    NOT_SCANNED = "not_scanned"
    RUNNING = "running"
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    SCAN_ERROR = "scan_error"
    STALE = "stale"


class SkillFindingSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_RANK = {
    SkillFindingSeverity.INFO: 0,
    SkillFindingSeverity.LOW: 1,
    SkillFindingSeverity.MEDIUM: 2,
    SkillFindingSeverity.HIGH: 3,
    SkillFindingSeverity.CRITICAL: 4,
}


class SkillSecurityFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    rule_id: str = Field(min_length=1, max_length=200)
    severity: SkillFindingSeverity
    category: str = Field(default="general", min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=4000)
    path: str | None = Field(default=None, max_length=500)
    line: int | None = Field(default=None, ge=1)
    fingerprint: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def add_fingerprint(self) -> "SkillSecurityFinding":
        if self.fingerprint is None:
            value = json.dumps(
                {
                    "rule": self.rule_id,
                    "category": self.category,
                    "message": self.message,
                    "path": self.path,
                    "line": self.line,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            object.__setattr__(
                self,
                "fingerprint",
                hashlib.sha256(value.encode()).hexdigest(),
            )
        return self


class SkillScanProviderResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    scanner_version: str = Field(min_length=1, max_length=120)
    risk_score: float = Field(ge=0, le=100)
    findings: tuple[SkillSecurityFinding, ...] = Field(default=(), max_length=500)
    report_artifact_id: str | None = Field(default=None, max_length=300)


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


class SkillSecurityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scan_imported_on_ingest: bool = True
    require_scan_before_publish: bool = True
    require_scan_before_assignment: bool = True
    scan_locally_authored: bool = False
    fail_closed_on_scanner_error: bool = True
    max_risk_score: float = Field(default=49, ge=0, le=100)
    blocked_severities: tuple[SkillFindingSeverity, ...] = (
        SkillFindingSeverity.HIGH,
        SkillFindingSeverity.CRITICAL,
    )
    blocked_categories: tuple[str, ...] = ()
    allow_audited_override: bool = False
    semantic_for_untrusted_sources: bool = False
    provider_id: str = Field(default="nvidia-skillspector", min_length=1, max_length=120)

    @model_validator(mode="after")
    def normalize(self) -> "SkillSecurityPolicy":
        object.__setattr__(
            self,
            "blocked_severities",
            tuple(dict.fromkeys(self.blocked_severities)),
        )
        object.__setattr__(
            self,
            "blocked_categories",
            tuple(
                dict.fromkeys(
                    value.strip().casefold()
                    for value in self.blocked_categories
                    if value.strip()
                )
            ),
        )
        return self


class SkillSecurityPolicyUpdate(SkillSecurityPolicy):
    reason: str = Field(min_length=1, max_length=1000)


class SkillScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: SkillScanMode = SkillScanMode.STATIC


class SkillSecurityOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=1000)
    expires_at: float | None = None


class SkillSecurityBaselineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    finding_fingerprints: tuple[str, ...] = Field(default=(), max_length=500)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def normalize(self) -> "SkillSecurityBaselineRequest":
        object.__setattr__(
            self,
            "finding_fingerprints",
            tuple(
                dict.fromkeys(
                    value.strip()
                    for value in self.finding_fingerprints
                    if value.strip()
                )
            ),
        )
        return self


class SkillSecurityOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: f"skill-security-override-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    record_id: str
    actor_id: str
    reason: str
    created_at: float = Field(default_factory=time.time)
    expires_at: float | None = None


class SkillSecurityBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: f"skill-security-baseline-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    record_id: str
    finding_fingerprints: tuple[str, ...] = ()
    actor_id: str
    reason: str
    created_at: float = Field(default_factory=time.time)


class SkillSecurityAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: f"skill-security-event-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    record_id: str
    event_type: str
    actor_id: str
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    occurred_at: float = Field(default_factory=time.time)
