"""Actual atomic row guards precede assignment mutation; no authority is cached."""
import unittest

from tests.test_execution_workers import ExecutionWorkerServiceTests


class WorkerTransitionGuardObservationTests(unittest.TestCase):
    setUp = ExecutionWorkerServiceTests.setUp
    tearDown = ExecutionWorkerServiceTests.tearDown
    _assignment = ExecutionWorkerServiceTests._assignment

    def test_guard_uses_current_locked_rows_before_updater_and_preserves_legacy_default(self):
        assignment = self._assignment()
        store = self.service.store
        backend = store.store
        namespace = 'private.guard-observation'
        captured = {'value': 'before'}
        backend.put(namespace, captured)
        # The metadata changed after preparation but BEFORE the actual transition.
        backend.put(namespace, {'value': 'current'})
        order = []
        def guard(rows):
            self.assertTrue(store._lock._is_owned())
            self.assertEqual(rows[namespace][None], {'value': 'current'})
            self.assertEqual(rows[store.assignment_namespace][assignment.id]['id'], assignment.id)
            order.append('guard')
        def update(state):
            self.assertEqual(order, ['guard'])
            order.append('updater')
            state.assignments[0].updated_at += 1
            return state
        changed = store.transition_assignment(self.worker.id, assignment.id, update,
            guard_rows={namespace: (None,)}, validate_guards=guard).assignments[0]
        self.assertEqual(order, ['guard', 'updater'])
        self.assertEqual(changed.updated_at, assignment.updated_at + 1)
        self.assertEqual(backend.get(namespace), {'value': 'current'})
        legacy = store.transition_assignment(self.worker.id, assignment.id, lambda state: state)
        self.assertEqual(legacy.assignments[0], changed)

    def test_rejected_raw_guard_cannot_enter_updater_or_write_assignment(self):
        assignment = self._assignment()
        store = self.service.store
        namespace = 'private.guard-missing'
        called = []
        def guard(rows):
            self.assertIsNone(rows[namespace][None])
            raise ValueError('prepared raw metadata differs')
        def update(state):
            called.append(True)
            state.assignments[0].updated_at += 1
            return state
        with self.assertRaisesRegex(ValueError, 'prepared raw metadata differs'):
            store.transition_assignment(self.worker.id, assignment.id, update,
                guard_rows={namespace: (None,)}, validate_guards=guard)
        self.assertEqual(called, [])
        self.assertEqual(store.assignment(assignment.id), assignment)

    def test_async_guard_is_closed_without_entering_updater(self):
        assignment = self._assignment()
        store = self.service.store
        called, coroutines = [], []
        async def delayed():
            called.append('async body')
        def guard(rows):
            coroutine = delayed()
            coroutines.append(coroutine)
            return coroutine
        def update(state):
            called.append('updater')
            return state
        with self.assertRaisesRegex(ValueError, 'must be synchronous'):
            store.transition_assignment(self.worker.id, assignment.id, update, validate_guards=guard)
        self.assertEqual(called, [])
        self.assertIsNone(coroutines[0].cr_frame)
        self.assertEqual(store.assignment(assignment.id), assignment)


def load_tests(loader, standard_tests, pattern):
    return loader.loadTestsFromTestCase(WorkerTransitionGuardObservationTests)
