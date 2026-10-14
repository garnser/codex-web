from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.outcome_metrics import OutcomeFeedbackCreate, OutcomeWindow
from codex_web.services.artifact_evidence import ArtifactEvidenceError, ArtifactEvidenceConflictError
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.projects import ProjectNotFoundError


def build_outcome_metrics_router(service):
    router = APIRouter(prefix="/api/metrics/outcomes/projects/{project_id}", tags=["metrics"])

    def access(request, *, mutation=False):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not {"metrics:admin", "artifact-evidence:admin"}.issubset(actor.service_scopes):
                raise AuthorizationError("joined outcome inspection requires Metrics and Evidence authority")
        else:
            IdentityService.require_admin(actor)
            if mutation:
                IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    def error(exc):
        return HTTPException(status_code=403 if isinstance(exc, AuthorizationError) else
            404 if isinstance(exc, ProjectNotFoundError) else
            409 if isinstance(exc, ArtifactEvidenceConflictError) else 400, detail=str(exc))

    @router.get("")
    async def inspect(project_id: str, request: Request, start_at: float, end_at: float, as_of: float | None = None):
        try:
            actor = access(request)
            return service.evaluate(project_id=project_id, actor=actor,
                start_at=start_at, end_at=end_at, as_of=as_of)
        except (AuthorizationError, ArtifactEvidenceError, ProjectNotFoundError, ValueError) as exc:
            raise error(exc) from exc

    @router.post("/feedback")
    async def feedback(project_id: str, payload: OutcomeFeedbackCreate, request: Request):
        try:
            actor = access(request, mutation=True)
            item = service.feedback(payload, project_id=project_id, actor=actor)
            return {"item": item.model_dump(mode="json")}
        except (AuthorizationError, ArtifactEvidenceError, ProjectNotFoundError, ValueError) as exc:
            raise error(exc) from exc

    @router.post("/publish")
    async def publish(project_id: str, payload: OutcomeWindow, request: Request):
        try:
            actor = access(request, mutation=True)
            report = service.evaluate(project_id=project_id, actor=actor, **payload.model_dump())
            return {"report": report, "observations": service.publish(report, actor=actor)}
        except (AuthorizationError, ArtifactEvidenceError, ProjectNotFoundError, ValueError) as exc:
            raise error(exc) from exc

    return router
