"""Versioned processing receipts inside the existing admission, never ACKs."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def processing_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class NotificationProcessingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    original_id: str = Field(min_length=1, max_length=200)
    expected_processing_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class NotificationProcessingReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    original_id: str = Field(min_length=1)
    original_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_processing_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: Literal["reconciled", "follow_up_required"]
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    verification_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    processed_by: str = Field(min_length=1)
    processed_at: float = Field(allow_inf_nan=False)
    control_snapshot_json: str = Field(max_length=65536)
    authority_decision_json: str = Field(max_length=32768)
    follow_up_snapshot_json: str | None = Field(default=None, max_length=32768)
    # A prepared result has no external receipt. No delivery state is inferred
    # from native completion, processing, or linking a canonical Work Item.
    reply_state: Literal["prepared", "no_destination"]
