"""Actual separate-connection denials at the irreversible domain-write boundary."""
import asyncio
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_action_intents as intent_fixtures
from tests import test_recovery_operator_api as api
from tests.test_recovery_scheduled_postgres import _PostgresFixture
from tests import test_recovery_operator_postgres as pg
from codex_web.services.identity import IdentityService
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.services.action_providers import ActionProviderRegistry
from codex_web.storage.action_providers import ActionProviderStateStore


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL test DSN required')
class MutationAdmissionPostgresTests(_PostgresFixture, unittest.IsolatedAsyncioTestCase):
    client = api.RecoveryOperatorApiTests.client
    url = api.RecoveryOperatorApiTests.url
    body = api.RecoveryOperatorApiTests.body
    asyncTearDown = api.RecoveryOperatorApiTests.asyncTearDown
    writer_store = pg.PostgresRecoveryOperatorTests.writer_store

    async def asyncSetUp(self):
        with patch.object(intent_fixtures, 'SQLiteStateStore', self.state_store_factory):
            await api.RecoveryOperatorApiTests.asyncSetUp(self)
        self.addCleanup(self.sqlite.close)

    async def denied_at_write(self, change, *, stepped_session=False):
        async with self.client() as client:
            identity = IdentityService(IdentityStateStore(self.writer_store()))
            if stepped_session:
                until = time.time() + 30
                def step(state):
                    from codex_web.identity import AuthenticationAssurance
                    state.sessions = [s.model_copy(update={'assurance': AuthenticationAssurance.PRIMARY,
                        'step_up_until': until}) if s.id == self.credentials.session_id else s for s in state.sessions]
                    return state
                self.identity.store.update(step)
            before = self.recovery.store.load()
            entered, release = threading.Event(), threading.Event()
            self.addCleanup(release.set)
            original = self.recovery._write_key_bound
            def boundary(*args, **kwargs):
                if kwargs.get('mutation_admission') is not None:
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('final-write barrier not released')
                return original(*args, **kwargs)
            with patch.object(self.recovery, '_write_key_bound', boundary):
                work = asyncio.create_task(client.post(self.url(), json=self.body()))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    await asyncio.to_thread(change, identity)
                finally:
                    release.set()
                    response = await work
            self.assertLess(response.status_code, 500, response.text)
            if response.status_code == 200:
                self.assertNotEqual(response.json()['item']['status'], 'succeeded', response.text)
            self.assertEqual(self.recovery.store.load(), before)
            self.assertEqual(before.schema_version, '1.2')
            self.assertFalse(before.reconciliations)
            self.assertEqual(self.recovery._scheduled_executor.status()['inflight_work'], 0)

    async def test_session_rotation_committed_before_domain_lock_refuses_activation(self):
        await self.denied_at_write(lambda identity: identity.refresh_session(
            self.credentials.session_id, self.credentials.refresh_token))

    async def test_session_revocation_committed_before_domain_lock_refuses_activation(self):
        await self.denied_at_write(lambda identity: identity.revoke_session(self.credentials.session_id))

    async def test_membership_revocation_committed_before_domain_lock_refuses_activation(self):
        def revoke(identity):
            membership = next(m for m in identity.store.load().memberships
                if m.identity_id == self.actor.identity_id and m.revoked_at is None)
            identity.revoke_membership(membership.id, actor=self.actor)
        await self.denied_at_write(revoke)

    async def test_binding_disable_committed_before_domain_lock_refuses_activation(self):
        def disable(identity):
            registry = ActionProviderRegistry(ActionProviderStateStore(identity.store.store))
            registry.set_enabled(self.recovery_binding.id, False, actor=self.actor)
        await self.denied_at_write(disable)

    async def test_authority_catalog_quarantine_before_domain_lock_refuses_activation(self):
        from codex_web.services.definitions import DefinitionRegistryService
        from codex_web.storage.definition_registry import DefinitionRegistryStore
        record = self.authority.catalog_record(actor=self.actor)
        def quarantine(identity):
            registry = DefinitionRegistryService(DefinitionRegistryStore(identity.store.store))
            registry.quarantine(record.record_id, actor=self.actor.identity_id,
                reason='Synthetic separate-writer authority revocation')
        await self.denied_at_write(quarantine)

    async def test_new_required_procedure_before_domain_lock_refuses_activation(self):
        from codex_web.services.definitions import DefinitionRegistryService
        from codex_web.storage.definition_registry import DefinitionRegistryStore
        from codex_web.services.skills import SkillService
        from codex_web.services.procedure_discovery import ProcedureDiscoveryService
        from codex_web.services.skill_security import SkillSecurityService
        from codex_web.storage.skill_security import SkillSecurityStore
        from codex_web.services.skill_scanners import SkillScannerRegistry
        from codex_web.skills import SkillCreate, SkillPublish
        from tests.test_skill_security import FakeScanner
        from tests.test_skill_procedures import MetadataProvider, procedure
        self.registry.register(MetadataProvider())
        def skill_service(store):
            definitions = DefinitionRegistryService(DefinitionRegistryStore(store))
            skills = SkillService(definitions)
            skills.bind_procedure_contracts(actions=self.registry, resources=self.resources,
                scope_actor=lambda record: self.actor)
            scanners = SkillScannerRegistry()
            scanners.register(FakeScanner())
            skills.bind_security(SkillSecurityService(SkillSecurityStore(store), definitions, scanners))
            return skills
        skills = skill_service(self.sqlite)
        self.service.procedure_discovery = ProcedureDiscoveryService(skills, self.resources)
        self.execution.procedure_admission = self.service.procedure_provider_admission
        def require(identity):
            writer_skills = skill_service(identity.store.store)
            content = procedure()
            content['operation_triggers'] = ['recovery.scheduled-firing.reconcile']
            draft = writer_skills.create(SkillCreate(skill_id='recovery-late-procedure',
                name='Current Recovery procedure', instructions='Canonical Evidence and current authority required.',
                operational_procedure=content), actor=self.actor)
            writer_skills.publish(draft['skillId'], draft['recordId'], SkillPublish(), actor=self.actor)
        await self.denied_at_write(require)

    async def test_step_up_expiry_without_revision_change_refuses_activation(self):
        # Only the identity clock advances; no namespace revision can fence
        # time itself. The transaction must actually reauthenticate the session.
        offset = [0]
        real_time = time.time
        with patch('codex_web.services.identity.time', SimpleNamespace(time=lambda: real_time() + offset[0])):
            await self.denied_at_write(lambda identity: offset.__setitem__(0, 31), stepped_session=True)

    async def test_revocation_after_final_identity_read_waits_for_domain_commit(self):
        from codex_web.storage.recovery import RecoveryStore
        async with self.client() as client:
            writer_store = self.writer_store()
            identity = IdentityService(IdentityStateStore(writer_store))
            entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
            self.addCleanup(release.set)
            original = RecoveryStore._encode
            def encode(state):
                if state.schema_version == '1.3' and state.reconciliations:
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('post-identity-read barrier not released')
                return original(state)
            original_lock = type(writer_store)._lock
            def lock(cursor, namespace):
                if namespace == identity.store.namespace:
                    attempted.set()
                return original_lock(cursor, namespace)
            with patch.object(RecoveryStore, '_encode', side_effect=encode):
                work = asyncio.create_task(client.post(self.url(), json=self.body()))
                writer = None
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    with patch.object(type(writer_store), '_lock', side_effect=lock):
                        writer = asyncio.create_task(asyncio.to_thread(identity.revoke_session, self.credentials.session_id))
                        self.assertTrue(await asyncio.to_thread(attempted.wait, 2))
                        self.assertFalse(writer.done(), 'separate denial writer escaped held identity lock')
                        release.set()
                        await writer
                finally:
                    release.set()
                    response = await work
                    if writer is not None:
                        await writer
            self.assertLess(response.status_code, 500, response.text)
            # This writer was ordered AFTER the domain commit, not a denial
            # committed before it. Do not roll back or falsify the domain receipt.
            self.assertEqual(self.recovery.store.load().schema_version, '1.3')
            self.assertEqual(len(self.recovery.store.load().reconciliations), 1)

    async def test_positive_real_postgres_commit_has_one_original_unknown_receipt(self):
        await api.RecoveryOperatorApiTests.test_real_operator_route_commits_once_preserving_unknown_and_current_preview(self)
