"""On-demand current handoff inspection never performs admission or delivery."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from fastapi import HTTPException
from tests import test_owner_routing_dispatch as routing_fixtures
from codex_web.owner_routing import OwnerRoutingError


class PendingHandoffEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = routing_fixtures.OwnerRoutingContinuityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.owner = self.fixture.continuity
        self.routing = self.fixture.fixture.service
        self.routing.inspection_ready = lambda: True
        self.routing.inspection_actor = lambda: self.fixture.fixture.actor
        self.env = self.fixture.fixture.env
        self.state = self.env.pending()
        self.state.handoff.owner_routing = self.env.machine._owner_routing(self.state).binding('operator')
        self.env.states.put(self.state.ref, self.state)
        self.routing.prepare_compatible_pin = Mock(side_effect=AssertionError('preparation prohibited'))
        self.routing.record_failure = Mock(side_effect=AssertionError('canonical write prohibited'))
        self.routing.clear_failure = Mock(side_effect=AssertionError('canonical write prohibited'))

    def inspect(self):
        method = getattr(self.owner, 'handoff_eligibility_projection', None)
        if method is None:
            # Actual previous projection hides the denial behind pendinghandoff.
            return self.owner.owner_dispatch_projection(self.state)
        return method(self.state, expected_requested_at=1, expected_recipient='operator')

    def test_pending_handoff_exposes_actual_typed_denial_without_dispatch(self):
        self.routing.compatible_pin = Mock(side_effect=OwnerRoutingError('owner_recipient_preparation_changed'))
        result = self.inspect()
        self.assertEqual(result['blocked_reason'], 'owner_recipient_preparation_changed')
        self.assertFalse(result['eligible'])
        self.assertTrue(result['inspection_only'])
        self.assertEqual(result['handoff_requested_at'], 1)
        self.assertEqual(result['recipient'], 'operator')
        self.routing.prepare_compatible_pin.assert_not_called()
        self.routing.record_failure.assert_not_called()
        self.fixture.dispatch.assert_not_called()

    def test_real_pin_and_context_checks_never_fall_back_to_bootstrap_lookup_actor(self):
        self.routing.control_actor = Mock(side_effect=AssertionError('bootstrap lookup prohibited'))
        self.routing.inspection_actor = Mock(return_value=self.fixture.fixture.actor)
        result = self.inspect()
        self.assertEqual(result['status'], 'eligible')
        self.assertGreaterEqual(self.routing.inspection_actor.call_count, 2)
        self.routing.control_actor.assert_not_called()
        self.routing.prepare_compatible_pin.assert_not_called()
        self.fixture.dispatch.assert_not_called()

    def test_changed_nonce_or_recipient_does_not_discover_or_prepare(self):
        self.routing.compatible_pin = Mock(side_effect=AssertionError('discovery prohibited'))
        for nonce, recipient in [(2,'operator'),(1,'someone-else')]:
            result = self.owner.handoff_eligibility_projection(self.state,
                expected_requested_at=nonce, expected_recipient=recipient)
            self.assertEqual(result['status'], 'changed')
        self.routing.compatible_pin.assert_not_called()
        self.routing.prepare_compatible_pin.assert_not_called()

    def test_current_pin_context_is_revalidated_and_target_is_only_inspection(self):
        witness = SimpleNamespace(thread_id='current-recipient', assignment_id='current-assignment', fence=7)
        pin = SimpleNamespace(recipient=witness,
            binding=self.env.machine._owner_routing(self.state).binding('operator'))
        self.routing.compatible_pin = Mock(return_value=pin)
        self.routing.context_for_pin = Mock(return_value=(self.fixture.fixture.profile, self.fixture.fixture.actor))
        result = self.inspect()
        self.assertEqual(result['status'], 'eligible')
        self.assertEqual(result['current_thread_id'], 'current-recipient')
        self.assertEqual(result['assignment_id'], 'current-assignment')
        self.assertEqual(result['fence'], 7)
        self.assertEqual(result['agent_profile_id'], self.fixture.fixture.profile.profile_id)
        self.routing.context_for_pin.assert_called_once()
        self.routing.prepare_compatible_pin.assert_not_called()
        self.fixture.dispatch.assert_not_called()

    def test_context_refusal_and_work_drift_never_grant_eligibility(self):
        self.routing.compatible_pin = Mock(return_value=SimpleNamespace(recipient=None))
        self.routing.context_for_pin = Mock(side_effect=OwnerRoutingError('owner_executor_authority_changed'))
        self.assertEqual(self.inspect()['blocked_reason'], 'owner_executor_authority_changed')
        def drift(*args, **kwargs):
            newer = self.env.states.get(self.state.ref)
            newer.updated_at += 1
            self.env.states.put(newer.ref, newer)
            return self.fixture.fixture.profile, self.fixture.fixture.actor
        self.routing.context_for_pin = Mock(side_effect=drift)
        self.assertEqual(self.inspect()['status'], 'changed')
        self.fixture.dispatch.assert_not_called()

    def test_absent_delivery_and_unavailable_storage_are_not_authority(self):
        self.state.handoff = None
        result = self.owner.handoff_eligibility_projection(self.state,
            expected_requested_at=1, expected_recipient='operator')
        self.assertEqual(result['status'], 'no_pending_handoff')
        self.assertFalse(result['eligible'])
        self.state = self.env.pending()
        self.routing.compatible_pin = Mock(side_effect=RuntimeError('private storage detail'))
        result = self.inspect()
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('private', str(result))

    def test_uninitialized_readers_refuse_without_lookup_migration_or_selection(self):
        self.routing.inspection_ready = lambda: False
        self.routing.compatible_pin = Mock(side_effect=AssertionError('selection prohibited'))
        with patch.object(self.owner, 'get_state', side_effect=AssertionError('lookup prohibited')):
            result = self.inspect()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['blocked_reason'], 'owner_routing_inspection_not_initialized')
        self.routing.compatible_pin.assert_not_called()

    def test_denial_with_unavailable_final_snapshot_is_unknown_not_a_stale_reason(self):
        self.routing.compatible_pin = Mock(side_effect=OwnerRoutingError('owner_recipient_preparation_changed'))
        with patch.object(self.owner, 'get_state', side_effect=OSError('private storage detail')):
            result = self.inspect()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['blocked_reason'], 'owner_routing_inspection_unavailable')
        self.assertNotIn('private', str(result))


class InspectionReadinessTests(unittest.TestCase):
    def test_readonly_lookup_actor_reaches_missing_predecessor_and_current_recipient_checks(self):
        from tests import test_owner_missing_native_predecessor as missing_fixtures
        fixture = missing_fixtures.MissingNativePredecessorTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        native = fixture.n
        actor = native.routing.control_actor()
        native.routing.control_actor = Mock(side_effect=AssertionError('bootstrap lookup prohibited'))
        lookup = Mock(return_value=actor)
        pin = native.routing.compatible_pin(native.state, native.binding, 'orchestrator',
            writable_repository_ids=('platform',), lookup_actor=lookup)
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        native.routing.context_for_pin(pin, native.binding, lookup_actor=lookup)
        self.assertGreaterEqual(lookup.call_count, 5)
        native.routing.control_actor.assert_not_called()

    def test_existing_local_lookup_actor_cannot_bootstrap_or_restore_revoked_identity(self):
        import tempfile
        from pathlib import Path
        from codex_web.storage.sqlite_state import SQLiteStateStore
        from codex_web.storage.identity_state import IdentityStateStore
        from codex_web.services.identity import IdentityService, IdentityError
        with tempfile.TemporaryDirectory() as root:
            service = IdentityService(IdentityStateStore(SQLiteStateStore(Path(root)/'state.db')))
            with patch.object(service, 'bootstrap_local', side_effect=AssertionError('bootstrap prohibited')):
                with self.assertRaises(IdentityError):
                    service.existing_local_trusted_actor()
            self.assertEqual(service.store.load().humans, [])
            service.bootstrap_local()
            expected = service.local_trusted_actor()
            with patch.object(service, 'bootstrap_local', side_effect=AssertionError('bootstrap prohibited')):
                self.assertEqual(service.existing_local_trusted_actor().model_dump(exclude={'authenticated_at'}),
                    expected.model_dump(exclude={'authenticated_at'}))
            def revoke(state):
                state.memberships[0].revoked_at = 1
                return state
            service.store.update(revoke)
            with patch.object(service, 'bootstrap_local', side_effect=AssertionError('bootstrap prohibited')):
                with self.assertRaises(IdentityError):
                    service.existing_local_trusted_actor()
            self.assertEqual(service.store.load().memberships[0].revoked_at, 1)
            def disable(state):
                state.memberships[0].revoked_at = None
                state.humans[0].disabled_at = 1
                return state
            service.store.update(disable)
            with patch.object(service, 'bootstrap_local', side_effect=AssertionError('bootstrap prohibited')):
                with self.assertRaises(IdentityError):
                    service.existing_local_trusted_actor()
            self.assertEqual(service.store.load().humans[0].disabled_at, 1)

    def test_readiness_never_initializes_worker_or_session_records(self):
        import tempfile
        from pathlib import Path
        from codex_web.storage.sqlite_state import SQLiteStateStore
        from codex_web.storage.execution_workers import ExecutionWorkerStore
        from codex_web.storage.agent_sessions import AgentSessionStore
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteStateStore(Path(root)/'state.db')
            workers, sessions = ExecutionWorkerStore(store), AgentSessionStore(store)
            with patch.object(store, 'record_mutate', side_effect=AssertionError('write prohibited')), \
                 patch.object(store, 'update_many', side_effect=AssertionError('migration prohibited')):
                self.assertFalse(workers.reads_initialized())
                self.assertFalse(sessions.reads_initialized())
            workers._ensure_records()
            sessions._ensure_records()
            with patch.object(store, 'record_mutate', side_effect=AssertionError('write prohibited')), \
                 patch.object(store, 'update_many', side_effect=AssertionError('migration prohibited')):
                self.assertTrue(workers.reads_initialized())
                self.assertTrue(sessions.reads_initialized())
            store.record_apply(sessions.records_namespace,
                upserts={sessions._meta_key: {'schema_version': 'unknown'}})
            self.assertFalse(sessions.reads_initialized())

    def test_operator_rechecks_current_object_scope_before_projection(self):
        from codex_web.services.work_item_operator import WorkItemOperatorService
        current = SimpleNamespace(organization_id='foreign', workspace_id='workspace-a', project_id='project-a')
        operator = SimpleNamespace(state_machine=SimpleNamespace(_work_item_state=lambda ref: current),
            require_handoff_inspection_ready=Mock(), project_handoff_eligibility=Mock())
        with self.assertRaises(HTTPException) as error:
            WorkItemOperatorService.handoff_eligibility(operator, 'item-a', expected_requested_at=1,
                expected_recipient='quinn', organization_id='org-a', workspace_id='workspace-a',
                project_id='project-a')
        self.assertEqual(error.exception.status_code, 404)
        operator.project_handoff_eligibility.assert_not_called()
