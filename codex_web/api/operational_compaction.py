from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from codex_web.api.identity import request_actor
from codex_web.services.operational_compaction import (
    DeliveryTargetCompactionPlan,
    OperationalCompactionError,
    OperationalCompactionService,
    OperationalCompactionStale,
)


class DeliveryTargetCompactionApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: DeliveryTargetCompactionPlan


class OperationalCompactionRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backup_ref: str = Field(min_length=1)


def build_operational_compaction_router(
    service: OperationalCompactionService,
    *,
    bot_runtime: Any,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/operational-state",
        tags=["operations", "bootstrap"],
    )

    async def run_quiesced(call):
        await bot_runtime.stop()
        task = asyncio.create_task(asyncio.to_thread(call))
        try:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Once mutation may have crossed the backup boundary, finish
                # the guarded operation so recovery metadata remains truthful.
                await task
                raise
        finally:
            await bot_runtime.sync()

    @router.get("/inspection")
    async def inspection(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            # Keep filesystem metadata/stat work away from the event loop.
            report = await asyncio.to_thread(
                lambda: (
                    service._require_admin(actor),
                    service.inspect_bounded(),
                )[1]
            )
            return report.model_dump(mode="json")
        except OperationalCompactionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/delivery-targets/plan")
    async def delivery_target_plan(
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            plan = await asyncio.to_thread(
                service.plan_delivery_target_compaction,
                actor=actor,
            )
            return {
                "plan": plan.model_dump(mode="json"),
                "destructive": plan.removed_count > 0,
                "requires_confirmation": False,
                "non_reconstructible_deletions": 0,
            }
        except OperationalCompactionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/delivery-targets/apply")
    async def delivery_target_apply(
        payload: DeliveryTargetCompactionApplyRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            execution = await run_quiesced(
                lambda: service.apply_delivery_target_compaction(
                    payload.plan,
                    actor=actor,
                )
            )
            return execution.model_dump(mode="json")
        except OperationalCompactionStale as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "operational_compaction_plan_stale",
                    "message": str(exc),
                },
            ) from exc
        except OperationalCompactionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/delivery-targets/restore")
    async def delivery_target_restore(
        payload: OperationalCompactionRestoreRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            return await run_quiesced(
                lambda: service.restore_delivery_target_backup(
                    payload.backup_ref,
                    actor=actor,
                )
            )
        except OperationalCompactionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/compactions")
    async def compactions(request: Request) -> dict[str, Any]:
        actor = request_actor(request)
        try:
            items = await asyncio.to_thread(
                service.executions,
                actor=actor,
            )
            return {
                "version": "1.0",
                "items": [
                    item.model_dump(mode="json")
                    for item in items
                ],
            }
        except OperationalCompactionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
