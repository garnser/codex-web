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
        if observed != admission or observed.phase != "prepared":
            return False
        for attempt_id, queued, original in zip(admission.attempt_ids, admission.plan.members,
                                                admission.plan.original_snapshots_json, strict=True):
            member = self.preflight.get(attempt_id)
            if (member is None or member.notification_batch_id != admission.id
                    or member.queued_admission_claim_id != admission.claim_id or member.status != "admitting"):
                return False
            checked = member.model_copy(update={"status":"queued", "notification_batch_id":None})
            if _eligible(queued,checked,organization_id=admission.plan.organization_id,
                         workspace_id=admission.plan.workspace_id,
                         notification_source_origin=TurnQueuePolicy.source_origin(queued.source)) != original:
                return False
        return True

    def prepare(self, plan, *, claim_id, current):
        """Retain ownership first, then guarded all-or-none scheduling removal."""
        if not current():
            raise ValueError("notification batch authority/ownership changed")
        thread = plan.members[0].thread_id
        order = tuple(item.id for item in self.queue.get(thread))
        admission = self.preflight.claim_notification_batch(plan, claim_id=claim_id,
            now=self.clock(), queue_order=order)
        result = self.queue.remove_many_exact(thread, plan.members,
            guard=lambda:self._guard(admission,current))
        if result != "removed":
            # Exact changed copies cannot be overwritten by a rollback. The
            # retained prepared claim remains inspectable for guarded recovery.
            raise ValueError("notification batch queue changed; prepared recovery required")
        return admission

    def recover_prepared(self, batch_id, *, claim_id, current, priority_ids=frozenset()):
        admission = self.preflight.get_notification_batch(batch_id)
        if (admission is None or admission.claim_id != claim_id or admission.phase != "prepared"
                or not admission.queue_order):
            raise ValueError("notification batch cannot use prepared recovery")
        self.queue.restore_many_exact(admission.plan.members[0].thread_id, admission.plan.members,
            order=admission.queue_order, priority_ids=priority_ids,
            guard=lambda:self._guard(admission,current))
        # A crash here leaves exact restored queue copies and batch-bound claims;
        # another authorized invocation may repeat restoration idempotently.
        if not self._guard(admission,current):
            raise ValueError("notification batch was fenced after queue restoration")
        return self.preflight.transition_notification_batch(batch_id,claim_id=claim_id,phase="released")
