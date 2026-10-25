"""Assignment-bound operator controls. No arbitrary commands, grants or side effects."""
import asyncio
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from codex_web.api.identity import request_actor
from codex_web.services.procedure_execution import ProcedureExecutionError
from codex_web.services.procedure_discovery import ProcedureAdmissionError

from codex_web.procedure_execution import ProcedureStart, ProcedureAdvance

class ProcedureDeviationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str | None = Field(default=None, max_length=100)
    step_id: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=16)


def build_procedure_execution_router(service):
    router = APIRouter(tags=["execution-workers"])
    def view(item):
        return service.view(item)
    def error(exc):
        return HTTPException(status_code=409, detail={"code":exc.code})

    @router.get("/api/execution-workers/assignments/{assignment_id}/procedure")
    async def inspect(assignment_id: str, request: Request, run_id: str | None = None):
        try:
            return await asyncio.to_thread(service.inspect, assignment_id, request_actor(request), run_id=run_id)
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
    async def advance(assignment_id: str, request: Request, payload: ProcedureAdvance = ProcedureAdvance()):
        try:
            return view(await service.advance(assignment_id, actor=request_actor(request), run_id=payload.run_id,
                **({"current_input_snapshot_id": payload.current_input_snapshot_id}
                    if payload.current_input_snapshot_id is not None else {})))
        except ProcedureExecutionError as exc:
            raise error(exc) from exc

    @router.post("/api/execution-workers/assignments/{assignment_id}/procedure/deviate")
    async def deviate(assignment_id: str, payload: ProcedureDeviationRequest, request: Request):
        try:
            return view(await service.deviate(assignment_id, actor=request_actor(request), **payload.model_dump()))
        except ProcedureExecutionError as exc:
            raise error(exc) from exc
    return router
