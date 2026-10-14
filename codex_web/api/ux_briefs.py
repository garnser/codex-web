"""Project-bound UX views inside the existing authenticated Memory API."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from codex_web.organizational_memory import KnowledgeCreate, KnowledgeRevise
from codex_web.services.projects import ProjectNotFoundError
from codex_web.ux_briefs import UX_BRIEF_TAG, UxBriefSelection


def build_ux_brief_router(service, projects, *, reader, writer, translate, view):
    router = APIRouter(prefix="/projects/{project_id}/ux-briefs")

    def actor_for(project_id, request, mutate=False):
        try:
            projects.get(project_id, request.state.tenant_scope)
        except ProjectNotFoundError as error:
            raise HTTPException(status_code=404, detail="Project unavailable") from error
        return writer(request) if mutate else reader(request)

    @router.get("")
    async def list_briefs(project_id: str, request: Request, include_inactive: bool = False):
        actor = actor_for(project_id, request)
        try:
            records = service.memory.list(actor=actor, project_id=project_id, include_inactive=include_inactive)
            return {"items":[view(record) for record in records if record.object_type == "ux_context_brief"],"model_calls":0}
        except Exception as error:
            raise translate(error) from error

    @router.get("/sources/{kind}/{record_id}")
    async def pin_source(project_id: str, kind: Literal["knowledge","artifact","evidence"], record_id: str, request: Request):
        actor = actor_for(project_id, request)
        try:
            return {"source":service.pin_source(kind,record_id,project_id=project_id,actor=actor).model_dump(mode="json")}
        except Exception as error:
            raise translate(error) from error

    @router.post("/preview")
    async def preview(project_id: str, payload: UxBriefSelection, request: Request):
        actor = actor_for(project_id, request)
        try:
            return service.preview(payload,project_id=project_id,actor=actor)
        except Exception as error:
            raise translate(error) from error

    @router.post("")
    async def create(project_id: str, payload: KnowledgeCreate, request: Request):
        actor = actor_for(project_id, request, True)
        try:
            return {"item":view(service.create(payload,project_id=project_id,actor=actor))}
        except Exception as error:
            raise translate(error) from error

    @router.post("/{knowledge_id}/revise")
    async def revise(project_id: str, knowledge_id: str, payload: KnowledgeRevise, request: Request):
        actor = actor_for(project_id, request, True)
        try:
            return {"item":view(service.revise(knowledge_id,payload,project_id=project_id,actor=actor))}
        except Exception as error:
            raise translate(error) from error

    return router
