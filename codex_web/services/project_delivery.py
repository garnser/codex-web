from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from codex_web.autonomy import AutonomyMode, AutonomyObservation, AutonomyReasoningResult
from codex_web.canonical_events import CanonicalEventType
from codex_web.identity import TenantScope
from codex_web.models import ProjectDeliveryConfiguration, ProjectDeliveryUpdate, TurnCreate
from codex_web.services.identity import IdentityService


class ProjectDeliveryService:
    """Deterministic discovery and bounded wakeup for explicitly configured delivery."""

    ACTIONABLE = frozenset({"implementation_active", "ready_for_validation", "validation_running", "ready_to_close"})

    def __init__(self, *, projects, identity, scope, operator, states, turns, execution,
                 controller, events, event_sink, clock=time.time):
        self.projects, self.identity, self.scope = projects, identity, scope
        self.operator, self.states, self.turns, self.execution = operator, states, turns, execution
        self.controller, self.events, self.event_sink = controller, events, event_sink
        self.clock = clock
        self._next_scan: dict[str, float] = {}
        self._status: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def configure(self, project_id, payload: ProjectDeliveryUpdate, actor):
        IdentityService.require_admin(actor)
        project = self.projects.get(project_id, actor.tenant)
        if not payload.thread_id:
            return self.projects.set_delivery_supervision(project_id, None, actor.tenant)
        if project.authoritative_task_source is None:
            raise ValueError("Configure an authoritative task source before enabling delivery supervision")
        self.scope.for_thread(payload.thread_id, actor, project_id)
        config = ProjectDeliveryConfiguration(**payload.model_dump(), actor_identity_id=actor.identity_id)
        return self.projects.set_delivery_supervision(project_id, config, actor.tenant)

    def status(self, project_id, actor):
        project = self.projects.get(project_id, actor.tenant)
        return {"configuration": project.delivery_supervision,
                "last_scan": self._status.get(project_id),
                "next_scan_at": self._next_scan.get(project_id)}

    async def _context(self, project):
        config = project.delivery_supervision
        scope = TenantScope(organization_id=project.organization_id, workspace_id=project.workspace_id)
        actor = await asyncio.to_thread(self.identity.actor_for_identity, config.actor_identity_id, scope=scope)
        # Re-evaluate membership and exact thread ownership on every scan.
        await asyncio.to_thread(IdentityService.require_admin, actor)
        await asyncio.to_thread(self.scope.for_thread, config.thread_id, actor, project.id)
        return actor

    async def _scan(self, project):
        config = project.delivery_supervision
        actor = await self._context(project)
        recovery = getattr(self.turns, "recovery", None)
        replacement = (await asyncio.to_thread(recovery.replacement_thread_id, config.thread_id)
                       if recovery is not None else None)
        if replacement:
            current = await asyncio.to_thread(self.projects.get, project.id, actor.tenant)
            if current.delivery_supervision != config:
                return {"outcome": "configuration_changed", "actionable_count": 0}
            await asyncio.to_thread(self.scope.for_thread, replacement, actor, project.id)
            config = config.model_copy(update={"thread_id": replacement})
            project = await asyncio.to_thread(self.projects.set_delivery_supervision, project.id, config, actor.tenant)
        sync = await self.operator.sync_project(project.id, actor=actor.identity_id,
                                               reason="Configured project delivery backlog discovery")
        states = await asyncio.to_thread(self.states)
        discovered = set(sync.get("work_item_refs", ()))
        if "work_item_refs" in sync:
            # Discovery returns open work. Re-read absent retained open items before
            # trusting a stale canonical lane; bounded reconciliation never guesses closure.
            stale = [s for s in states.values() if s.project_id == project.id and not s.closed_at
                     and (s.organization_id, s.workspace_id) == (project.organization_id, project.workspace_id)
                     and s.current_stage != "closed" and s.source_identity and s.ref not in discovered]
            for state in sorted(stale, key=lambda s: s.ref)[:8]:
                await self.operator.reconcile(state.ref, actor=actor.identity_id,
                                              reason="Reconcile retained work absent from fresh open backlog")
            if stale:
                states = await asyncio.to_thread(self.states)
        items = [s for s in states.values() if s.project_id == project.id
                 and (s.organization_id, s.workspace_id) == (project.organization_id, project.workspace_id)
                 and s.current_stage in self.ACTIONABLE and not s.closed_at
                 and not s.blocker and not s.blocking_findings
                 and not (s.handoff and s.handoff.status == "pending")]
        items.sort(key=lambda s: (s.created_at, s.ref))
        if not items:
            return {"outcome": "idle", "actionable_count": 0}
        if await asyncio.to_thread(self.execution.thread_is_active, config.thread_id):
            return {"outcome": "active", "actionable_count": len(items)}
        drain = getattr(self.execution, "queue_drain_tasks", {}).get(config.thread_id)
        if drain is not None and not drain.done():
            return {"outcome": "dispatching", "actionable_count": len(items)}
        queue = await asyncio.to_thread(self.turns.queue, config.thread_id)
        if queue.get("queueDepth"):
            return {"outcome": "queued", "actionable_count": len(items)}
        signature = hashlib.sha256(json.dumps([(s.ref, s.current_stage, s.updated_at) for s in items],
                                               separators=(",", ":")).encode()).hexdigest()
        # A failed attempt must not tombstone the unchanged backlog forever.
        window = int(self.clock() // config.interval_seconds)
        delivery = await self.events.ingest(
            event_type=CanonicalEventType.WORK_TRANSITION, source="project-delivery-supervision",
            idempotency_key=f"{project.id}:{config.thread_id}:{signature}:{window}",
            payload={"project_id": project.id, "thread_id": config.thread_id,
                     "work_item_refs": [s.ref for s in items[:8]], "backlog_signature": signature},
            tenant_id=project.organization_id, workspace_id=project.workspace_id)
        if not delivery.inserted:
            return {"outcome": "duplicate", "actionable_count": len(items)}
        result = {}

        async def reasoner(*_args):
            current = await asyncio.to_thread(self.projects.get, project.id, actor.tenant)
            if current.delivery_supervision != config or current.authoritative_task_source != project.authoritative_task_source:
                return AutonomyReasoningResult(summary="Delivery configuration changed; dispatch deferred")
            await self._context(current)
            result.update(await self.turns.start(config.thread_id, TurnCreate(
                project_id=project.id, defer_start=True,
                message=(f"Configured delivery supervision found actionable work in Project {project.id}: "
                         + ", ".join(s.ref for s in items[:8]) + ". Refresh the authoritative backlog, "
                         "check existing PRs and active owners to avoid duplicate work, and pursue the next "
                         "independently actionable issue through implementation, validation, review, merge, "
                         "and evidence-backed closure. Continue with other actionable issues when one is "
                         "externally blocked. Preserve this thread's scope, authority, budgets and deployment constraints.")),
                actor=actor))
            return AutonomyReasoningResult(summary="Configured project delivery work queued")

        cycle = await self.controller.process(delivery.event,
            AutonomyObservation(deterministic_resolved=False, reasoning_score=1.0,
                                reason="Fresh actionable backlog requires engineering delivery"),
            cycle_key=f"project-delivery:{project.id}:{config.thread_id}", reasoner=reasoner)
        return {"outcome": str(cycle.outcome), "reason": cycle.reason,
                "actionable_count": len(items), "dispatch": result}

    async def run_cycle(self):
        if self._lock.locked():
            return
        async with self._lock:
            control = await asyncio.to_thread(lambda: self.controller.store.load().control)
            if control.mode != AutonomyMode.ACTIVE or control.dry_run or control.simulation:
                return
            projects = await asyncio.to_thread(self.projects.list)
            for project in projects:
                config = project.delivery_supervision
                now = self.clock()
                if not config or not config.enabled or not config.thread_id or not project.authoritative_task_source:
                    continue
                if self._next_scan.get(project.id, 0) > now:
                    continue
                self._next_scan[project.id] = now + config.interval_seconds
                try:
                    result = await self._scan(project)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    result = {"outcome": "failed", "error": type(exc).__name__}
                self._status[project.id] = {"observed_at": self.clock(), **result}
                self.event_sink({"type": "project_delivery_scan", "project_id": project.id,
                                 "thread_id": config.thread_id, **self._status[project.id]})
