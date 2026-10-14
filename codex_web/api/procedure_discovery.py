"""Read-only applicability inspection through canonical provider/Skill services."""
import asyncio
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from codex_web.action_providers import ActionRequest
from codex_web.api.identity import request_actor
from codex_web.services.action_providers import ActionProviderError
from codex_web.services.identity import AuthorizationError, TenantIsolationError
from codex_web.services.projects import ProjectNotFoundError


class ProcedurePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_id: str | None = None
    binding_id: str
    request: ActionRequest


def build_procedure_discovery_router(discovery, execution, intents, projects):
    router = APIRouter(tags=["skills"])

    @router.post("/api/skills/procedure-applicability")
    async def preview(payload: ProcedurePreview, request: Request):
        actor = request_actor(request)
        try:
            def inspect():
                if payload.request.project_id is not None:
                    projects.get(payload.request.project_id, scope=actor.tenant)
                _, provider, definition, effective = execution.resolve_contract(
                    payload.binding_id, payload.request, actor=actor)
                authority = intents._canonical_authority_snapshot(definition=definition, request=effective, actor=actor)
                context = intents._procedure_context(actor=actor, request=effective, definition=definition,
                    provider=provider, policy_source=authority.source, policy_allows=authority.outcome.value == "allow", execution_id=payload.execution_id)
                return discovery.discover(context)
            result, _ = await asyncio.to_thread(inspect)
            return {"preview": True, "authorizes_mutation": False, "discovery": result.model_dump(mode="json")}
        except (AuthorizationError, TenantIsolationError) as exc:
            raise HTTPException(status_code=403, detail={"code": "procedure_preview_denied"}) from exc
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail={"code": "procedure_project_unavailable"}) from exc
        except ActionProviderError as exc:
            raise HTTPException(status_code=409, detail={"code": "procedure_preview_unavailable", "message": str(exc)}) from exc

    return router
