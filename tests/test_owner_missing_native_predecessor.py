"""A missing historical native assignment never erases verified replacement custody."""
import unittest
from pathlib import Path
from tests import test_owner_native_session_recipient as fixtures
from codex_web.execution_workers import AssignmentStatus
from codex_web.storage.thread_index import ThreadIndexRepository


class MissingNativePredecessorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.n = fixtures.NativeRecipientScopeTests(); self.n.setUp()
        self.addCleanup(self.n.doCleanups)
        self.f = self.n.fixture
        self.source = self.f.assignments['assignment-existing-thread']
        self.source.status = AssignmentStatus.CANCELLED; self.source.lease = None
        self.f.assignments.pop(self.n.original.id)
        self.replacements = ThreadIndexRepository(self.f.f.env.env.state,
            Path(self.f.f.env.env.temp.name) / 'replacement-lineage.json')
        self.replacements.remember_replacement('existing-thread', 'platform-thread', project_id='project-a',
            provenance={'organization_id': 'org-a', 'workspace_id': 'workspace-a'})
        self.n.routing.replacement_resolver = self.replacements.replacement_thread_id
        current = self.f.assignments['assignment-platform-thread']
        self.current_session = self.n.session.model_copy(update={'id': 'current-native-session',
            'provider_native_session_id': 'platform-thread', 'assignment_id': current.id,
            'execution_id': current.execution_id, 'execution_workspace_id': current.execution_workspace_id,
            'worker_id': current.assigned_worker_id, 'resource_ids': ('platform',)})
        self.n.store.upsert(self.current_session)

    async def test_actual_continuity_uses_explicit_live_replacement_without_historical_repair(self):
        source_before = self.source.model_dump(mode='json')
        native_before = self.n.store.load().model_dump(mode='json')
        await self.f.continuity.dispatch_structured_handoff(self.n.state, source='work-item-handoff')
        self.f.fixture.dispatch.assert_awaited_once()
        pin = self.f.fixture.dispatch.call_args.kwargs['owner_routing_pin']
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        self.f.validate(pin)
        self.assertEqual(self.source.model_dump(mode='json'), source_before)
        self.assertEqual(self.n.store.load().model_dump(mode='json'), native_before)
        self.assertEqual(self.f.f.binding.model_dump(mode='json'), self.f.binding_before)
        self.assertNotIn(self.n.original.id, self.f.assignments)
        self.f.continuity.replace_nonperforming_thread.assert_not_called()

    async def test_missing_replacement_never_reinterprets_original_unknown(self):
        from codex_web.owner_routing import OwnerRoutingError
        self.n.routing.replacement_resolver = lambda *a, **k: None
        with self.assertRaisesRegex(OwnerRoutingError, 'native_custody_unverified'):
            self.n.select()

    async def test_current_native_session_must_positively_bind_current_assignment(self):
        from codex_web.owner_routing import OwnerRoutingError
        for update in ({'assignment_id': 'absent-current'}, {'execution_id': 'stale-execution'},
                       {'execution_workspace_id': 'stale-workspace'}, {'worker_id': 'stale-worker'}):
            with self.subTest(update=update):
                self.n.store.upsert(self.current_session.model_copy(update=update))
                with self.assertRaisesRegex(OwnerRoutingError, 'native_custody_unverified'):
                    self.n.select()
        self.n.store.upsert(self.current_session)

    async def test_ambiguous_or_foreign_original_native_custody_is_not_rescued(self):
        from codex_web.owner_routing import OwnerRoutingError
        self.n.store.upsert(self.n.session.model_copy(update={'project_id': 'foreign'}))
        with self.assertRaisesRegex(OwnerRoutingError, 'native_custody_unverified'):
            self.n.select()
        self.n.store.upsert(self.n.session)
        self.n.store.upsert(self.n.session.model_copy(update={'id': 'second-original', 'runtime_id': 'another'}))
        with self.assertRaisesRegex(OwnerRoutingError, 'native_custody_unverified'):
            self.n.select()

    async def test_live_original_native_assignment_is_not_rescued(self):
        from codex_web.owner_routing import OwnerRoutingError
        self.n.original.status = AssignmentStatus.RUNNING
        self.f.assignments[self.n.original.id] = self.n.original
        with self.assertRaisesRegex(OwnerRoutingError, 'native_custody_unverified'):
            self.n.select()

    async def test_original_source_must_be_terminal_and_unleased(self):
        from codex_web.owner_routing import OwnerRoutingError
        self.source.status = AssignmentStatus.RUNNING
        with self.assertRaisesRegex(OwnerRoutingError, 'native_custody_unverified'):
            self.n.select()

    async def test_replacement_scope_profile_actor_or_lease_drift_refuses(self):
        from codex_web.owner_routing import OwnerRoutingError
        import copy
        current = self.f.assignments['assignment-platform-thread']
        for update in ({'created_by': 'foreign'},
                       {'agent_profile': current.agent_profile.model_copy(update={'profile_revision': 99})},
                       {'repository_scope': current.repository_scope.model_copy(update={'writable_repository_ids': ('other',)})},
                       {'fence': current.fence + 1}):
            with self.subTest(update=update):
                self.f.assignments[current.id] = current.model_copy(deep=True, update=copy.deepcopy(update))
                with self.assertRaises(OwnerRoutingError):
                    self.n.select()
        self.f.assignments[current.id] = current

    async def test_foreign_or_cyclic_replacement_graph_refuses(self):
        from codex_web.owner_routing import OwnerRoutingError
        namespace = self.replacements.REPLACEMENTS_NAMESPACE
        original = self.replacements.store.get(namespace)
        import copy
        foreign = copy.deepcopy(original)
        foreign['edges']['existing-thread']['project_id'] = 'foreign'
        self.replacements.store.put(namespace, foreign)
        with self.assertRaisesRegex(OwnerRoutingError, 'native_custody_unverified'):
            self.n.select()
        cycle = copy.deepcopy(original)
        cycle['edges']['platform-thread'] = {**cycle['edges']['existing-thread'],
            'old_thread_id': 'platform-thread', 'new_thread_id': 'existing-thread'}
        self.replacements.store.put(namespace, cycle)
        with self.assertRaisesRegex(OwnerRoutingError, 'native_custody_unverified'):
            self.n.select()

    async def test_multiple_live_candidates_remain_ambiguous(self):
        from types import SimpleNamespace
        from codex_web.owner_routing import OwnerRoutingError
        current = self.f.assignments['assignment-platform-thread']
        other = current.model_copy(deep=True, update={'id': 'assignment-other',
            'execution_id': 'other-execution', 'execution_workspace_id': 'other-workspace'})
        self.f.assignments[other.id] = other
        self.f.bootstraps['other'] = SimpleNamespace(id='other', thread_id='other', assignment_id=other.id,
            execution_id=other.execution_id, execution_workspace_id=other.execution_workspace_id,
            created_by=other.created_by, organization_id=other.organization_id, workspace_id=other.workspace_id)
        with self.assertRaisesRegex(OwnerRoutingError, 'ambiguous'):
            self.n.select()

    async def test_original_unresolved_work_still_blocks_current_replacement(self):
        from codex_web.owner_routing import OwnerRoutingError
        attempt = self.f.f.uncertain(); self.f.f.preflight.put(attempt)
        with self.assertRaisesRegex(OwnerRoutingError, 'native_outcome_unresolved'):
            self.n.select()
        self.assertEqual(self.f.f.preflight.get(attempt.id), attempt)

    async def test_final_guard_rechecks_lineage_and_current_native_custody(self):
        from codex_web.owner_routing import OwnerRoutingError
        pin = self.n.select()
        self.replacements.remember_replacement('platform-thread', 'new-successor', project_id='project-a',
            provenance={'organization_id': 'org-a', 'workspace_id': 'workspace-a'})
        with self.assertRaisesRegex(OwnerRoutingError, 'native_custody_unverified'):
            self.f.validate(pin)

    async def test_final_guard_retains_original_foreign_and_current_fence_denials(self):
        from codex_web.owner_routing import OwnerRoutingError
        pin = self.n.select()
        self.n.store.upsert(self.n.session.model_copy(update={'project_id': 'foreign'}))
        with self.assertRaisesRegex(OwnerRoutingError, 'native_custody_unverified'):
            self.f.validate(pin)
        self.n.store.upsert(self.n.session)
        self.f.assignments[pin.recipient.assignment_id].fence += 1
        with self.assertRaisesRegex(OwnerRoutingError, 'custody_unavailable'):
            self.f.validate(pin)

    async def test_actual_native_capture_excludes_only_its_own_retained_original_then_acknowledges(self):
        await self.f.test_retained_native_capture_observer_projects_selected_lane_then_allows_ack()
        self.assertEqual(self.f.f.env.states.get(self.n.state.ref).handoff.status, 'accepted')
        self.assertNotIn(self.n.original.id, self.f.assignments)
