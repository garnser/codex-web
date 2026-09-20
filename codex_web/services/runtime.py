from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter


class RuntimeService:
    """Runtime/operator API behavior over explicit process dependencies."""

    def __init__(
        self,
        host: Any | None = None,
        *,
        codex: Any | None = None,
        static_version: Callable[[], str] | None = None,
        runtime_health: Callable[[], dict[str, Any]] | None = None,
        load_active_turns: Callable[[], dict[str, Any]] | None = None,
        load_turn_queues: Callable[[], dict[str, list[Any]]] | None = None,
        active_turn_stale_seconds: Callable[[], float] | None = None,
        resume_active_threads_after_startup: Callable[
            [set[str]], Any
        ] | None = None,
        schedule_queue_drain: Callable[[str], Any] | None = None,
        load_work_item_states: Callable[[], dict[str, Any]] | None = None,
        work_item_split_brain_findings: Callable[
            [Any], list[str]
        ] | None = None,
        recent_events: Callable[
            [int], list[dict[str, Any]]
        ] | None = None,
        supervisor_status: Callable[[], dict[str, Any]] | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        state_store: Any | None = None,
        coordination_backend: Any | None = None,
        replicated_ownership: Any | None = None,
        canonical_event_bus: Any | None = None,
        event_transport_runtime: Any | None = None,
        event_transport: Any | None = None,
        deployment_mode: str = "local",
        instance_id: str = "local",
    ) -> None:
        if host is not None:
            codex = codex or getattr(host, "codex", None)
            static_version = static_version or getattr(
                host,
                "_static_version",
                lambda: "unknown",
            )
            runtime_health = runtime_health or getattr(
                host,
                "_daemon_health",
                lambda: {"ok": True, "problems": []},
            )
            load_active_turns = load_active_turns or getattr(
                host,
                "_load_active_turns",
                lambda: {},
            )
            load_turn_queues = load_turn_queues or getattr(
                host,
                "_load_turn_queues",
                lambda: {},
            )
            active_turn_stale_seconds = (
                active_turn_stale_seconds
                or getattr(
                    host,
                    "_active_turn_stale_seconds",
                    lambda: 120.0,
                )
            )
            resume_active_threads_after_startup = (
                resume_active_threads_after_startup
                or getattr(
                    host,
                    "_resume_active_threads_after_startup",
                    lambda _ids: None,
                )
            )
            schedule_queue_drain = schedule_queue_drain or getattr(
                host,
                "_schedule_queue_drain",
                lambda _thread_id: None,
            )
            load_work_item_states = (
                load_work_item_states
                or getattr(host, "_load_work_item_states", lambda: {})
            )
            work_item_split_brain_findings = (
                work_item_split_brain_findings
                or getattr(
                    host,
                    "_work_item_split_brain_findings",
                    lambda _state: [],
                )
            )
            recent_events = recent_events or getattr(
                host,
                "_recent_bot_events",
                lambda _limit: [],
            )
            supervisor_status = supervisor_status or (
                lambda: {}
            )
            event_sink = event_sink or getattr(
                host,
                "_append_bot_event",
                lambda _event: None,
            )
            state = getattr(
                getattr(host, "app", None),
                "state",
                None,
            )
            state_store = state_store or getattr(
                state,
                "state_store",
                None,
            )
            coordination_backend = coordination_backend or getattr(
                state,
                "coordination_backend",
                None,
            )
            replicated_ownership = replicated_ownership or getattr(
                state,
                "replicated_ownership_service",
                None,
            )
            canonical_event_bus = canonical_event_bus or getattr(
                state,
                "canonical_event_bus",
                None,
            )
            event_transport_runtime = event_transport_runtime or getattr(
                state,
                "event_transport_runtime",
                None,
            )
            event_transport = event_transport or getattr(
                state,
                "event_transport",
                None,
            )
            deployment_mode = getattr(
                state,
                "deployment_mode",
                deployment_mode,
            )
            instance_id = getattr(
                state,
                "instance_id",
                instance_id,
            )

        if codex is None:
            raise TypeError("RuntimeService requires a runtime transport")
        self.codex = codex
        self.static_version = static_version or (lambda: "unknown")
        self.runtime_health = runtime_health or (
            lambda: {"ok": True, "problems": []}
        )
        self.load_active_turns = load_active_turns or (lambda: {})
        self.load_turn_queues = load_turn_queues or (lambda: {})
        self.active_turn_stale_seconds = (
            active_turn_stale_seconds or (lambda: 120.0)
        )
        self.resume_active_threads_after_startup = (
            resume_active_threads_after_startup
            or (lambda _ids: None)
        )
        self.schedule_queue_drain = (
            schedule_queue_drain or (lambda _thread_id: None)
        )
        self.load_work_item_states = (
            load_work_item_states or (lambda: {})
        )
        self.work_item_split_brain_findings = (
            work_item_split_brain_findings
            or (lambda _state: [])
        )
        self.recent_events = recent_events or (lambda _limit: [])
        self.supervisor_status = supervisor_status or (lambda: {})
        self.event_sink = event_sink or (lambda _event: None)
        self.state_store = state_store
        self.coordination_backend = coordination_backend
        self.replicated_ownership = replicated_ownership
        self.canonical_event_bus = canonical_event_bus
        self.event_transport_runtime = event_transport_runtime
        self.event_transport = event_transport
        self.deployment_mode = deployment_mode
        self.instance_id = instance_id

        if host is not None:
            host.healthz = self.healthz
            host.recovery_resume = self.recovery_resume

    async def status(self) -> dict[str, Any]:
        try:
            await CodexAgentRuntimeAdapter(self.codex).recover()
        except Exception:
            pass
        return {
            "ok": self.codex.ready.is_set(),
            "pid": self.codex.proc.pid if self.codex.proc else None,
            "error": (
                None if self.codex.ready.is_set() else self.codex.last_error
            ),
            "version": self.static_version(),
            "pendingApprovals": list(
                self.codex.pending_approvals.values()
            ),
            "activeTurns": len(self.load_active_turns()),
            "queuedTurns": sum(
                len(items) for items in self.load_turn_queues().values()
            ),
        }

    def health(self) -> dict[str, Any]:
        return self.runtime_health()

    async def healthz(self) -> dict[str, Any]:
        health = self.health()
        if not health["ok"]:
            raise HTTPException(status_code=503, detail=health)
        return health

    async def recovery_resume(self) -> dict[str, Any]:
        try:
            await CodexAgentRuntimeAdapter(self.codex).recover()
        except Exception:
            pass
        now = time.time()
        active_turns = self.load_active_turns()
        stale_thread_ids = {
            thread_id
            for thread_id, active in active_turns.items()
            if now - active.updated_at > self.active_turn_stale_seconds()
        }
        if stale_thread_ids:
            asyncio.create_task(
                self.resume_active_threads_after_startup(
                    stale_thread_ids
                )
            )
        queues = self.load_turn_queues()
        for thread_id in queues:
            self.schedule_queue_drain(thread_id)
        return {
            "ok": True,
            "resumingStaleThreads": sorted(stale_thread_ids),
            "activeTurns": len(active_turns),
            "queuedTurns": sum(len(items) for items in queues.values()),
        }

    def operations(
        self,
        *,
        window_seconds: float = 900.0,
    ) -> dict[str, Any]:
        now = time.time()
        window = max(60.0, min(float(window_seconds), 86400.0))
        since = now - window

        queues = self.load_turn_queues()
        queued = [item for items in queues.values() for item in items]
        queue_ages = [
            max(0.0, now - float(item.created_at)) for item in queued
        ]
        active_turns = self.load_active_turns()

        states = list(self.load_work_item_states().values())
        open_states = [
            state
            for state in states
            if state.current_stage != "closed"
            and not getattr(state, "closed_at", None)
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
            if self.work_item_split_brain_findings(state)
        )

        events = [
            event
            for event in self.recent_events(300)
            if float(event.get("created_at") or 0) >= since
        ]
        event_types = Counter(
            str(event.get("type") or "unknown") for event in events
        )
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
            if event_type.startswith("executive_")
            or event_type.startswith("executive.")
        )

        health = self.runtime_health()
        return {
            "generatedAt": now,
            "windowSeconds": window,
            "runtime": {
                "healthy": bool(health.get("ok")),
                "healthProblems": list(
                    health.get("problems") or []
                ),
                "activeTurns": len(active_turns),
                "queuedTurns": len(queued),
                "queueThreads": sum(
                    1 for items in queues.values() if items
                ),
                "oldestQueueAgeSeconds": max(
                    queue_ages,
                    default=0.0,
                ),
                "averageQueueAgeSeconds": (
                    sum(queue_ages) / len(queue_ages)
                    if queue_ages
                    else 0.0
                ),
                "pendingApprovals": len(
                    self.codex.pending_approvals
                ),
                "supervisorTasks": self.supervisor_status(),
            },
            "workItems": {
                "open": len(open_states),
                "stages": dict(sorted(stages.items())),
                "owners": dict(sorted(owners.items())),
                "pendingHandoffs": len(pending_handoffs),
                "oldestPendingHandoffAgeSeconds": max(
                    pending_handoff_ages,
                    default=0.0,
                ),
                "releaseGates": sum(
                    1 for state in open_states if state.release_gate
                ),
                "splitBrain": split_brain_count,
                "blocked": sum(
                    1
                    for state in open_states
                    if state.current_stage
                    == "failed_with_action_owner"
                ),
            },
            "providers": {
                "runtimeConnections": int(
                    health.get("runtimeConnections") or 0
                ),
                "recentDeliveryFailures": delivery_failures,
                "slackBackfillCooldownRemainingSeconds": float(
                    health.get(
                        "slackBackfillCooldownRemainingSeconds"
                    )
                    or 0.0
                ),
                "gitlabSyncConsecutiveFailures": int(
                    health.get("gitlabSyncConsecutiveFailures") or 0
                ),
                "gitlabSyncLastError": health.get(
                    "gitlabSyncLastError"
                ),
                "gitlabSyncLastSuccessAt": health.get(
                    "gitlabSyncLastSuccessAt"
                ),
            },
            "activity": {
                "events": len(events),
                "eventTypes": dict(sorted(event_types.items())),
                "recoveryEvents": recovery_events,
                "executiveEvents": executive_events,
            },
        }

    async def distributed_status(self) -> dict[str, Any]:
        transport_health = (
            await self.canonical_event_bus.transport_health()
            if self.canonical_event_bus is not None
            else None
        )
        outbox = (
            self.canonical_event_bus.store.outbox_status()
            if self.canonical_event_bus is not None
            else {}
        )
        coordination_health = (
            self.coordination_backend.health().model_dump(
                mode="json"
            )
            if self.coordination_backend is not None
            else None
        )
        capabilities = getattr(
            self.event_transport,
            "capabilities",
            None,
        )
        return {
            "deploymentMode": self.deployment_mode,
            "instanceId": self.instance_id,
            "stateStore": (
                self.state_store.status()
                if self.state_store is not None
                else None
            ),
            "eventTransport": (
                transport_health.model_dump(mode="json")
                if transport_health is not None
                else None
            ),
            "outbox": outbox,
            "coordination": coordination_health,
            "ownership": (
                self.replicated_ownership.status()
                if self.replicated_ownership is not None
                else None
            ),
            "transportRuntime": (
                await self.event_transport_runtime.status()
                if self.event_transport_runtime is not None
                else None
            ),
            "replicatedSafety": {
                "sharedStore": bool(
                    self.state_store is not None
                    and self.state_store.status().get("shared", False)
                ),
                "sharedCoordination": bool(
                    self.coordination_backend is not None
                    and self.coordination_backend.shared
                ),
                "durableConsumerGroupTransport": bool(
                    capabilities
                    and capabilities.durable
                    and capabilities.consumer_groups
                ),
            },
        }

    async def rate_limits(self) -> dict[str, Any]:
        return await self.codex.request("account/rateLimits/read")

    async def models(
        self,
        *,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        try:
            return await self.codex.request(
                "model/list",
                {"includeHidden": include_hidden, "limit": 100},
            )
        except Exception as exc:
            self.event_sink(
                {"type": "model_list_failed", "error": str(exc)}
            )
            return {"data": [], "nextCursor": None, "error": str(exc)}
