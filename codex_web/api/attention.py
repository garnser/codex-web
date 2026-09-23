from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.identity import request_actor
from codex_web.services.attention import AttentionService, AttentionStateError
from codex_web.services.identity import AuthorizationError
from codex_web.storage.attention import AttentionItemNotFoundError


class AttentionResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class AttentionSnoozeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    until: float


class AttentionReassignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    owner_identity_id: str = Field(min_length=1, max_length=500)


class AttentionBulkAcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_ids: tuple[str, ...] = Field(min_length=1, max_length=100)


def build_attention_router(service: AttentionService) -> APIRouter:
    router = APIRouter(prefix="/api/attention", tags=["attention"])

    def serialize(item) -> dict[str, Any]:
        return item.model_dump(mode="json")

    def translate(exc: Exception) -> HTTPException:
        if isinstance(exc, AttentionItemNotFoundError):
            return HTTPException(status_code=404, detail="attention item not found")
        if isinstance(exc, AttentionStateError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, AuthorizationError):
            return HTTPException(status_code=403, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    @router.get("")
    async def list_items(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: int = Query(default=0, ge=0),
        status: str | None = Query(default=None),
        severity: str | None = Query(default=None),
        project_id: str | None = Query(default=None),
        item_type: str | None = Query(default=None, alias="type"),
        assignee: str | None = Query(default=None),
    ) -> dict[str, Any]:
        actor = request_actor(request)
        items, next_cursor, total = service.list_page(
            actor,
            limit=limit,
            cursor=cursor,
            status=status,
            severity=severity,
            project_id=project_id,
            item_type=item_type,
            assignee=assignee,
        )
        return {
            "attention_items": [serialize(item) for item in items],
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
            "total": total,
        }

    @router.post("/bulk/acknowledge")
    async def bulk_acknowledge(
        payload: AttentionBulkAcknowledgeRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            items, skipped = await service.acknowledge_many(
                payload.item_ids,
                actor=actor,
            )
        except Exception as exc:
            raise translate(exc) from exc
        return {
            "attention_items": [serialize(item) for item in items],
            "skipped_item_ids": list(skipped),
        }

    @router.get("/{item_id}")
    async def get_item(item_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = service.get(item_id, actor=actor)
        except Exception as exc:
            raise translate(exc) from exc
        return {"attention_item": serialize(item)}

    @router.post("/{item_id}/acknowledge")
    async def acknowledge(item_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = await service.acknowledge(item_id, actor=actor)
        except Exception as exc:
            raise translate(exc) from exc
        return {"attention_item": serialize(item)}

    @router.post("/{item_id}/resolve")
    async def resolve(
        item_id: str,
        payload: AttentionResolveRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = await service.resolve(item_id, actor=actor, reason=payload.reason)
        except Exception as exc:
            raise translate(exc) from exc
        return {"attention_item": serialize(item)}

    @router.post("/{item_id}/reassign")
    async def reassign(
        item_id: str,
        payload: AttentionReassignRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = await service.reassign(
                item_id,
                actor=actor,
                owner_identity_id=payload.owner_identity_id,
            )
        except Exception as exc:
            raise translate(exc) from exc
        return {"attention_item": serialize(item)}

    @router.post("/{item_id}/escalate")
    async def escalate(item_id: str, request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = await service.escalate_for_actor(item_id, actor=actor)
        except Exception as exc:
            raise translate(exc) from exc
        return {"attention_item": serialize(item)}

    @router.post("/{item_id}/snooze")
    async def snooze(
        item_id: str,
        payload: AttentionSnoozeRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            item = await service.snooze(item_id, actor=actor, until=payload.until)
        except Exception as exc:
            raise translate(exc) from exc
        return {"attention_item": serialize(item)}

    return router
