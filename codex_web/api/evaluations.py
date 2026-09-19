from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from codex_web.api.identity import request_actor
from codex_web.evaluations import EvaluationRunRequest, EvaluationScenarioCreate
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.evaluations import EvaluationError, EvaluationService
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.storage.evaluations import (
    EvaluationConflictError,
    EvaluationNotFoundError,
)


class EvaluationSuiteRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_fixture_id: str | None = None


def build_evaluations_router(service: EvaluationService) -> APIRouter:
    router = APIRouter(prefix="/api/evaluations", tags=["evaluations"])

    def error(exc: Exception) -> HTTPException:
        if isinstance(exc, EvaluationNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, EvaluationConflictError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, AuthorizationError):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(exc, (EvaluationError, ValueError)):
            return HTTPException(status_code=400, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    def admin(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not any(
                scope in actor.service_scopes
                for scope in ("evaluation:admin", "evaluation:run")
            ):
                raise HTTPException(
                    status_code=403,
                    detail="evaluation:admin or evaluation:run service scope required",
                )
            return actor
        try:
            IdentityService.require_admin(actor)
            IdentityService.require_assurance(
                actor,
                AuthenticationAssurance.MFA,
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return actor

    @router.get("/scenarios")
    async def list_scenarios(
        request: Request,
        suite_id: str | None = None,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        items = service.list_scenarios(actor, suite_id=suite_id)
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "count": len(items),
            "backends": list(service.backend_ids()),
        }

    @router.post("/scenarios")
    async def create_scenario(
        payload: EvaluationScenarioCreate,
        request: Request,
    ) -> dict[str, Any]:
        actor = admin(request)
        try:
            item = service.create_scenario(payload, actor=actor)
        except Exception as exc:
            raise error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/scenarios/{scenario_id}/{version}")
    async def get_scenario(
        scenario_id: str,
        version: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.get_scenario(
                scenario_id,
                version,
                actor=request_actor(request),
            )
        except Exception as exc:
            raise error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.post("/runs")
    async def run(
        payload: EvaluationRunRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = admin(request)
        try:
            item, comparison = await service.run(payload, actor=actor)
        except Exception as exc:
            raise error(exc) from exc
        return {
            "item": item.model_dump(mode="json"),
            "comparison": (
                comparison.model_dump(mode="json")
                if comparison is not None
                else None
            ),
        }

    @router.get("/runs")
    async def list_runs(
        request: Request,
        scenario_id: str | None = None,
    ) -> dict[str, Any]:
        items = service.list_runs(
            request_actor(request),
            scenario_id=scenario_id,
        )
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "count": len(items),
        }

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> dict[str, Any]:
        try:
            item = service.get_run(run_id, actor=request_actor(request))
        except Exception as exc:
            raise error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/comparisons")
    async def list_comparisons(
        request: Request,
        scenario_id: str | None = None,
    ) -> dict[str, Any]:
        items = service.list_comparisons(
            request_actor(request),
            scenario_id=scenario_id,
        )
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "count": len(items),
        }

    @router.post("/suites/{suite_id}/run")
    async def run_suite(
        suite_id: str,
        payload: EvaluationSuiteRunRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = admin(request)
        try:
            item = await service.run_suite(
                suite_id,
                actor=actor,
                candidate_fixture_id=payload.candidate_fixture_id,
            )
        except Exception as exc:
            raise error(exc) from exc
        return {"item": item.model_dump(mode="json")}

    @router.get("/suites/runs")
    async def list_suite_runs(
        request: Request,
        suite_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        items = service.list_suite_runs(
            request_actor(request),
            suite_id=suite_id,
        )[:limit]
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "count": len(items),
        }

    return router
