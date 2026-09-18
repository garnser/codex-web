from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.model_gateway import (
    ModelDefinitionUpsert,
    ModelInvocationRequest,
    ModelProviderUpsert,
    PromptTemplateUpsert,
    TenantModelPolicyUpdate,
)
from codex_web.services.identity import AuthorizationError
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


def build_model_gateway_router(service: ModelGatewayService) -> APIRouter:
    router = APIRouter(prefix="/api/model-gateway", tags=["model-gateway"])

    @router.get("/providers")
    async def providers(request: Request) -> dict[str, Any]:
        items = service.list_providers(request_actor(request))
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.put("/providers/{provider_id}")
    async def put_provider(
        provider_id: str,
        payload: ModelProviderUpsert,
        request: Request,
    ) -> dict[str, Any]:
        if provider_id != payload.id:
            raise HTTPException(status_code=422, detail="provider id mismatch")
        try:
            item = service.upsert_provider(payload, actor=request_actor(request))
            return {"item": item.model_dump(mode="json")}
        except Exception as exc:
            if isinstance(exc, (ModelGatewayError, AuthorizationError, ValueError)):
                raise _error(exc) from exc
            raise

    @router.get("/models")
    async def models(request: Request) -> dict[str, Any]:
        items = service.list_models(request_actor(request))
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.put("/models/{model_id}")
    async def put_model(
        model_id: str,
        payload: ModelDefinitionUpsert,
        request: Request,
    ) -> dict[str, Any]:
        if model_id != payload.id:
            raise HTTPException(status_code=422, detail="model id mismatch")
        try:
            item = service.upsert_model(payload, actor=request_actor(request))
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
            item = service.upsert_template(payload, actor=request_actor(request))
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
            item = service.set_policy(payload, actor=request_actor(request))
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
