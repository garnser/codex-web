"""Retired equal-scope bindings cannot bypass canonical live-recipient custody."""
import unittest
from unittest.mock import patch, AsyncMock
from codex_web.owner_routing import OwnerRoutingError
from codex_web.execution_workers import AssignmentStatus
from tests.test_owner_routing_recipients import CompatibleRecipientTests


class RetiredSameScopeRecipientTests(CompatibleRecipientTests):
    def retired_source(self):
        source = self.assignments['assignment-existing-thread']
        source.repository_scope = source.repository_scope.model_copy(update={
            'writable_repository_ids': ('platform',), 'read_only_repository_ids': ()})
        source.status = AssignmentStatus.CANCELLED
        source.lease = None
        return source

    def canonical_replacement(self, *, project='project-a'):
        from pathlib import Path
        from codex_web.storage.thread_index import ThreadIndexRepository
        index = ThreadIndexRepository(self.f.env.env.state, Path(self.f.env.env.temp.name) / 'replacements.json')
        index.remember_replacement('existing-thread', 'platform-thread', project_id=project,
            provenance={'organization_id': 'org-a', 'workspace_id': 'workspace-a', 'bootstrap_id': 'actual-retained-successor'})
        self.service.replacement_resolver = index.replacement_thread_id
        return index

    async def test_actual_continuity_uses_live_successor_when_retired_binding_has_same_scope(self):
        source = self.retired_source()
        original = source.model_dump(mode='json')
        self.canonical_replacement()
        with patch.object(self.continuity.policy, 'handoff_continuity_delay', return_value=0):
            await self.continuity.run_handoff_continuity_check(self.state.ref,
                expected_recipient='orchestrator', expected_requested_at=1, source='handoff-continuity')
        self.fixture.dispatch.assert_awaited_once()
        pin = self.fixture.dispatch.call_args.kwargs['owner_routing_pin']
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        self.assertEqual(pin.original_thread_id, 'existing-thread')
        self.continuity.replace_nonperforming_thread.assert_not_called()
        self.assertEqual(source.model_dump(mode='json'), original)
        self.assertEqual(self.f.binding.model_dump(mode='json'), self.binding_before)
        self.validate(pin)

    async def test_unreplaced_first_admission_and_retired_recovery_keep_existing_bootstrap(self):
        source = self.retired_source()
        self.bootstraps.pop('platform-thread')
        # No live recipient/lineage: preserve the supported ordinary bootstrap,
        # not a new rule requiring every fresh owner to be running already.
        for status in (AssignmentStatus.PENDING, AssignmentStatus.CANCELLED, AssignmentStatus.LOST):
            with self.subTest(status=status):
                source.status = status
                self.assertIsNone(self.select())
        self.continuity.replace_nonperforming_thread = AsyncMock(return_value=self.f.binding)
        await self.continuity.dispatch_structured_handoff(self.state, source='ordinary-bootstrap')
        self.continuity.replace_nonperforming_thread.assert_awaited_once()
        self.fixture.dispatch.assert_awaited_once()
        self.assertIsNone(self.fixture.dispatch.call_args.kwargs.get('owner_routing_pin'))

    async def test_replaced_source_missing_live_recipient_refuses_without_retarget(self):
        self.retired_source()
        self.canonical_replacement()
        self.assignments['assignment-platform-thread'].status = AssignmentStatus.CANCELLED
        with self.assertRaisesRegex(OwnerRoutingError, 'compatible_lane_unavailable'):
            self.select()
        self.continuity.replace_nonperforming_thread.assert_not_called()
        self.assertEqual(self.f.binding.model_dump(mode='json'), self.binding_before)

    async def test_replaced_source_ambiguous_live_scope_refuses(self):
        from types import SimpleNamespace
        self.retired_source()
        self.canonical_replacement()
        original = self.assignments['assignment-platform-thread']
        other = original.model_copy(deep=True, update={'id': 'assignment-other',
            'execution_id': 'execution-other', 'execution_workspace_id': 'workspace-other'})
        self.assignments[other.id] = other
        self.bootstraps['other'] = SimpleNamespace(id='bootstrap-other', thread_id='other',
            assignment_id=other.id, execution_id=other.execution_id,
            execution_workspace_id=other.execution_workspace_id, created_by=other.created_by,
            organization_id='org-a', workspace_id='workspace-a')
        with self.assertRaisesRegex(OwnerRoutingError, 'ambiguous'):
            self.select()
        self.continuity.replace_nonperforming_thread.assert_not_called()

    async def test_foreign_canonical_replacement_is_not_an_executor_grant(self):
        self.retired_source()
        self.canonical_replacement(project='project-b')
        with self.assertRaisesRegex(OwnerRoutingError, 'replacement_unverified'):
            self.select()
        self.fixture.dispatch.assert_not_awaited()

    async def test_same_scope_replacement_preserves_original_unknown_hold(self):
        self.retired_source()
        self.canonical_replacement()
        original = self.f.uncertain()
        self.f.preflight.put(original)
        with self.assertRaisesRegex(OwnerRoutingError, 'native_outcome_unresolved'):
            self.select()
        self.assertEqual(self.f.preflight.get(original.id), original)
        self.fixture.dispatch.assert_not_awaited()

    async def test_selected_successor_fence_drift_still_refuses_final_native(self):
        self.retired_source()
        self.canonical_replacement()
        pin = self.select()
        self.assignments['assignment-platform-thread'].fence += 1
        with self.assertRaisesRegex(OwnerRoutingError, 'custody_unavailable|custody_changed'):
            self.validate(pin)
        self.fixture.dispatch.assert_not_awaited()


def load_tests(loader, tests, pattern):
    # Parent fixtures are reused, not inherited as a second broad suite.
    return unittest.TestSuite(RetiredSameScopeRecipientTests(name) for name in [
        'test_actual_continuity_uses_live_successor_when_retired_binding_has_same_scope',
        'test_unreplaced_first_admission_and_retired_recovery_keep_existing_bootstrap',
        'test_replaced_source_missing_live_recipient_refuses_without_retarget',
        'test_replaced_source_ambiguous_live_scope_refuses',
        'test_foreign_canonical_replacement_is_not_an_executor_grant',
        'test_same_scope_replacement_preserves_original_unknown_hold',
        'test_selected_successor_fence_drift_still_refuses_final_native',
    ])
