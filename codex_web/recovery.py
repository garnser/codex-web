from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.crypto import EncryptedEnvelope, KeyManifestEntry


class RecoveryDeploymentMode(StrEnum):
    LOCAL = "local"
    SHARED = "shared"
    REPLICATED = "replicated"


class RecoveryDataClass(StrEnum):
    CANONICAL_STATE = "canonical_state"
    AUDIT = "audit"
    ARTIFACT_METADATA = "artifact_metadata"
    CONFIGURATION = "configuration"
    MEMORY = "memory"


class RecoveryObjective(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    data_class: RecoveryDataClass
    rpo_seconds: int = Field(ge=0, le=31_536_000)
    rto_seconds: int = Field(gt=0, le=31_536_000)


class RecoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default="recovery-policy-default", min_length=1)
    version: str = Field(default="1", min_length=1)
    deployment_mode: RecoveryDeploymentMode = RecoveryDeploymentMode.LOCAL
    backup_key_id: str = Field(min_length=1)
    destination_id: str = Field(default="local", min_length=1)
    backup_interval_seconds: int = Field(default=3600, ge=60)
    restore_verification_interval_seconds: int = Field(default=86400, ge=300)
    retention_count: int = Field(default=30, ge=1, le=10000)
    objectives: tuple[RecoveryObjective, ...] = (
        RecoveryObjective(
            data_class=RecoveryDataClass.CANONICAL_STATE,
            rpo_seconds=3600,
            rto_seconds=14400,
        ),
        RecoveryObjective(
            data_class=RecoveryDataClass.AUDIT,
            rpo_seconds=3600,
            rto_seconds=14400,
        ),
    )
    require_audit_integrity: bool = True
    require_key_manifest: bool = True
    allow_point_in_time_recovery: bool = False

    @model_validator(mode="after")
    def normalize(self) -> "RecoveryPolicy":
        classes = [item.data_class for item in self.objectives]
        if len(classes) != len(set(classes)):
            raise ValueError("recovery objectives must be unique by data class")
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


class BackupSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: str = "1.0"
    organization_id: str
    workspace_id: str
    state_backend: str
    state_schema_version: int
    created_at: float
    documents: dict[str, Any]
    documents_checksum: str
    key_manifest: tuple[KeyManifestEntry, ...]
    audit_root_hash: str | None = None
    audit_checkpoint_id: str | None = None


class BackupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"backup-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    policy_id: str
    policy_version: str
    policy_fingerprint: str
    deployment_mode: RecoveryDeploymentMode
    state_backend: str
    state_schema_version: int
    destination_id: str
    destination_ref: str
    envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plaintext_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_count: int = Field(ge=0)
    namespaces: tuple[str, ...]
    key_id: str
    key_version: int = Field(ge=1)
    key_manifest: tuple[KeyManifestEntry, ...]
    audit_root_hash: str | None = None
    audit_checkpoint_id: str | None = None
    point_in_time_reference: str | None = None
    created_at: float = Field(default_factory=time.time)
    expires_at: float | None = None


class RestoreVerificationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class RestoreVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"restore-verification-{uuid.uuid4().hex}")
    backup_id: str
    organization_id: str
    workspace_id: str
    status: RestoreVerificationStatus
    checksum_valid: bool
    schema_compatible: bool
    key_manifest_valid: bool
    audit_continuity_valid: bool | None = None
    document_count: int = Field(ge=0)
    restored_checksum: str | None = None
    key_missing_refs: tuple[str, ...] = ()
    key_revoked_refs: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    evidence_id: str | None = None
    duration_seconds: float = Field(ge=0.0)
    isolated: bool = True
    side_effects_enabled: bool = False
    verified_at: float = Field(default_factory=time.time)


class RecoveryHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_configured: bool
    latest_backup_id: str | None = None
    latest_backup_age_seconds: float | None = None
    latest_restore_verification_id: str | None = None
    latest_restore_verification_age_seconds: float | None = None
    latest_restore_passed: bool = False
    rpo_satisfied: bool = False
    recovery_qualified: bool = False
    blockers: tuple[str, ...] = ()


class RecoveryState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    policy: RecoveryPolicy | None = None
    backups: dict[str, BackupManifest] = Field(default_factory=dict)
    verifications: dict[str, RestoreVerification] = Field(default_factory=dict)
    backup_schedule_id: str | None = None
    verification_schedule_id: str | None = None


@runtime_checkable
class BackupDestination(Protocol):
    destination_id: str

    def put(self, backup_id: str, payload: bytes) -> str: ...
    def get(self, destination_ref: str) -> bytes: ...
    def delete(self, destination_ref: str) -> bool: ...
    def healthy(self) -> bool: ...


class BackupDestinationError(RuntimeError):
    pass
