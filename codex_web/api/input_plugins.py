from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.input_plugin_definitions import InputPipelineDefinition
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.input_plugin_definitions import InputPipelineDefinitionService


def build_input_plugins_router(
    service: InputPipelineDefinitionService,
) -> APIRouter:
    router = APIRouter(prefix="/api/input-plugins", tags=["input-plugins"])

    @router.get("")
    async def input_plugins(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            IdentityService.require_admin(actor)
            record = service.record(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
            )
            definition = InputPipelineDefinition.model_validate(record.payload)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"effective input pipeline unavailable: {exc}",
            ) from exc

        catalog = service.catalog.metadata()
        available = {
            (item["plugin_id"], item["plugin_version"]): item
            for item in catalog
        }
        registrations = []
        for item in definition.registrations:
            local = available.get((item.plugin_id, item.plugin_version))
            registrations.append(
                {
                    **item.model_dump(mode="json"),
                    "implementation_available": local is not None,
                    "implementation_transport": (
                        local["transport"] if local is not None else None
                    ),
                }
            )

        return {
            "definition": {
                "definition_id": record.definition_id,
                "kind": record.kind,
                "record_id": record.record_id,
                "revision": record.revision,
                "definition_schema_version": record.definition_schema_version,
                "scope_type": record.scope_type.value,
                "scope_id": record.scope_id,
                "lifecycle": record.lifecycle.value,
                "checksum": record.checksum,
                "published_by": record.published_by,
                "published_at": record.published_at,
                "effective_from": record.effective_from,
                "effective_until": record.effective_until,
            },
            "registrations": registrations,
            "catalog": list(catalog),
        }

    return router
