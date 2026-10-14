"""Existing-domain scheduling coordinator; native I/O is deliberately separate."""

import time
from codex_web.notification_batches import _eligible
from codex_web.services.turn_queue_policy import TurnQueuePolicy


class NotificationBatchCoordinator:
    def __init__(self, *, queue, preflight, clock=time.time):
        self.queue, self.preflight, self.clock = queue, preflight, clock

    def _guard(self, admission, current):
        if not current():
            return False
        observed = self.preflight.get_notification_batch(admission.id)
        if observed != admission or observed.phase not in {
            "prepared",
            "prestart_refused",
        }:
            return False
        for attempt_id, queued, original in zip(
            admission.attempt_ids,
            admission.plan.members,
            admission.plan.original_snapshots_json,
            strict=True,
        ):
            member = self.preflight.get(attempt_id)
            if (
                member is None
                or member.notification_batch_id != admission.id
                or member.queued_admission_claim_id != admission.claim_id
                or member.status
                != (
                    "reconciliation_required"
                    if admission.phase == "prestart_refused"
                    else "admitting"
                )
            ):
                return False
            checked = member.model_copy(
                update={"status": "queued", "notification_batch_id": None}
            )
            if (
                _eligible(
                    queued,
                    checked,
                    organization_id=admission.plan.organization_id,
                    workspace_id=admission.plan.workspace_id,
                    notification_source_origin=TurnQueuePolicy.source_origin(
                        queued.source
                    ),
                )
                != original
            ):
                return False
        return True

    def prepare(self, plan, *, claim_id, current):
        """Retain ownership first, then guarded all-or-none scheduling removal."""
        if not current():
            raise ValueError("notification batch authority/ownership changed")
        thread = plan.members[0].thread_id
        order = tuple(item.id for item in self.queue.get(thread))
        admission = self.preflight.claim_notification_batch(
            plan, claim_id=claim_id, now=self.clock(), queue_order=order
        )
        result = self.queue.remove_many_exact(
            thread, plan.members, guard=lambda: self._guard(admission, current)
        )
        if result != "removed":
            # Exact changed copies cannot be overwritten by a rollback. The
            # retained prepared claim remains inspectable for guarded recovery.
            raise ValueError(
                "notification batch queue changed; prepared recovery required"
            )
        return admission

    def recover_prepared(
        self, batch_id, *, claim_id, current, priority_ids=frozenset()
    ):
        admission = self.preflight.get_notification_batch(batch_id)
        if (
            admission is None
            or admission.claim_id != claim_id
            or admission.phase not in {"prepared", "prestart_refused"}
            or not admission.queue_order
        ):
            raise ValueError("notification batch cannot use prepared recovery")
        self.queue.restore_many_exact(
            admission.plan.members[0].thread_id,
            admission.plan.members,
            order=admission.queue_order,
            priority_ids=priority_ids,
            guard=lambda: self._guard(admission, current),
        )
        # A crash here leaves exact restored queue copies and batch-bound claims;
        # another authorized invocation may repeat restoration idempotently.
        if not self._guard(admission, current):
            raise ValueError("notification batch was fenced after queue restoration")
        return self.preflight.transition_notification_batch(
            batch_id, claim_id=claim_id, phase="released"
        )

    async def submit_native(
        self, admission, *, runtime, project, actor, current, native_current=None
    ):
        if admission.phase != "prepared" or not self._guard(admission, current):
            raise ValueError("notification batch authority/ownership changed")
        member = admission.plan.members[0]
        if (
            project.id != member.project_id
            or project.organization_id != admission.plan.organization_id
            or project.workspace_id != admission.plan.workspace_id
            or actor.identity_id != member.agent_profile_actor_id
            or actor.organization_id != admission.plan.organization_id
            or actor.workspace_id != admission.plan.workspace_id
        ):
            raise ValueError("notification batch actor/Project scope differs")
        return await runtime.start_thread_turn_now(
            member.thread_id,
            project=project,
            message=admission.native_input(),
            sandbox=member.sandbox or project.sandbox,
            approval_policy=member.approval_policy or project.approval_policy,
            model=member.model,
            reasoning_effort=member.reasoning_effort,
            source="queued:" + member.source,
            reply_target=member.reply_target,
            execution_id=admission.id,
            work_item_ref=member.work_item_ref,
            repository_resource_id=member.repository_resource_id,
            writable_repository_resource_ids=member.writable_repository_resource_ids,
            read_only_repository_resource_ids=member.read_only_repository_resource_ids,
            execution_profile_id=member.execution_profile_id,
            actor=actor,
            agent_profile_id=member.agent_profile_id,
            agent_profile_revision=member.agent_profile_revision,
            agent_profile_actor_id=member.agent_profile_actor_id,
            **(
                {"memory_context_binding": member.memory_context_binding}
                if member.memory_context_binding is not None
                else {}
            ),
            queued_admission_claim_id=admission.claim_id,
            notification_batch_admission_id=admission.id,
            notification_batch_control_current=native_current or current,
        )
