from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter


class RuntimeService:
    def __init__(
        self,
        host: Any,
        *,
        static_version: Callable[[], str] | None = None,
        runtime_health: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.host = host
        self.static_version = (
            static_version
            or getattr(host, "_static_version", lambda: "unknown")
        )
        self.runtime_health = (
            runtime_health
            or getattr(
                host,
                "_daemon_health",
                lambda: {"ok": True, "problems": []},
            )
        )
        # Preserve the historical direct-call health/recovery entrypoints without
        # retaining duplicate implementations in the legacy runtime.
        host.healthz = self.healthz
        host.recovery_resume = self.recovery_resume

    async def status(self) -> dict[str, Any]:
        try:
            await CodexAgentRuntimeAdapter(self.host.codex).recover()
        except Exception:
            pass
        return {
            "ok": self.host.codex.ready.is_set(),
            "pid": self.host.codex.proc.pid if self.host.codex.proc else None,
            "error": None if self.host.codex.ready.is_set() else self.host.codex.last_error,
            "version": self.static_version(),
            "pendingApprovals": list(self.host.codex.pending_approvals.values()),
            "activeTurns": len(self.host._load_active_turns()),
            "queuedTurns": sum(len(items) for items in self.host._load_turn_queues().values()),
        }

    def health(self) -> dict[str, Any]:
        return self.runtime_health()

    async def healthz(self) -> dict[str, Any]:
        health = self.health()
        if not health["ok"]:
            raise HTTPException(status_code=503, detail=health)
        return health

    async def recovery_resume(self) -> dict[str, Any]:
        h = self.host
        try:
            await CodexAgentRuntimeAdapter(h.codex).recover()
        except Exception:
            pass
        now = time.time()
        stale_thread_ids = {
            thread_id
            for thread_id, active in h._load_active_turns().items()
            if now - active.updated_at > h._active_turn_stale_seconds()
        }
        if stale_thread_ids:
            asyncio.create_task(h._resume_active_threads_after_startup(stale_thread_ids))
        for thread_id in h._load_turn_queues():
            h._schedule_queue_drain(thread_id)
        return {
            "ok": True,
            "resumingStaleThreads": sorted(stale_thread_ids),
            "activeTurns": len(h._load_active_turns()),
            "queuedTurns": sum(len(items) for items in h._load_turn_queues().values()),
        }

    def operations(self, *, window_seconds: float = 900.0) -> dict[str, Any]:
        h = self.host
        now = time.time()
        window = max(60.0, min(float(window_seconds), 86400.0))
        since = now - window

        queues = h._load_turn_queues()
        queued = [item for items in queues.values() for item in items]
        queue_ages = [max(0.0, now - float(item.created_at)) for item in queued]
        active_turns = h._load_active_turns()

        states = list(h._load_work_item_states().values())
        open_states = [
            state
            for state in states
            if state.current_stage != "closed" and not getattr(state, "closed_at", None)
        ]
        stages = Counter(state.current_stage for state in open_states)
        owners = Counter(
            (state.current_owner or state.next_owner or "unassigned")
            for state in open_states
        )
        pending_handoffs = [
            state.handoff
            for state in open_states
            if state.handoff and state.handoff.status == "pending"
        ]
        pending_handoff_ages = [
            max(0.0, now - float(handoff.requested_at))
            for handoff in pending_handoffs
        ]
        split_brain_count = sum(
            1
            for state in open_states
            if h._work_item_split_brain_findings(state)
        )

        events = [
            event
            for event in h._recent_bot_events(300)
            if float(event.get("created_at") or 0) >= since
        ]
        event_types = Counter(str(event.get("type") or "unknown") for event in events)
        delivery_failures = sum(
            1
            for event in events
            if isinstance(event.get("delivery"), dict)
            and event["delivery"].get("sent") is False
        )
        recovery_events = sum(
            count
            for event_type, count in event_types.items()
            if "recovery" in event_type or "replaced" in event_type
        )
        executive_events = sum(
            count
            for event_type, count in event_types.items()
            if event_type.startswith("executive_") or event_type.startswith("executive.")
        )

        supervisor = getattr(getattr(h.app, "state", None), "runtime_supervisor", None)
        supervisor_tasks = supervisor.task_status() if supervisor is not None else {}
        health = self.runtime_health()

        return {
            "generatedAt": now,
            "windowSeconds": window,
            "runtime": {
                "healthy": bool(health.get("ok")),
                "healthProblems": list(health.get("problems") or []),
                "activeTurns": len(active_turns),
                "queuedTurns": len(queued),
                "queueThreads": sum(1 for items in queues.values() if items),
                "oldestQueueAgeSeconds": max(queue_ages, default=0.0),
                "averageQueueAgeSeconds": (sum(queue_ages) / len(queue_ages)) if queue_ages else 0.0,
                "pendingApprovals": len(h.codex.pending_approvals),
                "supervisorTasks": supervisor_tasks,
            },
            "workItems": {
                "open": len(open_states),
                "stages": dict(sorted(stages.items())),
                "owners": dict(sorted(owners.items())),
                "pendingHandoffs": len(pending_handoffs),
                "oldestPendingHandoffAgeSeconds": max(pending_handoff_ages, default=0.0),
                "releaseGates": sum(1 for state in open_states if state.release_gate),
                "splitBrain": split_brain_count,
                "blocked": sum(1 for state in open_states if state.current_stage == "failed_with_action_owner"),
            },
            "providers": {
                "runtimeConnections": int(health.get("runtimeConnections") or 0),
                "recentDeliveryFailures": delivery_failures,
                "slackBackfillCooldownRemainingSeconds": float(
                    health.get("slackBackfillCooldownRemainingSeconds") or 0.0
                ),
                "gitlabSyncConsecutiveFailures": int(health.get("gitlabSyncConsecutiveFailures") or 0),
                "gitlabSyncLastError": health.get("gitlabSyncLastError"),
                "gitlabSyncLastSuccessAt": health.get("gitlabSyncLastSuccessAt"),
            },
            "activity": {
                "events": len(events),
                "eventTypes": dict(sorted(event_types.items())),
                "recoveryEvents": recovery_events,
                "executiveEvents": executive_events,
            },
        }

    async def distributed_status(self) -> dict[str, Any]:
        state = getattr(self.host.app, "state", None)
        store = getattr(state, "state_store", None)
        coordination = getattr(state, "coordination_backend", None)
        ownership = getattr(state, "replicated_ownership_service", None)
        bus = getattr(state, "canonical_event_bus", None)
        transport_runtime = getattr(state, "event_transport_runtime", None)

        transport_health = (
            await bus.transport_health()
            if bus is not None
            else None
        )
        outbox = (
            bus.store.outbox_status()
            if bus is not None
            else {}
        )
        coordination_health = (
            coordination.health().model_dump(mode="json")
            if coordination is not None
            else None
        )
        return {
            "deploymentMode": getattr(state, "deployment_mode", "local"),
            "instanceId": getattr(state, "instance_id", None),
            "stateStore": store.status() if store is not None else None,
            "eventTransport": (
                transport_health.model_dump(mode="json")
                if transport_health is not None
                else None
            ),
            "outbox": outbox,
            "coordination": coordination_health,
            "ownership": ownership.status() if ownership is not None else None,
            "transportRuntime": (
                await transport_runtime.status()
                if transport_runtime is not None
                else None
            ),
            "replicatedSafety": {
                "sharedStore": bool(
                    store is not None
                    and store.status().get("shared", False)
                ),
                "sharedCoordination": bool(
                    coordination is not None
                    and coordination.shared
                ),
                "durableConsumerGroupTransport": bool(
                    getattr(
                        getattr(state, "event_transport", None),
                        "capabilities",
                        None,
                    )
                    and state.event_transport.capabilities.durable
                    and state.event_transport.capabilities.consumer_groups
                ),
            },
        }

    async def rate_limits(self) -> dict[str, Any]:
        return await self.host.codex.request("account/rateLimits/read")

    async def models(self, *, include_hidden: bool = False) -> dict[str, Any]:
        try:
            return await self.host.codex.request(
                "model/list",
                {"includeHidden": include_hidden, "limit": 100},
            )
        except Exception as exc:
            self.host._append_bot_event({"type": "model_list_failed", "error": str(exc)})
            return {"data": [], "nextCursor": None, "error": str(exc)}
