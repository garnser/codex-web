"""Fixed observation protocol; publication procedures and authority remain data."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from codex_web.definitions import DefinitionReference

METHOD = 'workspace-initial-publication-input.v1'


class PublicationInputRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    method: Literal['workspace-initial-publication-input.v1']
    definition: DefinitionReference
    repository_resource_id: str = Field(pattern=r'^[A-Za-z0-9._:-]{1,200}$')
    expected_assignment_id: str = Field(min_length=1, max_length=200)
    expected_fence: StrictInt = Field(ge=1)
    expected_head_sha: str = Field(pattern=r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
    request_id: str = Field(pattern=r'^[0-9a-f]{32}$')


class PublicationRepositoryFact(BaseModel):
    """Provider-owned bounded same-repository MR/branch observation, not consent."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    repository: str = Field(min_length=1, max_length=500)
    number: StrictInt = Field(ge=1)
    branch: str = Field(min_length=1, max_length=255)
    head_revision: str = Field(pattern=r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
    observed_at: float = Field(gt=0)


def public_contract():
    return {'method':METHOD, 'schema':PublicationInputRequest.model_json_schema(),
        'effect':'Governed current publication-input Artifact and INFO Evidence; no publication, QA or merge consent',
        'response_loss':'GET /api/verifications?method='+METHOD+'&source=<original source>; never new request IDs or origin',
        'source_identity':{'fields':['assignment_id','fence','method','request_id'],
            'values':'Actual authenticated assignment_id/fence, fixed method and original request_id',
            'encoding':'UTF-8 JSON, sort_keys=true, separators=(comma,colon), allow_nan=false; source=sha256:<lowercase SHA256 hex>',
            'response_not_required':True},
        'inputs':'Exact published typed five/six-field contract; facts derived from current owned MR, WRITE workspace and provider',
        'origin':'expected_assignment_id/expected_fence assert actual authenticated origin; cannot select or authorize another assignment'}
