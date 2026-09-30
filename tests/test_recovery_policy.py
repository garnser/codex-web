from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace as NS

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.api.recovery import build_recovery_router
from codex_web.compatibility import ContractCompatibilityError
from codex_web.crypto import ManagedKeyCreate
from codex_web.identity import AuthenticationAssurance, MembershipRole
from codex_web.scheduler import ScheduleStatus
from codex_web.services.identity import AuthorizationError
from codex_web.services.recovery import RecoveryConflictError
from codex_web.services.recovery_policy import RecoveryPolicyControl
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.recovery import RecoveryStore
from codex_web.storage.scheduler import SchedulerStore


class RecoveryPolicyTests(unittest.TestCase):
    def setUp(self):
        from tests.test_recovery import RecoveryTests
        RecoveryTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.control = RecoveryPolicyControl(self.service)

    def test_policy_changes_are_scoped_audited_and_stale_update_is_rejected(self):
        original = self.policy.fingerprint()
        updated = self.policy.model_copy(update={'retention_count': 3, 'version': '2'})
        self.service.configure(updated, actor=self.actor, expected_fingerprint=original)
        state = self.service.store.load()
        self.assertEqual(len(state.policy_changes), 2)
        self.assertEqual(state.policy_changes[-1].actor_id, self.actor.identity_id)
        self.assertEqual(state.policy_changes[-1].previous_fingerprint, original)
        with self.assertRaises(RecoveryConflictError):
            self.service.configure(self.policy, actor=self.actor, expected_fingerprint=original)
        self.assertEqual(self.service.policy(), updated)
        with self.assertRaises(AuthorizationError):
            self.control.require_scope(state, self.actor.model_copy(update={'workspace_id': 'foreign'}))

    def test_policy_rollback_creates_new_provenance_and_does_not_change_backups(self):
        backup = self.service.create_backup(actor=self.actor)
        first = self.service.store.load().policy_changes[0]
        changed = self.policy.model_copy(update={'retention_count': 1, 'version': '2'})
        self.service.configure(changed, actor=self.actor, expected_fingerprint=self.policy.fingerprint())
        restored = self.control.rollback(first.id, actor=self.actor, expected_fingerprint=changed.fingerprint())
        state = self.service.store.load()
        self.assertEqual(restored, first.policy)
        self.assertEqual(state.policy_changes[-1].restored_from_id, first.id)
        self.assertEqual(state.backups[backup.id], backup)
        with self.assertRaises(RecoveryConflictError):
            self.control.rollback(first.id, actor=self.actor, expected_fingerprint=changed.fingerprint())

    def test_policy_rollback_cannot_restore_revoked_key_authority(self):
        first = self.service.store.load().policy_changes[0]
        replacement = self.crypto.create_key(ManagedKeyCreate(purpose='backup'), actor=self.actor)
        changed = self.policy.model_copy(update={'backup_key_id': replacement.id})
        self.service.configure(changed, actor=self.actor)
        self.crypto.revoke_key(self.backup_key.id, 'unused key retired', actor=self.actor)
        with self.assertRaises(RecoveryConflictError):
            self.control.rollback(first.id, actor=self.actor, expected_fingerprint=changed.fingerprint())
        self.assertEqual(self.service.policy(), changed)

    def test_scheduler_replacement_is_atomic_and_stale_events_are_fenced(self):
        self.service.scheduler = SchedulerService(SchedulerStore(self.state), NS(), clock=self.clock)
        self.service.configure(self.policy, actor=self.actor)
        old = self.service.store.load().backup_schedule_id
        self.service.scheduler.pause(old, actor_id=self.actor.identity_id)
        changed = self.policy.model_copy(update={'backup_interval_seconds': 600})
        self.service.configure(changed, actor=self.actor)
        state = self.service.store.load(); scheduler = self.service.scheduler
        self.assertNotEqual(state.backup_schedule_id, old)
        self.assertEqual(scheduler.get(old).status, ScheduleStatus.CANCELLED)
        self.assertIsNone(scheduler.get(old).lease_owner)
        current = scheduler.get(state.backup_schedule_id)
        self.assertEqual(current.status, ScheduleStatus.PAUSED)
        self.assertEqual(current.recurrence.interval_seconds, 600)
        self.service.service_actor = self.actor
        calls = []; self.service.create_backup = lambda **_: calls.append('backup')
        asyncio.run(self.service._handle_schedule_event(NS(tenant_id=self.actor.organization_id, workspace_id=self.actor.workspace_id, payload={'trigger_type': self.service.BACKUP_TRIGGER, 'schedule_id': old})))
        self.assertEqual(calls, [])
        asyncio.run(self.service._handle_schedule_event(NS(tenant_id=self.actor.organization_id, workspace_id=self.actor.workspace_id, payload={'trigger_type': self.service.BACKUP_TRIGGER, 'schedule_id': current.id, 'payload': {'policy_fingerprint': 'stale'}})))
        self.assertEqual(calls, [])
        scheduler.resume(current.id, actor_id=self.actor.identity_id)
        asyncio.run(self.service._handle_schedule_event(NS(tenant_id=self.actor.organization_id, workspace_id=self.actor.workspace_id, payload={'trigger_type': self.service.BACKUP_TRIGGER, 'schedule_id': current.id, 'payload': current.payload})))
        self.assertEqual(calls, ['backup'])

    def test_legacy_scope_is_derived_from_key_and_future_schema_is_rejected(self):
        raw = self.service.store.load().model_dump(mode='json')
        for field in ['policy_changes', 'policy_organization_id', 'policy_workspace_id']:
            raw.pop(field)
        raw['schema_version'] = '1.0'
        self.state.update('recovery', lambda _: raw, default={})
        self.control.require_scope(self.service.store.load(), self.actor)
        self.service.configure(self.policy, actor=self.actor)
        self.assertEqual(self.service.store.load().schema_version, '1.1')
        self.assertEqual(self.service.store.load().policy_organization_id, self.actor.organization_id)
        with self.assertRaises(ContractCompatibilityError):
            RecoveryStore._decode({'schema_version': '9.0'})

    def test_policy_api_permissions_validation_preview_cas_and_rollback(self):
        app = FastAPI()
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_recovery_router(self.service))
        with TestClient(app) as client:
            status = client.get('/api/recovery/status').json()
            revision = status['policy_control']['history'][0]['id']
            proposed = self.policy.model_dump(mode='json'); proposed['retention_count'] = 3
            preview = client.post('/api/recovery/policy/preview', json=proposed)
            self.assertEqual(preview.status_code, 200)
            expected = preview.json()['expected_fingerprint']
            self.assertEqual(preview.json()['changed_fields'], ['retention_count'])
            self.assertEqual(client.put('/api/recovery/policy?expected_fingerprint=' + expected, json=proposed).status_code, 200)
            self.assertEqual(client.put('/api/recovery/policy?expected_fingerprint=' + expected, json=proposed).status_code, 409)
            self.assertEqual(client.post('/api/recovery/policy/preview', json={**proposed, 'retention_count': 0}).status_code, 422)
            current = self.service.policy().fingerprint()
            self.assertEqual(client.post('/api/recovery/policy/rollback/' + revision, json={'expected_fingerprint': current}).status_code, 200)
            self.actor = self.actor.model_copy(update={'assurance': AuthenticationAssurance.PRIMARY})
            self.assertFalse(client.get('/api/recovery/status').json()['policy_control']['can_configure'])
            self.assertEqual(client.put('/api/recovery/policy', json=proposed).status_code, 403)
            self.actor = self.actor.model_copy(update={'roles': (MembershipRole.MEMBER,)})
            self.assertEqual(client.post('/api/recovery/policy/preview', json=proposed).status_code, 403)
            self.actor = self.actor.model_copy(update={'workspace_id': 'foreign'})
            self.assertEqual(client.get('/api/recovery/status').status_code, 403)

    def test_retention_does_not_delete_another_tenants_backup(self):
        backup = self.service.create_backup(actor=self.actor)
        foreign_ref = self.destination.put('foreign-fixture', b'encrypted-fixture')
        foreign = backup.model_copy(update={'id': 'foreign-backup', 'organization_id': 'foreign', 'created_at': 0, 'destination_ref': foreign_ref})
        self.service.store.update(lambda state: state.model_copy(update={'backups': {backup.id: backup, foreign.id: foreign}}))
        self.service._enforce_retention(self.policy.model_copy(update={'retention_count': 1}), actor=self.actor)
        self.assertIn(foreign.id, self.service.store.load().backups)
        self.assertEqual(self.destination.get(foreign_ref), b'encrypted-fixture')

    def test_unsupported_capabilities_and_project_keys_fail_before_policy_changes(self):
        for update in [{'id': 'renamed'}, {'allow_point_in_time_recovery': True}, {'require_key_manifest': False}]:
            with self.assertRaises(RecoveryConflictError):
                self.service.configure(self.policy.model_copy(update=update), actor=self.actor)
        scoped = self.crypto.create_key(ManagedKeyCreate(purpose='backup', project_id='project-a'), actor=self.actor)
        with self.assertRaises(RecoveryConflictError):
            self.service.configure(self.policy.model_copy(update={'backup_key_id': scoped.id}), actor=self.actor)
        self.assertEqual(self.service.policy(), self.policy)

    def test_invalid_project_context_rejects_reads_previews_and_updates(self):
        from codex_web.models import Project
        from codex_web.services.projects import ProjectService
        projects = [Project(id='foreign', name='Foreign', path='/foreign', organization_id='foreign', workspace_id='foreign')]
        app = FastAPI()
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_recovery_router(self.service, ProjectService(NS(load=lambda: projects))))
        with TestClient(app) as client:
            for project in ['foreign', 'missing', '']:
                query = '?project_id=' + project
                self.assertEqual(client.get('/api/recovery/status' + query).status_code, 404)
                self.assertEqual(client.post('/api/recovery/policy/preview' + query, json=self.policy.model_dump(mode='json')).status_code, 404)
                self.assertEqual(client.put('/api/recovery/policy' + query, json=self.policy.model_dump(mode='json')).status_code, 404)
        self.assertEqual(len(self.service.store.load().policy_changes), 1)

    def test_backup_restores_policy_history_without_recursive_manifests(self):
        from codex_web.storage.sqlite_state import SQLiteStateStore
        from pathlib import Path
        unused = self.crypto.create_key(ManagedKeyCreate(purpose='backup'), actor=self.actor)
        self.crypto.revoke_key(unused.id, 'unused reference retired', actor=self.actor)
        first = self.service.create_backup(actor=self.actor)
        self.assertNotIn(unused.id, [entry.key_id for entry in first.key_manifest])
        self.clock.value += 1
        second = self.service.create_backup(actor=self.actor)
        target = SQLiteStateStore(Path(self.temp.name) / 'policy-restored.sqlite3')
        result = self.service.restore_into_isolated(second.id, target, actor=self.actor)
        self.assertEqual(result.status.value, 'pass')
        restored = RecoveryStore(target).load()
        self.assertEqual(restored.policy, self.policy)
        self.assertEqual(len(restored.policy_changes), 1)
        self.assertEqual(restored.backups, {})
        self.assertEqual(restored.verifications, {})

    def test_legacy_adoption_uses_the_transaction_key_snapshot(self):
        from unittest.mock import patch
        state = self.service.store.load().model_copy(update={'policy_organization_id': None, 'policy_workspace_id': None, 'policy_changes': ()})
        self.service.store.update(lambda _: state)
        # get_key performs one authorized read before the transaction; ownership
        # inside the transaction must use its locked key document, not a nested
        # StateStore connection (which can exhaust a one-connection pool).
        original = self.crypto.get_key
        def authorized_key(*args, **kwargs):
            result = original(*args, **kwargs)
            guard.start()
            return result
        guard = patch.object(self.crypto.store, 'load', side_effect=AssertionError('nested key read'))
        try:
            with patch.object(self.crypto, 'get_key', side_effect=authorized_key):
                self.service.configure(self.policy, actor=self.actor)
        finally:
            guard.stop()
        self.assertEqual(self.service.store.load().policy_organization_id, self.actor.organization_id)
