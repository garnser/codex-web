"""Directional governed-data policy; never a repository execution grant (#2008)."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

KIND = 'artifact-sharing-policy'
VERSION = '1.0'
POLICY_ID = 'artifact.sharing.default'
METHOD = 'governed-artifact-sharing.v1'
SOURCE_METHOD = 'repository-source-capture.v1'
SOURCE = 'worker-repository-source-capture'
MAX_BYTES = 256 * 1024


class ArtifactSharingRule(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    id: str = Field(pattern=r'^[a-z0-9][a-z0-9._-]{0,127}$')
    source_resource_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    target_resource_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    source_contract: Literal['repository-source-capture.v1'] = SOURCE_METHOD
    max_content_bytes: int = Field(default=MAX_BYTES, ge=1, le=MAX_BYTES, strict=True)

    @model_validator(mode='after')
    def explicit_resources(self):
        for values in (self.source_resource_ids, self.target_resource_ids):
            if len(set(values)) != len(values) or any(not v.strip() or v != v.strip() or v == '*' for v in values):
                raise ValueError('Sharing rules require distinct explicit canonical Resources')
        return self


class ArtifactSharingPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal['1.0'] = VERSION
    organization_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    rules: tuple[ArtifactSharingRule, ...] = Field(default=(), max_length=64)

    @model_validator(mode='after')
    def distinct_rules(self):
        if len({r.id for r in self.rules}) != len(self.rules):
            raise ValueError('Sharing rule IDs must be unique')
        return self


class ArtifactShareRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    method: Literal['governed-artifact-sharing.v1'] = METHOD
    request_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    expected_fence: int = Field(ge=1, strict=True)
    source_evidence_id: str = Field(min_length=1, max_length=200)
    target_work_item_ref: str = Field(min_length=1, max_length=512)
