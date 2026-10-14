from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.evaluation_quality import EvaluationQualityCreate
from codex_web.services.artifact_evidence import ArtifactEvidenceConflictError
from codex_web.services.identity import AuthorizationError


def build_evaluation_quality_router(service):
    router = APIRouter(prefix="/api/evaluations/runs/{run_id}/quality", tags=["evaluations"])

    def error(exc):
        return HTTPException(status_code=403 if isinstance(exc, AuthorizationError) else
            409 if isinstance(exc, ArtifactEvidenceConflictError) else
            404 if isinstance(exc, LookupError) else 400, detail=str(exc))

    @router.get("/options")
    async def options(run_id: str, request: Request):
        try:
            return service.options(run_id, actor=request_actor(request))
        except (ValueError, RuntimeError, LookupError) as exc:
            raise error(exc) from exc

    @router.get("")
    async def inspect(run_id: str, request: Request, baseline_evidence_id: str | None = None,
                      candidate_evidence_id: str | None = None):
        try:
            return service.inspect(run_id, actor=request_actor(request), baseline_evidence_id=baseline_evidence_id,
                                   candidate_evidence_id=candidate_evidence_id)
        except (ValueError, RuntimeError, LookupError) as exc:
            raise error(exc) from exc

    @router.post("")
    async def annotate(run_id: str, payload: EvaluationQualityCreate, request: Request):
        try:
            item = service.annotate(run_id, payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except (ValueError, RuntimeError, LookupError) as exc:
            raise error(exc) from exc

    return router
