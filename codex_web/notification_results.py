"""Bounded untrusted native result protocol; no outcome/authority assertions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.notification_outcomes import original_snapshot_digest

MAX_RESULT_BYTES = 32768


class NotificationResultCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    expected_native_turn_id: str = Field(min_length=1, max_length=256)


class NotificationOriginalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    original_id: str = Field(min_length=1, max_length=128)
    original_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: Literal["reconciled", "follow_up_required", "unresolved"]
    reason: str = Field(min_length=1, max_length=1000)


class NotificationBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"]
    admission_id: str = Field(pattern=r"^notification-admission-[0-9a-f]{64}$")
    results: tuple[NotificationOriginalResult, ...] = Field(min_length=2, max_length=16)

    @model_validator(mode="after")
    def unique_originals(self):
        if len({item.original_id for item in self.results}) != len(self.results):
            raise ValueError("duplicate notification result original")
        return self


def result_input_contract(originals):
    import json

    return {
        "schema_version": "1.0",
        "max_output_utf8_bytes": MAX_RESULT_BYTES,
        "instruction": (
            "Return one final JSON object only, with schema_version=1.0, admission_id "
            "equal to the Notification admission identifier above, and results for every "
            "original below. Each result has original_id, original_snapshot_sha256, "
            "disposition (reconciled, follow_up_required, or unresolved) and a bounded "
            "reason (1-1000 characters). Preserve uncertainty. These are proposals, "
            "not independent verification, ACK or business acceptance. Do not include secrets."
        ),
        "original_bindings": [
            {"original_id": json.loads(raw)["id"], "original_snapshot_sha256": original_snapshot_digest(raw)}
            for raw in originals
        ],
    }
