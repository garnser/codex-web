from __future__ import annotations

from codex_web.crypto import KeyManifestEntry, KeyPurpose
from codex_web.recovery import RecoveryPolicyChange
from codex_web.scheduler import MisfirePolicy, RecurrenceKind, ScheduleCreate, ScheduleRecord, ScheduleRecurrence, ScheduleStatus
from codex_web.services.identity import AuthorizationError
from codex_web.services.recovery import RecoveryConflictError


class RecoveryPolicyControl:
    """Policy changes extend RecoveryState; no independent policy or audit store."""

    def __init__(self, service):
        self.service = service

    def require_scope(self, state, actor, *, key_records=None):
        scope = (state.policy_organization_id, state.policy_workspace_id)
        if state.policy and scope == (None, None):
            # Legacy ownership is derived from the existing canonical key. Never
            # infer another tenant's ownership from a request or a display label.
            records = self.service.crypto.store.load().keys if key_records is None else key_records
            key = next((item for item in records if item.id == state.policy.backup_key_id), None)
            if key is None:
                raise RecoveryConflictError('Legacy policy ownership cannot be established; key metadata recovery is required')
            scope = (key.scope.organization_id, key.scope.workspace_id)
        if scope != (None, None) and scope != (actor.organization_id, actor.workspace_id):
            raise AuthorizationError('Recovery policy is unavailable in this tenant')

    @staticmethod
    def validate_changes(state, policy):
        if state.policy and state.policy.id != policy.id:
            raise RecoveryConflictError('Recovery policy identity is immutable')
        for name in ('require_key_manifest', 'allow_point_in_time_recovery'):
            previous = getattr(state.policy, name) if state.policy else type(policy).model_fields[name].default
            if getattr(policy, name) != previous:
                raise RecoveryConflictError(f'{name} is not a supported mutable runtime capability')

    @staticmethod
    def validate_backup_key(key):
        if key.purpose != KeyPurpose.BACKUP or key.status.value != 'active':
            raise RecoveryConflictError('An active backup key is required')
        if key.scope.project_id is not None or key.scope.resource_id is not None:
            raise RecoveryConflictError('Deployment recovery requires a workspace-scoped backup key')

    def configure(self, policy, *, actor, expected_fingerprint=None, restored_from_id=None):
        service = self.service
        key = service.crypto.get_key(policy.backup_key_id, actor)
        self.validate_backup_key(key)
        service._destination(policy.destination_id)
        if service.service_actor is not None and service.service_actor.tenant != actor.tenant:
            raise AuthorizationError('Recovery execution identity belongs to another tenant')
        changed = False
        now = float(service.clock())

        def apply(state, keys):
            nonlocal changed
            self.require_scope(state, actor, key_records=keys.keys)
            self.validate_changes(state, policy)
            current = state.policy.fingerprint() if state.policy else 'none'
            if expected_fingerprint is not None and current != expected_fingerprint:
                raise RecoveryConflictError('Recovery policy changed; reload and review its current impact')
            if restored_from_id and not any(item.id == restored_from_id and item.policy == policy for item in state.policy_changes):
                raise RecoveryConflictError('Recovery policy revision is unavailable')
            schedules_exist = service.scheduler is None or (state.backup_schedule_id and state.verification_schedule_id)
            if current == policy.fingerprint() and state.policy_organization_id and state.policy_changes and schedules_exist:
                return state
            changed = True
            state.policy_changes += (RecoveryPolicyChange(
                organization_id=actor.organization_id, workspace_id=actor.workspace_id,
                actor_id=actor.identity_id, occurred_at=now,
                previous_fingerprint=None if current == 'none' else current,
                policy=policy, restored_from_id=restored_from_id,
            ),)
            state.policy = policy
            state.policy_organization_id = actor.organization_id
            state.policy_workspace_id = actor.workspace_id
            return state

        def schedules(state, scheduler_state):
            if not changed and all(record_id in scheduler_state.schedules for record_id in (state.backup_schedule_id, state.verification_schedule_id)):
                return
            for field, trigger, interval in [
                ('backup_schedule_id', service.BACKUP_TRIGGER, policy.backup_interval_seconds),
                ('verification_schedule_id', service.VERIFY_TRIGGER, policy.restore_verification_interval_seconds),
            ]:
                previous = scheduler_state.schedules.get(getattr(state, field))
                paused = previous is not None and previous.status in (ScheduleStatus.PAUSED, ScheduleStatus.CANCELLED)
                if previous is not None:
                    scheduler_state.schedules[previous.id] = previous.model_copy(update={
                        'status': ScheduleStatus.CANCELLED, 'lease_owner': None, 'lease_expires_at': None,
                        'revision': previous.revision + 1, 'updated_at': now, 'updated_by': actor.identity_id,
                    })
                record = ScheduleRecord.from_create(ScheduleCreate(
                    name='recovery-backup' if field == 'backup_schedule_id' else 'recovery-restore-verification',
                    tenant_id=actor.organization_id, workspace_id=actor.workspace_id,
                    trigger_type=trigger, payload={'policy_fingerprint': policy.fingerprint()},
                    due_at=now + interval, recurrence=ScheduleRecurrence(kind=RecurrenceKind.INTERVAL, interval_seconds=interval),
                    misfire_policy=MisfirePolicy.FIRE_ONCE,
                ), actor_id=actor.identity_id, now=now)
                if paused:
                    record = record.model_copy(update={'status': ScheduleStatus.PAUSED})
                scheduler_state.schedules[record.id] = record
                setattr(state, field, record.id)

        state = service._write_key_bound(apply, actor=actor, active_key=key.id,
            requirements=(KeyManifestEntry(key_id=key.id, versions=(key.current_version,)),),
            scheduler_update=schedules if service.scheduler else None)
        if service.scheduler:
            service.scheduler.notify_state_changed()
        return state.policy

    def rollback(self, revision_id, *, actor, expected_fingerprint):
        state = self.service.store.load()
        self.require_scope(state, actor)
        revision = next((row for row in state.policy_changes if row.id == revision_id), None)
        if revision is None:
            raise RecoveryConflictError('Recovery policy revision is unavailable')
        return self.configure(revision.policy, actor=actor, expected_fingerprint=expected_fingerprint,
                              restored_from_id=revision.id)

    def preview(self, policy, *, actor):
        state = self.service.store.load()
        self.require_scope(state, actor)
        key = self.service.crypto.get_key(policy.backup_key_id, actor)
        self.validate_backup_key(key)
        self.validate_changes(state, policy)
        self.service._destination(policy.destination_id)
        before = state.policy.model_dump(mode='json') if state.policy else {}
        after = policy.model_dump(mode='json')
        backups = [row for row in state.backups.values()
                   if (row.organization_id, row.workspace_id) == (actor.organization_id, actor.workspace_id)]
        return {
            'available': True, 'schema_version': '1.0',
            'expected_fingerprint': state.policy.fingerprint() if state.policy else 'none',
            'proposed_fingerprint': policy.fingerprint(),
            'organization_id': actor.organization_id, 'workspace_id': actor.workspace_id,
            'changed_fields': [name for name in after if before.get(name) != after[name]],
            'retained_backup_count': len(backups),
            'backups_over_new_retention': max(0, len(backups) - policy.retention_count),
            'schedule_effect': 'Canonical timers are replaced atomically; paused timers remain paused. In-flight stale claims/events are fenced.' if self.service.scheduler else 'No scheduler is attached; automatic backup/verification is unavailable.',
            'application': 'Policy applies to subsequent operations immediately. Existing backup bytes and verification evidence remain unchanged by this policy change.',
            'rollback_limit': 'Rollback restores a retained policy revision only. It cannot recover expired backup bytes, undo completed operations, or restore revoked key material.',
        }
