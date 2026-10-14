"""Explicit, fenced restoration of affirmatively unstarted batch originals."""

import asyncio
import json
import time
import uuid

from fastapi import HTTPException

from codex_web.authority import (
    AuthorityEvaluationRequest,
    AuthorityLevel,
    AuthorityDecisionOutcome,
)
from codex_web.identity import AuthenticationAssurance
from codex_web.notification_batches import NotificationBatchRecovery
from codex_web.services.identity import IdentityService
from codex_web.services.notification_batch_inspection import NotificationBatchInspection
from codex_web.services.turn_queue_policy import TurnQueuePolicy


class NotificationBatchRecoveryService:
    def __init__(
        self,
        *,
        preflight,
        queues,
        ownership,
        authority,
        projects,
        resources,
        replacement_resolver,
        actor_resolver,
        clock=time.time,
    ):
        self.preflight, self.queues, self.ownership = preflight, queues, ownership
        self.authority, self.projects, self.resources = authority, projects, resources
        self.replacement_resolver, self.actor_resolver, self.clock = (
            replacement_resolver,
            actor_resolver,
            clock,
        )

    def _get(self, batch_id, thread_id, project, actor):
        batch = self.preflight.get_notification_batch(batch_id)
        NotificationBatchInspection._authorize(
            batch, thread_id=thread_id, project=project, actor=actor
        )
        return batch

    @staticmethod
    def _decision_json(decision):
        return json.dumps(
            decision.model_dump(mode="json", exclude={"id", "evaluated_at"}),
            sort_keys=True,
            separators=(",", ":"),
        )

    def _authority(self, batch, project, actor):
        try:
            IdentityService.require_admin(actor)
            IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
            fresh = self.actor_resolver(actor.identity_id, project)
            IdentityService.require_admin(fresh)
        except (PermissionError, RuntimeError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if (fresh.identity_id, fresh.organization_id, fresh.workspace_id) != (
            actor.identity_id,
            project.organization_id,
            project.workspace_id,
        ):
            raise HTTPException(status_code=404, detail="notification batch not found")
        targets = tuple(
            dict.fromkeys(
                resource
                for member in batch.plan.members
                for resource in (
                    *member.writable_repository_resource_ids,
                    *member.read_only_repository_resource_ids,
                    *(
                        (member.repository_resource_id,)
                        if member.repository_resource_id
                        else ()
                    ),
                )
            )
        )
        if not set(targets).issubset(self.resources.resource_ids_for_project(project)):
            raise HTTPException(
                status_code=409,
                detail="Original repository scope is no longer bound to this Project.",
            )
        decision = self.authority.evaluate(
            AuthorityEvaluationRequest(
                capability="execution.retry",
                level=AuthorityLevel.EXECUTE,
                project_id=project.id,
                resource_ids=targets,
            ),
            actor=fresh,
        )
        if decision.outcome != AuthorityDecisionOutcome.ALLOW:
            raise HTTPException(
                status_code=403, detail="Current Role authority denies batch recovery."
            )
        return decision

    def _target(self, batch, project):
        original = batch.plan.members[0].thread_id
        return (
            self.replacement_resolver(
                original,
                project_id=project.id,
                organization_id=project.organization_id,
                workspace_id=project.workspace_id,
            )
            or original
        )

    def preview(self, batch_id, *, thread_id, project, actor):
        batch = self._get(batch_id, thread_id, project, actor)
        self._authority(batch, project, actor)
        try:
            target = self._target(batch, project)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "notification_batch_route_unverified",
                    "message": str(exc),
                },
            ) from exc
        available = batch.phase in {"prepared", "prestart_refused"} and bool(
            batch.queue_order
        )
        blocker = None
        if available:
            try:
                self.preflight._notification_recovery_members(
                    batch,
                    [
                        member.model_dump(mode="json") if member is not None else None
                        for member in [
                            self.preflight.get(attempt_id)
                            for attempt_id in batch.attempt_ids
                        ]
                    ],
                )
            except ValueError as exc:
                available = False
                blocker = str(exc)
        return {
            "schema_version": "1.0",
            "batch_id": batch.id,
            "phase": batch.phase,
            "expected_admission_sha256": batch.recovery.source_admission_sha256
            if batch.recovery
            else batch.recovery_source_digest(),
            "original_count": len(batch.plan.members),
            "original_ids": [member.id for member in batch.plan.members],
            "original_thread_id": batch.plan.members[0].thread_id,
            "target_thread_id": target,
            "can_restore": available,
            "recovery_state": batch.recovery.state if batch.recovery else None,
            "reserved_reason": batch.recovery.reason if batch.recovery else None,
            "reserved_by": batch.recovery.requested_by if batch.recovery else None,
            "effect": "Return exact originals to the queue without starting native work. Every original outcome remains unresolved.",
            "blocker": blocker
            or (
                None
                if available
                else "Originals already returned to the queue."
                if batch.phase == "released"
                else "Native delivery or uncertainty requires authoritative batch reconciliation; absence of receipts is insufficient."
            ),
        }

    async def recover(self, batch_id, request, *, thread_id, project, actor):
        batch = await asyncio.to_thread(self._get, batch_id, thread_id, project, actor)
        try:
            await asyncio.to_thread(self._authority, batch, project, actor)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        try:
            target = await asyncio.to_thread(self._target, batch, project)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "notification_batch_route_unverified",
                    "message": str(exc),
                },
            ) from exc
        if self.ownership is None:
            raise HTTPException(
                status_code=409, detail="Owned queue recovery is unavailable."
            )
        responsibilities = sorted(
            {
                "thread-queue:" + batch.plan.members[0].thread_id,
                "thread-queue:" + target,
            }
        )
        fences = {}

        async def locked(index):
            if index == len(responsibilities):
                return await asyncio.to_thread(
                    self._recover,
                    batch_id,
                    request,
                    thread_id,
                    project,
                    actor,
                    target,
                    fences,
                )
            name = responsibilities[index]

            async def operation():
                fences[name] = self.ownership.require_fence(name).fencing_token
                return await locked(index + 1)

            owned, result = await self.ownership.run_exclusive(name, operation)
            if not owned:
                raise HTTPException(
                    status_code=409,
                    detail="Another queue owner is active; reload recovery state.",
                )
            return result

        try:
            return await locked(0)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "notification_batch_recovery_conflict",
                    "message": str(exc),
                },
            ) from exc

    def _recover(self, batch_id, request, thread_id, project, actor, target, fences):
        batch = self._get(batch_id, thread_id, project, actor)
        decision = self._authority(batch, project, actor)
        decision_json = self._decision_json(decision)
        restored = []
        for member in batch.plan.members:
            current = member.model_copy(deep=True)
            current.thread_id = target
            if (
                current.reply_target
                and current.reply_target.thread_id == member.thread_id
            ):
                current.reply_target.thread_id = target
            restored.append(current)
        recovery = NotificationBatchRecovery(
            id="notification-recovery-" + uuid.uuid4().hex,
            source_admission_sha256=request.expected_admission_sha256,
            requested_by=actor.identity_id,
            reason=request.reason,
            requested_at=self.clock(),
            target_thread_id=target,
            restoration_snapshots_json=tuple(
                member.model_dump_json() for member in restored
            ),
            authority_decision_json=decision_json,
        )

        def current():
            for name, token in fences.items():
                if self.ownership.require_fence(name).fencing_token != token:
                    return False
            fresh_project = self.projects.get(project.id)
            return (
                fresh_project == project
                and self._target(batch, project) == target
                and self._decision_json(self._authority(batch, project, actor))
                == decision_json
            )

        admitted = self.preflight.reserve_notification_recovery(
            batch_id,
            recovery,
            expected_admission_sha256=request.expected_admission_sha256,
            guard=current,
        )
        if admitted.recovery.state == "released":
            return {
                "ok": True,
                "batch_id": batch_id,
                "threadId": target,
                "state": "released",
                "native_started": False,
            }

        def guarded():
            observed = self.preflight.get_notification_batch(batch_id)
            return current() and observed == admitted

        if target != batch.plan.members[0].thread_id:
            self.queues.remove_present_originals_exact(
                batch.plan.members[0].thread_id, batch.plan.members, guard=guarded
            )
        priorities = frozenset(
            member.id
            for member in self.queues.get(target)
            if TurnQueuePolicy.admission_priority(member.source) == "human_slack"
        )
        self.queues.restore_many_exact(
            target,
            tuple(restored),
            order=batch.queue_order,
            guard=guarded,
            priority_ids=priorities,
        )
        released = self.preflight.release_notification_recovery(
            batch_id, recovery_id=admitted.recovery.id, now=self.clock(), guard=guarded
        )
        return {
            "ok": True,
            "batch_id": batch_id,
            "threadId": target,
            "state": released.recovery.state,
            "native_started": False,
        }
