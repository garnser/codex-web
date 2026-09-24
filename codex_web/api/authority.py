from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from codex_web.api.identity import request_actor
from codex_web.authority import AuthorityEvaluationRequest
from codex_web.definitions import DefinitionScope
from codex_web.identity import AuthenticationAssurance
from codex_web.services.authority_policy_explorer import AuthorityPolicyExplorerService
from codex_web.services.authority_roles import AuthorityRoleService
from codex_web.services.definitions import (
    DefinitionConflictError,
    DefinitionError,
    DefinitionNotFoundError,
)
from codex_web.services.identity import (
    AuthorizationError,
    IdentityError,
    IdentityService,
)
from codex_web.services.projects import ProjectNotFoundError, ProjectService
from codex_web.services.work_items import WorkItemService


class AuthoritySimulationHttpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request: AuthorityEvaluationRequest
    identity_id: str | None = None
    record_id: str | None = None
    work_item_ref: str | None = None


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, (DefinitionNotFoundError, ProjectNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (DefinitionConflictError,)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (AuthorizationError,)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (DefinitionError, IdentityError, LookupError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_authority_router(
    explorer: AuthorityPolicyExplorerService,
    authority: AuthorityRoleService,
    identity: IdentityService,
    work_items: WorkItemService,
    projects: ProjectService,
) -> APIRouter:
    router = APIRouter(tags=["authority-policy"])

    def admin(request: Request):
        actor = request_actor(request)
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    def subject(actor, identity_id: str | None):
        if not identity_id or identity_id == actor.identity_id:
            return actor
        return identity.actor_for_identity(identity_id, scope=actor.tenant)

    def work_item_project(
        actor,
        work_item_ref: str | None,
        project_id: str | None,
    ) -> str | None:
        if not work_item_ref:
            if project_id:
                projects.get(project_id, actor.tenant)
            return project_id
        item = work_items.get(work_item_ref)
        if (
            item.get("organization_id") != actor.organization_id
            or item.get("workspace_id") != actor.workspace_id
        ):
            raise AuthorizationError("work item is outside tenant scope")
        item_project = item.get("project_id")
        if project_id and item_project and project_id != item_project:
            raise ValueError("work item project conflicts with requested project")
        effective_project = item_project or project_id
        if effective_project:
            projects.get(effective_project, actor.tenant)
        return effective_project

    def require_record_visible(record, actor, *, project_id: str | None = None) -> None:
        if record.scope_type == DefinitionScope.GLOBAL:
            return
        if (
            record.scope_type == DefinitionScope.ORGANIZATION
            and record.scope_id == actor.organization_id
        ):
            return
        if (
            record.scope_type == DefinitionScope.WORKSPACE
            and record.scope_id == actor.workspace_id
        ):
            return
        if record.scope_type == DefinitionScope.PROJECT:
            if project_id is not None and record.scope_id != project_id:
                raise AuthorizationError(
                    "authority definition does not match requested project context"
                )
            projects.get(str(record.scope_id), actor.tenant)
            return
        raise AuthorizationError("authority definition is outside tenant/project scope")

    @router.get("/api/authority/effective")
    async def effective(
        request: Request,
        identity_id: str | None = None,
        project_id: str | None = None,
        work_item_ref: str | None = None,
        role_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            actor = admin(request)
            target = subject(actor, identity_id)
            effective_project = work_item_project(actor, work_item_ref, project_id)
            result = explorer.effective(
                actor=target,
                project_id=effective_project,
                role_id=role_id,
            )
            result["work_item_ref"] = work_item_ref
            return result
        except Exception as exc:
            if isinstance(
                exc,
                (
                    DefinitionError,
                    IdentityError,
                    AuthorizationError,
                    ProjectNotFoundError,
                    LookupError,
                    ValueError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/api/authority/access-subjects")
    async def access_subjects(
        request: Request,
        project_id: str | None = None,
        resource_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        try:
            actor = admin(request)
            if limit < 1 or limit > 200:
                raise ValueError("limit must be between 1 and 200")
            if offset < 0:
                raise ValueError("offset must be non-negative")
            effective_project = work_item_project(actor, None, project_id)

            resource = None
            if resource_id is not None:
                if authority.resources is None:
                    raise ValueError("canonical Resource Catalog is unavailable")
                resource = authority.resources.get(resource_id, actor)

            state = identity.state()
            active_memberships = {
                item.identity_id
                for item in state.memberships
                if item.principal_kind.value == "human"
                and item.organization_id == actor.organization_id
                and item.workspace_id in {None, actor.workspace_id}
                and item.revoked_at is None
            }
            humans = sorted(
                (
                    item
                    for item in state.humans
                    if item.id in active_memberships
                    and item.disabled_at is None
                ),
                key=lambda item: (item.display_name.lower(), item.id),
            )

            rows: list[dict[str, Any]] = []
            for human in humans:
                target = identity.actor_for_identity(
                    human.id,
                    scope=actor.tenant,
                )
                effective = (
                    explorer.effective_for_resource(
                        actor=target,
                        project_id=effective_project,
                        resource=resource,
                    )
                    if resource is not None
                    else explorer.effective(
                        actor=target,
                        project_id=effective_project,
                    )
                )
                if not effective["permission_matrix"]:
                    continue
                rows.append(
                    {
                        "identity": {
                            "id": human.id,
                            "display_name": human.display_name,
                            "email": human.email,
                        },
                        "assignments": effective["assignments"],
                        "permission_matrix": effective["permission_matrix"],
                    }
                )

            page = rows[offset : offset + limit]
            return {
                "organization_id": actor.organization_id,
                "workspace_id": actor.workspace_id,
                "project_id": effective_project,
                "resource": (
                    {
                        "id": resource.id,
                        "name": resource.name,
                        "resource_type": getattr(
                            resource.resource_type,
                            "value",
                            resource.resource_type,
                        ),
                    }
                    if resource is not None
                    else None
                ),
                "items": page,
                "count": len(page),
                "total": len(rows),
                "offset": offset,
                "limit": limit,
            }
        except Exception as exc:
            if isinstance(
                exc,
                (
                    DefinitionError,
                    IdentityError,
                    AuthorizationError,
                    ProjectNotFoundError,
                    LookupError,
                    ValueError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.get("/api/authority/impact/{record_id}")
    async def impact(record_id: str, request: Request) -> dict[str, Any]:
        try:
            actor = admin(request)
            record = explorer.registry.get_record(record_id)
            project_id = record.scope_id if record.scope_type == DefinitionScope.PROJECT else None
            require_record_visible(record, actor, project_id=project_id)
            result = explorer.impact(record_id)
            visible_work = work_items.list(
                project_id=None,
                owner=None,
                stage=None,
                release_gate=None,
                scope=actor.tenant,
            )
            visible_refs = {
                str(item.get("ref"))
                for item in visible_work.get("items", [])
                if item.get("ref")
            }
            active_work = [
                item
                for item in result["affected"]["active_work"]
                if item.get("object_type") == "work_item"
                and str(item.get("object_id")) in visible_refs
            ]
            result["affected"]["active_work"] = active_work
            result["affected"]["active_work_count"] = len(active_work)
            return result
        except Exception as exc:
            if isinstance(
                exc,
                (
                    DefinitionError,
                    IdentityError,
                    AuthorizationError,
                    ProjectNotFoundError,
                    LookupError,
                    ValueError,
                ),
            ):
                raise _error(exc) from exc
            raise

    @router.post("/api/authority/simulate")
    async def simulate(
        payload: AuthoritySimulationHttpRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            actor = admin(request)
            target = subject(actor, payload.identity_id)
            effective_project = work_item_project(
                actor,
                payload.work_item_ref,
                payload.request.project_id,
            )
            evaluation_request = payload.request.model_copy(
                update={"project_id": effective_project}
            )
            record = None
            if payload.record_id:
                record = explorer.registry.get_record(payload.record_id)
                require_record_visible(
                    record,
                    actor,
                    project_id=evaluation_request.project_id,
                )
                decision = authority.evaluate_record(
                    payload.record_id,
                    evaluation_request,
                    actor=target,
                )
            else:
                decision = authority.evaluate(
                    evaluation_request,
                    actor=target,
                )
            return {
                "mode": "exact-revision" if record is not None else "runtime-effective",
                "record_id": record.record_id if record is not None else None,
                "work_item_ref": payload.work_item_ref,
                "decision": decision.model_dump(mode="json"),
            }
        except Exception as exc:
            if isinstance(
                exc,
                (
                    DefinitionError,
                    IdentityError,
                    AuthorizationError,
                    ProjectNotFoundError,
                    LookupError,
                    ValueError,
                ),
            ):
                raise _error(exc) from exc
            raise

    return router
