"""Read-only extraction and explicit inactive-draft conversion; no execution."""
import asyncio
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from codex_web.api.identity import request_actor
from codex_web.api.skills import _error
from codex_web.services.procedure_execution import ProcedureExecutionError
from codex_web.services.organizational_memory import KnowledgeAuthorizationError


class OutcomeDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str | None = Field(default=None, max_length=100)
    incident_id: str | None = Field(default=None, max_length=200)
    skill_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]*$", max_length=160)


def build_procedure_outcomes_router(service):
    router = APIRouter(tags=["skills"])

    @router.get("/api/execution-workers/assignments/{assignment_id}/procedure/outcome")
    async def inspect(assignment_id: str, request: Request, incident_id: str | None = None, run_id: str | None = None):
        actor = request_actor(request)
        try:
            return await asyncio.to_thread(service.inspect, assignment_id,
                actor=actor, incident_id=incident_id, run_id=run_id)
        except ProcedureExecutionError as exc:
            raise HTTPException(409, detail={"code":exc.code}) from exc
        except KnowledgeAuthorizationError as exc:
            raise HTTPException(403, detail="source memory read denied") from exc
        except Exception as exc:
            raise _error(exc) from exc

    @router.post("/api/execution-workers/assignments/{assignment_id}/procedure/outcome-draft")
    async def convert(assignment_id: str, payload: OutcomeDraftRequest, request: Request):
        actor = request_actor(request)
        try:
            return await asyncio.to_thread(service.convert, assignment_id,
                actor=actor, **payload.model_dump())
        except ProcedureExecutionError as exc:
            raise HTTPException(409, detail={"code":exc.code}) from exc
        except KnowledgeAuthorizationError as exc:
            raise HTTPException(403, detail="source memory authority denied") from exc
        except Exception as exc:
            raise _error(exc) from exc

    return router
