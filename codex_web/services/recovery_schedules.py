"""Transactional schedule admission in the existing RecoveryState domain.

These receipts are replay barriers, not a second queue or scheduler. A running
receipt from a lost process cannot be reclaimed on a timeout: that does not
prove its actual backup/verification operation has stopped.
"""
from __future__ import annotations

import hashlib
import math

from codex_web.canonical_events import CanonicalEventType
from codex_web.crypto import KeyVersionStatus
from codex_web.recovery import RecoveryFiring, RecoveryFiringWatermark
from codex_web.scheduler import MisfirePolicy, ScheduleStatus
from codex_web.services.recovery import RecoveryConflictError
from codex_web.services.recovery_executor import RecoveryExecutorBusy
from codex_web.services.recovery_policy import RecoveryPolicyControl


class RecoveryScheduleAdmission:
    def __init__(self, service, owner_id: str):
        self.service = service
        self.owner_id = owner_id

    def validate_policy(self, state, keys, firing):
        service = self.service
        actor = service.service_actor
        if (actor is None or (actor.organization_id, actor.workspace_id, actor.identity_id)
                != (firing.organization_id, firing.workspace_id, firing.actor_id)
                or firing.owner_id != self.owner_id
                or state.scheduled_firings.get(firing.event_id) != firing
                or firing.status != "running"
                or state.policy is None or state.policy.fingerprint() != firing.policy_fingerprint):
            raise RecoveryConflictError("Scheduled recovery authority or policy changed")
        service.crypto._require_admin(actor)
        RecoveryPolicyControl(service).require_scope(state, actor, key_records=keys.keys)
        key = service.crypto._key(keys, state.policy.backup_key_id, actor)
        RecoveryPolicyControl.validate_backup_key(key)
        if service.crypto._version(key, key.current_version).status != KeyVersionStatus.ACTIVE:
            raise RecoveryConflictError("Scheduled recovery key version changed")

    def validate_schedule(self, state, schedules, firing):
        service = self.service
        expected = (state.backup_schedule_id if firing.trigger_type == service.BACKUP_TRIGGER
                    else state.verification_schedule_id)
        record = schedules.schedules.get(expected)
        if (record is None or expected != firing.schedule_id or record.status != ScheduleStatus.ACTIVE
                or (record.tenant_id, record.workspace_id) != (firing.organization_id, firing.workspace_id)
                or record.trigger_type != firing.trigger_type
                or record.misfire_policy != MisfirePolicy.FIRE_ONCE
                or record.payload != {"policy_fingerprint": firing.policy_fingerprint}):
            raise RecoveryConflictError("Scheduled recovery schedule changed")

    def checkpoint(self, firing):
        return self.service._write_key_bound(lambda state, keys: state,
            actor=self.service.service_actor, requirements=(), _firing=firing)

    def admit(self, event) -> RecoveryFiring | None:
        service = self.service
        actor = service.service_actor
        if actor is None or service.scheduler is None or service.canonical_events is None:
            return None
        # Only an actual immutable canonical envelope may create a receipt.
        stored = service.canonical_events.bus.store.event(event.event_id)
        if stored is None or stored != event:
            return None
        if event.event_type != CanonicalEventType.SCHEDULE or (
            event.tenant_id, event.workspace_id
        ) != (actor.organization_id, actor.workspace_id):
            return None
        trigger = event.payload.get("trigger_type")
        if trigger not in (service.BACKUP_TRIGGER, service.VERIFY_TRIGGER):
            return None
        scheduled_for = event.payload.get("scheduled_for")
        if isinstance(scheduled_for, bool) or not isinstance(scheduled_for, (int, float)):
            return None
        if not math.isfinite(scheduled_for) or not math.isfinite(event.occurred_at):
            return None
        schedule_id = event.payload.get("schedule_id")
        if (event.source != f"scheduler:{schedule_id}" or event.event_id != service.canonical_events._event_id(
                event.source, f"{schedule_id}:{float(scheduled_for)!r}")):
            return None
        selected = None

        def policy(state, keys):
            # Check authority against the same locked key document as policy
            # scope. No nested connection or implicit service-scope widening.
            service.crypto._require_admin(actor)
            RecoveryPolicyControl(service).require_scope(state, actor, key_records=keys.keys)
            if state.policy is not None:
                key = service.crypto._key(keys, state.policy.backup_key_id, actor)
                RecoveryPolicyControl.validate_backup_key(key)
                if service.crypto._version(key, key.current_version).status != KeyVersionStatus.ACTIVE:
                    raise RecoveryConflictError("Scheduled recovery requires an active key version")
            return state

        def schedule(state, schedules):
            nonlocal selected
            if state.policy is None:
                return
            fingerprint = state.policy.fingerprint()
            schedule_id = (state.backup_schedule_id if trigger == service.BACKUP_TRIGGER
                           else state.verification_schedule_id)
            record = schedules.schedules.get(schedule_id)
            if record is None or record.status != ScheduleStatus.ACTIVE:
                return
            if (record.tenant_id, record.workspace_id) != (actor.organization_id, actor.workspace_id):
                return
            if (event.source != f"scheduler:{record.id}"
                    or event.payload.get("schedule_id") != record.id
                    or event.payload.get("schedule_name") != record.name
                    or record.trigger_type != trigger
                    or record.misfire_policy != MisfirePolicy.FIRE_ONCE
                    or event.payload.get("misfire_policy") != MisfirePolicy.FIRE_ONCE.value
                    or record.payload != {"policy_fingerprint": fingerprint}
                    or event.payload.get("payload") != record.payload
                    or not record.due_at <= scheduled_for <= event.occurred_at):
                return
            if event.event_id in state.scheduled_firings:
                return
            watermark = state.scheduled_watermarks.get(trigger)
            if watermark is not None and watermark.schedule_id == record.id and scheduled_for <= watermark.scheduled_for:
                # FIRE_ONCE coalesces older occurrences. The watermark retains
                # the replay barrier after successful receipt compaction.
                return
            if any(item.status == "running" for item in state.scheduled_firings.values()):
                raise RecoveryExecutorBusy("A canonical recovery firing still owns actual work")
            candidates = [item for item in state.backups.values()
                          if (item.organization_id, item.workspace_id) == (actor.organization_id, actor.workspace_id)]
            backup_id = None
            if trigger == service.VERIFY_TRIGGER:
                if not candidates:
                    return
                backup_id = max(candidates, key=lambda item: (item.created_at, item.id)).id
            # Retain uncertainty. Only successful receipts may be compacted;
            # their current-schedule watermark (or cancelled old schedule)
            # continues to reject redelivery. Never evict a running receipt.
            if len(state.scheduled_firings) >= 128:
                completed = sorted((item for item in state.scheduled_firings.values()
                                    if item.status == "succeeded"),
                                   key=lambda item: (item.admitted_at, item.event_id))
                if not completed:
                    raise RecoveryExecutorBusy("Recovery firing receipts require reconciliation")
                del state.scheduled_firings[completed[0].event_id]
            digest = hashlib.sha256(
                f"{actor.organization_id}:{actor.workspace_id}:{event.event_id}:{trigger}:{fingerprint}".encode()
            ).hexdigest()[:32]
            selected = RecoveryFiring(
                event_id=event.event_id, schedule_id=record.id, trigger_type=trigger,
                scheduled_for=scheduled_for, organization_id=actor.organization_id,
                workspace_id=actor.workspace_id, actor_id=actor.identity_id,
                policy_fingerprint=fingerprint, owner_id=self.owner_id,
                effect_id=f"{'backup' if trigger == service.BACKUP_TRIGGER else 'restore-verification'}-{digest}",
                backup_id=backup_id, admitted_at=float(service.clock()),
            )
            state.scheduled_firings[event.event_id] = selected
            state.scheduled_watermarks[trigger] = RecoveryFiringWatermark(
                schedule_id=record.id, scheduled_for=scheduled_for)

        service._write_key_bound(policy, actor=actor, requirements=(), scheduler_update=schedule)
        return selected

    def finish(self, firing: RecoveryFiring, *, succeeded: bool, error_code: str | None = None):
        if firing.owner_id != self.owner_id:
            raise RecoveryConflictError("Recovery firing belongs to another execution owner")

        def apply(state):
            current = state.scheduled_firings.get(firing.event_id)
            # Atomic verification/Evidence settlement may have committed before
            # the observer lost its response. Never overwrite that terminal fact.
            if current is not None and current.status == "succeeded" and current.model_copy(
                    update={"status": firing.status, "finished_at": firing.finished_at,
                            "error_code": firing.error_code}) == firing:
                return state
            if current != firing or current.status != "running":
                raise RecoveryConflictError("Recovery firing custody changed")
            state.scheduled_firings[firing.event_id] = current.model_copy(update={
                "status": "succeeded" if succeeded else "unknown",
                "finished_at": float(self.service.clock()),
                "error_code": error_code,
            })
            return state

        # Only the owned worker calls this after the operation is terminal.
        # Revocation must not prevent it from recording an unknown result.
        self.service.store.update(apply)
