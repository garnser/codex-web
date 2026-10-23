"""Human-only preview/review; native readers use the assignment-bound broker."""
from typing import Literal
from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from codex_web.api.identity import request_actor
from codex_web.services.home_host_output import HostOutputSelection
from codex_web.services.data_governance import DataGovernanceError
from codex_web.services.identity import AuthorizationError
from codex_web.services.artifact_evidence import ArtifactEvidenceError
from codex_web.services.crypto_keys import CryptoKeyError
from codex_web.services.control_plane_broker import ControlPlaneBrokerError
from codex_web.services.definitions import DefinitionError


class HostOutputReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_home_revision: int = Field(ge=1, strict=True)
    selection: HostOutputSelection
    classification: Literal['internal', 'confidential'] = 'confidential'
    reason: str = Field(min_length=1, max_length=512)


def build_home_host_output_router(service):
    router = APIRouter(prefix='/api/home/operator/host-outputs', tags=['home-operator'])

    @router.get('')
    async def list_sources(expected_home_revision: int, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        try:
            return await run_in_threadpool(service.list_sources, actor=request_actor(request),
                expected_home_revision=expected_home_revision)
        except (DataGovernanceError, AuthorizationError, ArtifactEvidenceError, CryptoKeyError,
                ControlPlaneBrokerError, DefinitionError, ValueError) as error:
            raise HTTPException(status_code=403, detail='Current Home output list is unavailable') from error

    @router.get('/{artifact_id}/preview')
    async def preview(artifact_id: str, expected_home_revision: int, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        try:
            return await run_in_threadpool(service.preview, artifact_id, actor=request_actor(request),
                expected_home_revision=expected_home_revision)
        except (DataGovernanceError, AuthorizationError, ArtifactEvidenceError, CryptoKeyError,
                ControlPlaneBrokerError, DefinitionError, ValueError) as error:
            raise HTTPException(status_code=403, detail='Current Home output preview is unavailable') from error

    @router.post('/{artifact_id}/review')
    async def review(artifact_id: str, payload: HostOutputReviewRequest, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        try:
            return await run_in_threadpool(service.release, artifact_id, payload.selection, actor=request_actor(request),
                classification=payload.classification, reason=payload.reason,
                expected_home_revision=payload.expected_home_revision)
        except (DataGovernanceError, AuthorizationError, ArtifactEvidenceError, CryptoKeyError,
                ControlPlaneBrokerError, DefinitionError, ValueError) as error:
            raise HTTPException(status_code=403, detail='Current Home output review is unavailable') from error

    return router
