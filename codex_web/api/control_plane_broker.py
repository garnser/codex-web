from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, HTTPException
from codex_web.authority import AuthorityEvaluationRequest

from codex_web.api.identity import request_actor
from codex_web.execution_workspaces import PublicationRecoveryHandoffRequest
from codex_web.services.publication_recovery import PublicationRecoveryConflict
from codex_web.services.publication_recovery_handoff import PublicationRecoveryHandoffService
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.services.control_plane_broker import ControlPlaneBrokerService
from codex_web.services.identity import IdentityService


def _operator_actor(request: Request):
    actor = request_actor(request)
    IdentityService.require_admin(actor)
    if actor.principal_kind != PrincipalKind.SERVICE:
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
    return actor


def build_control_plane_broker_router(
    service: ControlPlaneBrokerService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/control-plane-broker",
        tags=["control-plane-broker"],
    )

    @router.get("/operations")
    async def list_operations(request: Request) -> dict[str, Any]:
        _operator_actor(request)
        return {
            "items": [
                service.public_operation(item)
                for item in service.operations()
            ],
            "limits": service.limits.model_dump(mode="json"),
            "transport": "assignment-bound-unix-socket",
            "credential_exposed": False,
        }

    @router.get("/audit")
    async def list_audit(
        request: Request,
        assignment_id: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        actor = _operator_actor(request)
        events = service.audit_events(
            actor=actor,
            assignment_id=assignment_id,
            limit=limit,
        )
        return {
            "items": [item.model_dump(mode="json") for item in events],
            "count": len(events),
        }

    async def recovery_handoff(payload, request, *, preview):
        actor = _operator_actor(request)
        try:
            return await PublicationRecoveryHandoffService(service).handoff(payload, actor=actor, preview=preview)
        except (PublicationRecoveryConflict, ValueError, LookupError) as exc:
            raise HTTPException(status_code=409, detail={"code": "publication_recovery_handoff_denied",
                "reason": str(exc), "next_action": "Inspect current scope, ownership and exact completion receipts; do not replay unknown intents"}) from exc

    @router.post("/publication-recovery/preview")
    async def preview_recovery_handoff(payload: PublicationRecoveryHandoffRequest, request: Request):
        return await recovery_handoff(payload, request, preview=True)

    @router.post("/publication-recovery/handoff")
    async def commit_recovery_handoff(payload: PublicationRecoveryHandoffRequest, request: Request):
        return await recovery_handoff(payload, request, preview=False)

    @router.get("/coordination")
    async def inspect_coordination(request: Request, assignment_id: str, project_id: str,
                                   repository_resource_id: str, view: str = "threads",
                                   limit: int = 20, cursor: str | None = None):
        """Read the same typed projection with explicit operator attribution."""
        actor = _operator_actor(request)
        coordinator = service.project_coordination
        if coordinator is None:
            raise HTTPException(status_code=503, detail="Project coordination is unavailable")
        assignment = coordinator.workers.store.assignment(assignment_id)
        if (assignment is None or (assignment.organization_id, assignment.workspace_id, assignment.project_id) !=
                (actor.organization_id, actor.workspace_id, project_id)):
            raise HTTPException(status_code=404, detail="Assignment not found")
        from codex_web.services.project_coordination import OPERATION_MAP
        operation_id = {"threads": "project.threads.list", "memory": "project.memory.list",
                        "elevation": "project.elevation.read"}.get(view)
        if operation_id is None:
            raise HTTPException(status_code=422, detail="Unsupported coordination view")
        operation = OPERATION_MAP[operation_id]
        for capability in {"project.coordinate", operation.capability}:
            decision = service.authority.evaluate(AuthorityEvaluationRequest(capability=capability,
                level="read", project_id=project_id, resource_ids=(repository_resource_id,)), actor=actor)
            if decision.outcome != "allow":
                raise HTTPException(status_code=403, detail={"code": "coordination_inspection_denied",
                    "reasons": decision.reasons})
        query = {"project_id": [project_id], "repository_resource_id": [repository_resource_id],
                 "limit": [str(limit)], **({"cursor": [cursor]} if cursor else {})}
        result = await coordinator.dispatch(assignment, operation, None, query, {}, None, None)
        return {**result, "operator_inspection": {"actor": actor.identity_id,
            "assignment_requester": assignment.created_by, "read_only": True}}

    return router
