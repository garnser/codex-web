from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from codex_web.api.project_record_scope import ProjectRecordScope, project_record_context_parameter
from codex_web.services.model_provider_administration import ModelProviderAdministration

from codex_web.api.identity import request_actor
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.model_gateway import (
    ModelDefinitionUpsert,
    ModelInvocationRequest,
    ModelProviderUpsert,
    PromptTemplateUpsert,
    TenantModelPolicyUpdate,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.model_gateway import (
    ModelGatewayError,
    ModelGatewayService,
    ModelRegistryConflictError,
    ModelRoutingError,
)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ModelRegistryConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ModelRoutingError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, (ModelGatewayError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def build_model_gateway_router(service: ModelGatewayService, projects=None, administration=None) -> APIRouter:
    router = APIRouter(prefix="/api/model-gateway", tags=["model-gateway"],
                      dependencies=[Depends(project_record_context_parameter)])
    administration = administration or ModelProviderAdministration(service)

    def context(request: Request):
        ProjectRecordScope.from_request(request, request_actor(request), projects)

    router.dependencies.append(Depends(context))

    @router.get("/provider-administration")
    async def provider_administration(request: Request):
        return administration.catalog(request_actor(request))

    @router.get("/providers/{provider_id}/impact")
    async def provider_impact(provider_id: str, request: Request):
        try:
            return administration.impact(provider_id, request_actor(request))
        except Exception as exc:
            raise _error(exc) from exc

    def mutation_actor(request: Request):
        actor = request_actor(request)
        if actor.principal_kind != PrincipalKind.SERVICE:
            IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        return actor

    @router.get("/providers")
    async def providers(request: Request) -> dict[str, Any]:
        items = service.list_providers(request_actor(request))
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.put("/providers/{provider_id}")
    async def put_provider(
        provider_id: str,
        payload: ModelProviderUpsert,
        request: Request,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        if provider_id != payload.id:
            raise HTTPException(status_code=422, detail="provider id mismatch")
        try:
            item = service.upsert_provider(payload, actor=mutation_actor(request), expected_revision=expected_revision)
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ModelGatewayError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.get("/models")
    async def models(request: Request) -> dict[str, Any]:
        items = service.list_models(request_actor(request))
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.get("/catalogs")
    async def catalogs(request: Request) -> dict[str, Any]:
        items = service.list_catalogs(request_actor(request))
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.post("/providers/{provider_id}/catalog/refresh")
    async def refresh_catalog(provider_id: str, request: Request) -> dict[str, Any]:
        try:
            item = await service.refresh_catalog(
                provider_id, actor=mutation_actor(request)
            )
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ModelGatewayError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.put("/models/{model_id}")
    async def put_model(
        model_id: str,
        payload: ModelDefinitionUpsert,
        request: Request,
    ) -> dict[str, Any]:
        if model_id != payload.id:
            raise HTTPException(status_code=422, detail="model id mismatch")
        try:
            item = service.upsert_model(payload, actor=mutation_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ModelGatewayError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.get("/prompts")
    async def prompts(request: Request) -> dict[str, Any]:
        items = service.list_templates(request_actor(request))
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.put("/prompts/{template_id}/{version}")
    async def put_prompt(
        template_id: str,
        version: str,
        payload: PromptTemplateUpsert,
        request: Request,
    ) -> dict[str, Any]:
        if template_id != payload.template_id or version != payload.version:
            raise HTTPException(status_code=422, detail="prompt template identity mismatch")
        try:
            item = service.upsert_template(payload, actor=mutation_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ModelGatewayError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.get("/policy")
    async def get_policy(request: Request) -> dict[str, Any]:
        item = service.get_policy(request_actor(request))
        return {"item": item.model_dump(mode="json")}

    @router.put("/policy")
    async def put_policy(
        payload: TenantModelPolicyUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            item = service.set_policy(payload, actor=mutation_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ModelGatewayError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.post("/route")
    async def route(
        payload: ModelInvocationRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.route(
                payload,
                actor=request_actor(request),
            ).model_dump(mode="json")
        except Exception as exc:
            if isinstance(exc, (ModelGatewayError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.get("/invocations")
    async def invocations(request: Request, limit: int = 100) -> dict[str, Any]:
        try:
            items = service.invocations(request_actor(request), limit=limit)
            return {"items": [item.model_dump(mode="json") for item in items]}
        except Exception as exc:
            if isinstance(exc, (ModelGatewayError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    return router
