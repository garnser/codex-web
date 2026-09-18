from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from codex_web.api.identity import request_actor
from codex_web.goal_decomposition import (
    GoalDecompositionGenerationRequest,
    GoalDecompositionProposalCreate,
    GoalDecompositionProposalRevise,
    GoalDecompositionReview,
)
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.goal_decomposition_generation import (
    GoalDecompositionGenerationService,
)
from codex_web.services.goal_decompositions import (
    GoalDecompositionConflictError,
    GoalDecompositionError,
    GoalDecompositionNotFoundError,
    GoalDecompositionScopeError,
    GoalDecompositionService,
)
from codex_web.services.identity import AuthorizationError, IdentityService


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, GoalDecompositionNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, GoalDecompositionConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (AuthorizationError, GoalDecompositionScopeError)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (GoalDecompositionError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_goal_decompositions_router(
    service: GoalDecompositionService,
    generation: GoalDecompositionGenerationService | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/goals/{goal_id}/decompositions",
        tags=["goal-decompositions"],
    )

    def mutation_actor(request: Request):
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "goals:admin" not in actor.service_scopes:
                raise AuthorizationError(
                    "goals:admin service scope required"
                )
            return actor
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )
        return actor

    @router.get("")
    async def list_proposals(
        goal_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.list(goal_id, scope=actor.tenant)
        except (GoalDecompositionError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    @router.post("")
    async def create_proposal(
        goal_id: str,
        payload: GoalDecompositionProposalCreate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            proposal = service.create(
                goal_id,
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
        except (
            AuthorizationError,
            GoalDecompositionError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"proposal": proposal.model_dump(mode="json")}

    @router.get("/events")
    async def events(
        goal_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            rows = service.events(
                goal_id,
                scope=actor.tenant,
                limit=limit,
            )
        except (GoalDecompositionError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    if generation is not None:
        @router.post("/generate")
        async def generate_proposal(
            goal_id: str,
            payload: GoalDecompositionGenerationRequest,
            request: Request,
        ) -> dict[str, Any]:
            try:
                actor = mutation_actor(request)
                proposal = await generation.generate(
                    goal_id,
                    payload,
                    actor=actor,
                )
            except (
                AuthorizationError,
                GoalDecompositionError,
                ValueError,
            ) as exc:
                raise _error(exc) from exc
            return {"proposal": proposal.model_dump(mode="json")}

    @router.get("/{proposal_id}")
    async def get_proposal(
        goal_id: str,
        proposal_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            proposal = service.get(proposal_id, scope=actor.tenant)
            if proposal.goal_id != goal_id:
                raise GoalDecompositionNotFoundError(
                    "goal decomposition proposal not found"
                )
        except (GoalDecompositionError, ValueError) as exc:
            raise _error(exc) from exc
        return {"proposal": proposal.model_dump(mode="json")}

    @router.post("/{proposal_id}/revise")
    async def revise_proposal(
        goal_id: str,
        proposal_id: str,
        payload: GoalDecompositionProposalRevise,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            current = service.get(proposal_id, scope=actor.tenant)
            if current.goal_id != goal_id:
                raise GoalDecompositionNotFoundError(
                    "goal decomposition proposal not found"
                )
            proposal = service.revise(
                proposal_id,
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
        except (
            AuthorizationError,
            GoalDecompositionError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"proposal": proposal.model_dump(mode="json")}

    @router.post("/{proposal_id}/review")
    async def review_proposal(
        goal_id: str,
        proposal_id: str,
        payload: GoalDecompositionReview,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = mutation_actor(request)
            current = service.get(proposal_id, scope=actor.tenant)
            if current.goal_id != goal_id:
                raise GoalDecompositionNotFoundError(
                    "goal decomposition proposal not found"
                )
            proposal = service.review(
                proposal_id,
                payload,
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
        except (
            AuthorizationError,
            GoalDecompositionError,
            ValueError,
        ) as exc:
            raise _error(exc) from exc
        return {"proposal": proposal.model_dump(mode="json")}

    @router.get("/{proposal_id}/revisions")
    async def revisions(
        goal_id: str,
        proposal_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            current = service.get(proposal_id, scope=actor.tenant)
            if current.goal_id != goal_id:
                raise GoalDecompositionNotFoundError(
                    "goal decomposition proposal not found"
                )
            rows = service.revisions(proposal_id, scope=actor.tenant)
        except (GoalDecompositionError, ValueError) as exc:
            raise _error(exc) from exc
        return {
            "items": [item.model_dump(mode="json") for item in rows],
            "count": len(rows),
        }

    return router
