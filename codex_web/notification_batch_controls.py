"""Immutable attribution for control-plane decisions used by a batch plan."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from codex_web.definitions import DefinitionReference


class NotificationBatchControlSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    organization_id: str
    workspace_id: str
    project_id: str
    actor_id: str
    agent_profile_id: str
    agent_profile_revision: int = Field(ge=1)
    agent_profile_record_id: str
    agent_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_profile_id: str
    execution_definition_ref: DefinitionReference
    authority_definition_ref: DefinitionReference
    configuration_pins_json: tuple[str, ...] = Field(min_length=3, max_length=3)
    authority_decision_json: str = Field(max_length=16384)
    profile_access_json: str = Field(max_length=16384)
    actor_scope_json: str = Field(max_length=16384)
    project_scope_json: str = Field(max_length=16384)
    resource_proofs_json: tuple[str, ...] = Field(max_length=64)
    max_members: int = Field(ge=2, le=16)
    max_context_bytes: int = Field(ge=256, le=262144)

    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
