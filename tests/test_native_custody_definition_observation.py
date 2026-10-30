"""One fresh Definition document for a real two-Skill custody projection."""
import copy
import unittest
from unittest.mock import patch

from codex_web.definitions import DefinitionLifecycle
from codex_web.services.procedure_execution import ProcedureExecutionError
from codex_web.services.skills import SkillResolutionFailure
from codex_web.skills import SkillCreate, SkillPublish
from tests import test_native_procedure_credential_custody as custody_fixture
from tests.test_skills import actor


class NativeCustodyDefinitionObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = custody_fixture.NativeProcedureCredentialCustodyTests()
        await self.fixture.asyncSetUp()
        n = self.fixture
        self.f = n.f
        self.owner = n.owner
        content = copy.deepcopy(self.f.skills.definitions.get_record(
            n.fixture.reference.record_id).payload['operational_procedure'])
        content['conflict_group'] = 'second-custody-observation'
        created = self.f.skills.create(SkillCreate(
            skill_id='second-custody-observation', name='Second custody observation',
            instructions='Use the same controlled canonical provider.',
            operational_procedure=content), actor=self.f.actor)
        self.f.skills.publish(created['skillId'], created['recordId'],
            SkillPublish(), actor=self.f.actor)
        n.bootstrap()
        discovery, _ = await n.admit()
        self.assignment = n.current()
        self.selected = self.assignment.native_credential_custody.procedures
        self.assertEqual(len(discovery.selected), 2)
        self.assertEqual(len(self.selected), 2)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def project(self, *, actor_override=None):
        return self.owner._native_credential_projection(
            self.assignment, self.selected, actor_override or self.f.actor)

    def count_reads(self, callback):
        store = self.f.skills.definitions.store
        original = store.store.get
        reads = []
        def get(namespace):
            if namespace == store.namespace:
                reads.append(namespace)
            return original(namespace)
        with patch.object(store.store, 'get', get):
            result = callback()
        return result, len(reads)

    async def test_two_sealed_skills_share_one_physical_definition_read(self):
        before = self.fixture.current().model_dump(mode='json')
        with patch.object(self.fixture.secrets, 'use', side_effect=AssertionError('No material borrow')):
            result, reads = self.count_reads(self.project)
        self.assertEqual(reads, 1)
        self.assertEqual(result.model_dump(exclude={'admitted_at'}),
            self.assignment.native_credential_custody.model_dump(exclude={'admitted_at'}))
        self.assertEqual(self.fixture.current().model_dump(mode='json'), before)
        self.assertEqual(self.f.ops.calls, [])

    async def test_next_callback_sees_selected_skill_revocation(self):
        self.project()
        selected_id = self.selected[-1].record_id
        self.f.skills.definitions.store.update(lambda rows: [
            row.model_copy(update={'lifecycle': DefinitionLifecycle.DISABLED})
            if row.record_id == selected_id else row for row in rows])
        with self.assertRaises(SkillResolutionFailure):
            self.project()

    async def test_next_callback_sees_secret_revocation(self):
        self.project()
        self.fixture.secrets.revoke(self.fixture.provider_secret.id, actor=self.f.actor)
        with self.assertRaisesRegex(ProcedureExecutionError, 'credential_reference_revoked'):
            self.project()

    async def test_custom_public_preparation_keeps_legacy_signature_and_reads(self):
        original = self.owner.validate_native_preparation
        calls = []
        def custom(assignment, selected, actor):
            calls.append((assignment.id, tuple(selected), actor.identity_id))
            return original(assignment, selected, actor)
        with patch.object(self.owner, 'validate_native_preparation', custom):
            _, reads = self.count_reads(self.project)
        self.assertEqual(reads, 4)
        self.assertEqual(calls, [(self.assignment.id, self.selected, self.f.actor.identity_id)])

    async def test_custom_private_preparation_refusal_is_not_bypassed(self):
        with patch.object(self.owner, '_validate_native_preparation',
                side_effect=ProcedureExecutionError('custom_preparation_refused')):
            with self.assertRaisesRegex(ProcedureExecutionError, 'custom_preparation_refused'):
                self.project()

    async def test_custom_observer_keeps_legacy_read_count_and_call_order(self):
        skills = self.f.skills
        original = skills.observe_definition_reads
        store = skills.definitions.store
        backend_get = store.store.get
        events = []
        def get(namespace):
            if namespace == store.namespace:
                events.append('definition_read')
            return backend_get(namespace)
        def custom(references, callback):
            events.append('custom_observer')
            self.assertEqual(tuple(references), self.selected)
            return original(references, callback)
        with patch.object(skills, 'observe_definition_reads', custom), \
                patch.object(store.store, 'get', get):
            self.project()
        self.assertEqual(events, ['definition_read', 'custom_observer',
            'definition_read', 'definition_read', 'definition_read'])

    async def test_custom_definition_owner_keeps_original_signature(self):
        definitions = self.f.skills.definitions
        original = definitions.get_record
        seen = []
        def custom(record_id):
            seen.append(record_id)
            return original(record_id)
        with patch.object(definitions, 'get_record', custom):
            self.project()
        self.assertTrue(set(r.record_id for r in self.selected).issubset(seen))

    async def test_malformed_matching_duplicate_is_not_hidden(self):
        store = self.f.skills.definitions.store
        raw = store.store.get(store.namespace)
        duplicate = copy.deepcopy(next(r for r in raw['records']
            if r['record_id'] == self.selected[-1].record_id))
        duplicate['revision'] = 'invalid'
        raw['records'].append(duplicate)
        store.store.put(store.namespace, raw)
        with self.assertRaises((ValueError, SkillResolutionFailure)):
            self.project()

    async def test_foreign_actor_cannot_borrow_current_projection(self):
        foreign = actor('foreign-custody', workspace_id='foreign-workspace', admin=True)
        def denied():
            with self.assertRaisesRegex(ProcedureExecutionError, 'credential_requester_changed'):
                self.project(actor_override=foreign)
        _, reads = self.count_reads(denied)
        self.assertEqual(reads, 0)

    async def test_expired_lease_refuses_before_any_definition_read(self):
        self.assignment = self.assignment.model_copy(update={
            'lease': self.assignment.lease.model_copy(update={'expires_at': 0})})
        def denied():
            with self.assertRaises(ProcedureExecutionError):
                self.project()
        _, reads = self.count_reads(denied)
        self.assertEqual(reads, 0)
