from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec
from codex_web.data_governance import DataClassification


CRYPTO_STATE_CONTRACT = ContractSpec("crypto-key-state", "1.0", ("1.0",))


class KeyAlgorithm(StrEnum):
    AES_256_GCM = "aes-256-gcm"


class KeyPurpose(StrEnum):
    APPLICATION_DATA = "application_data"
    BACKUP = "backup"
    INTEGRATION_PAYLOAD = "integration_payload"
    MEMORY = "memory"
    DECISION = "decision"
    ARTIFACT = "artifact"


class KeyVersionStatus(StrEnum):
    ACTIVE = "active"
    DECRYPT_ONLY = "decrypt_only"
    REVOKED = "revoked"


class KeyStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class KeyScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    project_id: str | None = None
    resource_id: str | None = None


class KeyVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version: int = Field(ge=1)
    algorithm: KeyAlgorithm = KeyAlgorithm.AES_256_GCM
    backend_ref: str = Field(min_length=1)
    status: KeyVersionStatus = KeyVersionStatus.ACTIVE
    created_at: float = Field(default_factory=time.time)
    rotated_at: float | None = None
    revoked_at: float | None = None
    revoke_reason: str | None = None


class ManagedKey(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"key-{uuid.uuid4().hex}")
    scope: KeyScope
    purpose: KeyPurpose = KeyPurpose.APPLICATION_DATA
    backend_type: str = Field(min_length=1)
    status: KeyStatus = KeyStatus.ACTIVE
    current_version: int = Field(default=1, ge=1)
    versions: list[KeyVersion] = Field(min_length=1)
    created_by: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def validate_versions(self) -> "ManagedKey":
        numbers = [item.version for item in self.versions]
        if len(numbers) != len(set(numbers)):
            raise ValueError("key versions must be unique")
        if self.current_version not in numbers:
            raise ValueError("current key version is missing")
        current = next(item for item in self.versions if item.version == self.current_version)
        if self.status == KeyStatus.ACTIVE and current.status != KeyVersionStatus.ACTIVE:
            raise ValueError("active key requires an active current version")
        return self


class ManagedKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str | None = None
    resource_id: str | None = None
    purpose: KeyPurpose = KeyPurpose.APPLICATION_DATA
    backend_type: str = "local"


class EncryptionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    object_type: str = Field(min_length=1)
    object_id: str = Field(min_length=1)
    project_id: str | None = None
    resource_id: str | None = None


class EncryptedEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    format_version: str = "1.0"
    algorithm: KeyAlgorithm = KeyAlgorithm.AES_256_GCM
    key_id: str = Field(min_length=1)
    key_version: int = Field(ge=1)
    wrap_nonce_b64: str = Field(min_length=1)
    wrapped_data_key_b64: str = Field(min_length=1)
    data_nonce_b64: str = Field(min_length=1)
    ciphertext_b64: str = Field(min_length=1)
    context_digest: str = Field(min_length=64, max_length=64)
    created_at: float = Field(default_factory=time.time)


class KeyRotationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key_id: str
    previous_version: int
    current_version: int


class KeyAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"keyevent-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    key_id: str | None = None
    key_version: int | None = None
    object_type: str | None = None
    object_id: str | None = None
    reason_code: str | None = None
    occurred_at: float = Field(default_factory=time.time)


class KeyManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key_id: str
    versions: tuple[int, ...]


class KeyManifestValidation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    missing_refs: tuple[str, ...]
    revoked_refs: tuple[str, ...]


class CryptoKeyState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CRYPTO_STATE_CONTRACT.current
    keys: list[ManagedKey] = Field(default_factory=list)
    events: list[KeyAuditEvent] = Field(default_factory=list)


ENCRYPTION_REQUIRED_AT_OR_ABOVE = DataClassification.CONFIDENTIAL


def encryption_required(classification: DataClassification) -> bool:
    ranks = {
        DataClassification.PUBLIC: 0,
        DataClassification.INTERNAL: 1,
        DataClassification.CONFIDENTIAL: 2,
        DataClassification.RESTRICTED: 3,
        DataClassification.SECRET: 4,
    }
    return ranks[classification] >= ranks[ENCRYPTION_REQUIRED_AT_OR_ABOVE]
