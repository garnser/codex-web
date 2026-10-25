"""Canonical cold completion preparation precedes scanner execution custody."""
import contextlib
import time
import unittest
from unittest.mock import patch

from codex_web.action_intents import (
    ActionDecisionSnapshot, ActionIntent, ActionIntentRetryPolicy,
)
from codex_web.action_providers import ActionDefinition, ActionRequest
from codex_web.security import ExecutionSecurityPolicy, SecurityTrustDecision
from codex_web.services.skill_scan_transport import ScannerUnavailable
from codex_web.services.skill_scanners import SkillScannerRegistry
from codex_web.services.skill_security import SkillSecurityService
from codex_web.skill_security import SkillScanRequest
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.skill_security import SkillSecurityStore
from tests.test_skill_scanner_phase_evidence import SkillScannerPhaseEvidenceTests


class SkillScannerCompletionPreparationTests(unittest.TestCase):
    setUp = SkillScannerPhaseEvidenceTests.setUp

    def seed_terminal_intents(self, count):
        decision = ActionDecisionSnapshot()
        seed = ActionIntent(
            organization_id='local', workspace_id='default', binding_id='fixture',
            provider_type='fixture', provider_instance='fixture', action_id='fixture.read',
            action_definition=ActionDefinition(action_id='fixture.read', title='Private fixture'),
            request=ActionRequest(action_id='fixture.read', organization_id='local', workspace_id='default'),
            authority_decision=decision, policy_decision=decision,
            security_policy=ExecutionSecurityPolicy(sandbox='read-only'),
            security_decision=SecurityTrustDecision(outcome='allow', risk_class='low',
                sandbox='read-only', network_enabled=False, authority_source='fixture', policy_source='fixture'),
            idempotency_key='fixture', timeout_seconds=10,
            retry_policy=ActionIntentRetryPolicy(), correlation_id='fixture',
            requested_by=self.actor.identity_id, created_at=time.time(), updated_at=time.time(),
            status='succeeded',
        )
        state = self.workers.store.store
        namespace = ActionIntentStore.records_namespace
        state.record_apply(namespace, upserts={'meta': {'schema_version': '1.4'}, **{
            'intents:terminal-%04d' % n: seed.model_copy(update={
                'id': 'terminal-%04d' % n, 'idempotency_key': 'terminal-%04d' % n,
            }).model_dump(mode='json') for n in range(count)}})

    def operation(self):
        self.provider.write_admission = lambda actor: contextlib.nullcontext()
        return self.provider._operation(self.actor, self.root, 'scan-test', self.reference)

    def test_cold_large_catalog_progresses_before_any_scanner_assignment(self):
        self.seed_terminal_intents(1100)
        before = self.workers.store.load()
        with patch('codex_web.storage.action_intents._ORIGIN_INDEX_EPOCH', 'a' * 32):
            with self.assertRaises(ScannerUnavailable) as caught:
                self.operation()
            self.assertEqual(caught.exception.code, 'scanner_completion_preparation_progressing')
            self.assertEqual(caught.exception.execution_attribution, {
                'scanner_phase': 'completion_preparation',
                'scanner_assignment_created': 0, 'scanner_provider_start_called': 0})
            self.assertEqual(self.backend.calls, [])
            self.assertEqual(self.workers.store.load(), before)
            self.assertEqual(self.workspaces.store.load().workspaces, [])
            # A distinct authorized operation resumes the durable cursor. There
            # is no first scanner assignment/process to replay or declare done.
            completion = self.operation()
            self.assertEqual(completion.assignment.status, 'succeeded')
            self.assertIsNone(completion.assignment.lease)
            self.assertEqual(len(self.backend.calls), 1)
            self.assertEqual(completion.assignment.limits.wall_seconds, 10)
            self.assertEqual(completion.assignment.limits.cpu_seconds, 10)

    def test_expired_deadline_after_returned_command_still_refuses(self):
        run = self.backend.run
        def expire(assignment, **kwargs):
            result = run(assignment, **kwargs)
            def update(state):
                state.assignments[0] = state.assignments[0].model_copy(
                    update={'deadline_at': time.time() - 1})
                return state
            self.workers.store.update_assignment(self.runtime.worker.id, assignment.id, update)
            return result
        self.backend.run = expire
        with self.assertRaises(ScannerUnavailable) as caught:
            self.operation()
        evidence = self.artifacts.get_evidence(
            caught.exception.execution_attribution['evidence_id'], actor=self.actor)
        self.assertEqual(evidence.metadata['scanner_phase'], 'backend_returned')
        self.assertFalse(evidence.metadata['scanner_completion_returned'])
        self.assertFalse(evidence.metadata['scanner_result_accepted'])
        self.assertNotEqual(self.workers.store.load().assignments[0].status, 'succeeded')
        self.assertEqual(len(self.backend.calls), 1)

    def test_small_cold_catalog_uses_real_fenced_completion(self):
        self.seed_terminal_intents(2)
        with patch('codex_web.storage.action_intents._ORIGIN_INDEX_EPOCH', 'b' * 32):
            completed = self.operation()
        self.assertEqual(completed.assignment.status, 'succeeded')
        self.assertIsNotNone(completed.assignment.completed_at)
        self.assertIsNone(completed.assignment.lease)
        self.assertEqual(completed.assignment.fence, 1)
        self.assertEqual(len(self.backend.calls), 1)

    def test_prestart_progress_is_persisted_as_typed_scan_error(self):
        self.seed_terminal_intents(1100)
        self.provider.write_admission = lambda actor: contextlib.nullcontext()
        # The existing worker fixture has no installed executable. Preparation
        # must stop before it is ever passed to the controlled fake backend.
        (self.root / 'bin').mkdir()
        (self.root / 'bin' / 'skillspector').write_text('private inert fixture')
        self.provider._settings = lambda actor: (self.root, 'scan-test', self.reference, ())
        providers = SkillScannerRegistry()
        providers.register(self.provider)
        store = SkillSecurityStore(self.workers.store.store)
        security = SkillSecurityService(store, self.provider.profiles.registry, providers)
        view = {'skillId': 'private-skill', 'recordId': 'private-record',
            'checksum': 'c' * 64, 'revision': 1,
            'skill': {'name': 'Private', 'instructions': 'Read only.',
                'provenance': {'origin': 'local'}}}
        with patch('codex_web.storage.action_intents._ORIGIN_INDEX_EPOCH', 'c' * 32):
            security.scan(view, SkillScanRequest(mode='static'), actor=self.actor)
        scans = store.scans('local', 'default', record_id='private-record')
        self.assertEqual(len(scans), 1)
        self.assertEqual(scans[0].status, 'scan_error')
        self.assertEqual(scans[0].error_code, 'scanner_completion_preparation_progressing')
        self.assertEqual(scans[0].execution_attribution, {
            'scanner_phase': 'completion_preparation',
            'scanner_assignment_created': 0, 'scanner_provider_start_called': 0})
        self.assertIs(type(scans[0].execution_attribution['scanner_assignment_created']), int)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.workers.store.load().assignments, [])
