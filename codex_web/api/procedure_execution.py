"""Assignment-bound operator controls. No arbitrary commands, grants or side effects."""
import asyncio
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from codex_web.api.identity import request_actor
from codex_web.definitions import DefinitionReference
from codex_web.services.procedure_execution import ProcedureExecutionError
from codex_web.services.procedure_discovery import ProcedureAdmissionError

class ProcedureStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: DefinitionReference
    input_snapshot_id: str = Field(min_length=1, max_length=200)

class ProcedureDeviationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=16)


def build_procedure_execution_router(service):
    router = APIRouter(tags=["execution-workers"])
    def view(item):
        return {"assignment_id":item.id, "execution_id":item.execution_id,
            "procedure_checkpoint":item.procedure_checkpoint.model_dump(mode="json") if item.procedure_checkpoint else None}
    def error(exc):
        return HTTPException(status_code=409, detail={"code":exc.code})

    @router.get("/api/execution-workers/assignments/{assignment_id}/procedure")
    async def inspect(assignment_id: str, request: Request):
        try:
            return view(await asyncio.to_thread(service.get, assignment_id, request_actor(request)))
        except ProcedureExecutionError as exc:
            raise error(exc) from exc

    @router.post("/api/execution-workers/assignments/{assignment_id}/procedure/start")
    async def start(assignment_id: str, payload: ProcedureStart, request: Request):
        try:
            return view(await asyncio.to_thread(service.start, assignment_id,
                definition=payload.definition, input_snapshot_id=payload.input_snapshot_id, actor=request_actor(request)))
        except (ProcedureExecutionError, ProcedureAdmissionError) as exc:
            raise error(exc) from exc

    @router.post("/api/execution-workers/assignments/{assignment_id}/procedure/advance")
    async def advance(assignment_id: str, request: Request):
        try:
            return view(await service.advance(assignment_id, actor=request_actor(request)))
        except ProcedureExecutionError as exc:
            raise error(exc) from exc

    @router.post("/api/execution-workers/assignments/{assignment_id}/procedure/deviate")
    async def deviate(assignment_id: str, payload: ProcedureDeviationRequest, request: Request):
        try:
            return view(await service.deviate(assignment_id, actor=request_actor(request), **payload.model_dump()))
        except ProcedureExecutionError as exc:
            raise error(exc) from exc
    return router
