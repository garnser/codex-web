"""Versioned bindings carried by canonical evidence, never execution authority."""

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OUTCOME_METADATA_KEY = "notification_original_outcome"
OUTCOME_REVIEW_METHOD = "notification-original-outcome/1.0"
NOTIFICATION_RESULT_SOURCE = "notification-native-result:1.0"


class NotificationOriginalOutcomeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0", "1.1"] = "1.0"
    admission_id: str = Field(min_length=1, max_length=128)
    original_id: str = Field(min_length=1, max_length=128)
    original_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: Literal["reconciled", "follow_up_required", "unresolved"]

    @model_validator(mode="after")
    def compatible_disposition(self):
        if self.disposition == "unresolved" and self.schema_version != "1.1":
            raise ValueError("unresolved proposal requires binding schema1.1")
        return self


def original_snapshot_digest(raw: str) -> str:
    # Hash the exact canonical immutable JSON retained by the admission.
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
