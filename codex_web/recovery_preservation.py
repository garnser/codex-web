"""Typed requests for explicit storage preservation, never semantic repair."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

CAPTURE = 'recovery.storage-preservation.capture'
VERIFY = 'recovery.storage-preservation.verify'
ACTIONS = frozenset({CAPTURE,VERIFY})
SOURCE = 'recovery-storage-preservation.v1'
CAPACITY_CONTRACT = 'recovery-storage-capacity.v1'


class StoragePreservationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid',frozen=True)
    schema_version: Literal['1.0'] = '1.0'
    operation_id: str = Field(min_length=1,max_length=100,pattern=r'^[a-zA-Z0-9_-]+$')
    resource_id: str = Field(min_length=1,max_length=200)
    expected_policy_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    capacity_evidence_id: str = Field(min_length=1,max_length=200)
    capacity_verification_id: str = Field(min_length=1,max_length=200)
    artifact_id: str | None = Field(default=None,max_length=200)
    idempotency_key: str = Field(min_length=1,max_length=200)
    confirm_opaque_preservation: Literal[True]


class StorageDiagnosticV1(BaseModel):
    model_config = ConfigDict(extra='forbid',frozen=True)
    reference: str = Field(pattern=r"^storage-key-sha256:[0-9a-f]{64}$")
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    error_code: Literal['json_decode_error']


class StorageInventoryV1(BaseModel):
    """Authenticated capture counts, not the later live database's size."""
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    rows: int = Field(ge=0, le=1_000_000)
    payload_bytes: int = Field(ge=0, le=2*1024**3)
    max_value_bytes: int = Field(ge=0, le=32*1024**2)
    key_bytes: int = Field(ge=0)
    framed_bytes: int = Field(ge=0, le=4*1024**3)


class StoragePreservationReportV1(BaseModel):
    """Operator-only storage result; no semantic recovery/upgrade attestation."""
    model_config = ConfigDict(extra='forbid',frozen=True)
    schema_version: Literal['1.0'] = '1.0'
    inventory: StorageInventoryV1
    rows: int = Field(ge=0)
    raw_stream_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    semantic_status: Literal['invalid','unknown']
    invalid_json_rows: int = Field(ge=0)
    diagnostics: tuple[StorageDiagnosticV1,...] = Field(max_length=100)
    diagnostics_bounded: Literal[True]
    diagnostics_truncated: bool
    isolated_restore_verified: bool
    integrity_status: Literal['verified']
    semantic_recovery_qualified: Literal[False]
    source_snapshot_rows_preserved: Literal[True]
    source_crypto_audit_appends: Literal[True]
    reserved_peak_bytes: int = Field(ge=0)
    memory_enforcement: Literal['cooperative_estimate_not_job_cgroup']
