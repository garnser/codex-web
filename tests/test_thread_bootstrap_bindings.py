from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.execution_subjects import ExecutionSubjectKind
from codex_web.services.identity import IdentityService
from codex_web.services.thread_bootstrap_bindings import (
    ThreadBootstrapBindingConflictError,
    ThreadBootstrapBindingNotFoundError,
    ThreadBootstrapBindingService,
)
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.thread_bootstrap_bindings import (
    ThreadBootstrapBindingStore,
)
from codex_web.thread_bootstrap import (
    deterministic_thread_bootstrap_binding_id,
)


class ThreadBootstrapBindingTests(unittest.TestCase):
    def test_lost_source_requires_explicit_recovery_cas_and_keeps_original_unchanged(self):
        from codex_web.execution_workers import ExecutionAssignment, NetworkPolicy, WorkerResourceLimits
        from codex_web.storage.execution_workers import ExecutionWorkerStore
        original = self._bind()
        source = ExecutionAssignment(id=original.assignment_id, organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id, project_id='project', execution_id=original.execution_id,
            subject={'kind':'thread_bootstrap','ref':original.bootstrap_id},
            execution_workspace_id=original.execution_workspace_id, resource_ids=('repo',),
            execution_contract_version='thread-bootstrap/1.0', required_capabilities=(),
            sandbox='workspace-write', approval_policy='never', network=NetworkPolicy(), limits=WorkerResourceLimits(),
            created_by=self.actor.identity_id, status='lost', fence=3)
        assignments = ExecutionWorkerStore(self.sqlite)
        assignments.update_assignment('fixture-worker', source.id,
            lambda state: state.model_copy(update={'assignments':[source]}))
        self.service.assignment_store = assignments
        args = dict(bootstrap_id='next', thread_id=original.thread_id, execution_id='next-execution',
            assignment_id='next-assignment', execution_workspace_id='next-workspace', actor=self.actor,
            expected_assignment_id=source.id, expected_assignment_fence=3)
        with self.assertRaises(ValueError): self.service.rebind(**args)
        self.assertEqual(self.service.get_by_thread(original.thread_id, self.actor), original)
        with self.assertRaises(ValueError): self.service.rebind(**{**args, 'expected_assignment_fence':4}, allow_lost_assignment=True)
        rebound = self.service.rebind(**args, allow_lost_assignment=True)
        self.assertEqual(rebound.assignment_id, 'next-assignment')
        self.assertEqual(assignments.assignment(source.id), source)

    def test_competing_assignment_renewals_compare_the_original_binding_atomically(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        original = self._bind()
        barrier = threading.Barrier(2)
        def renew(index):
            barrier.wait(5)
            try:
                result = self.service.rebind(bootstrap_id=f'new-{index}', thread_id=original.thread_id,
                    execution_id=f'new-execution-{index}', assignment_id=f'new-assignment-{index}',
                    execution_workspace_id=f'new-workspace-{index}', actor=self.actor,
                    expected_assignment_id=original.assignment_id)
                return result.assignment_id
            except ThreadBootstrapBindingConflictError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(renew, (1, 2)))
        winners = [value for value in results if value is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.service.get_by_thread(original.thread_id, self.actor).assignment_id, winners[0])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(self.sqlite))
        self.identity.bootstrap_local()
        self.actor = self.identity.local_trusted_actor()
        self.service = ThreadBootstrapBindingService(
            ThreadBootstrapBindingStore(self.sqlite)
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _bind(self, **overrides):
        values = {
            "bootstrap_id": "bootstrap-1",
            "thread_id": "thread-upstream-1",
            "execution_id": "thread-bootstrap-exec-1",
            "assignment_id": "assignment-1",
            "execution_workspace_id": "execws-1",
            "actor": self.actor,
        }
        values.update(overrides)
        return self.service.bind(**values)

    def test_binding_is_deterministic_immutable_and_idempotent(self) -> None:
        first = self._bind()
        second = self._bind()

        self.assertEqual(second, first)
        self.assertEqual(
            first.id,
            deterministic_thread_bootstrap_binding_id(
                self.actor.organization_id,
                self.actor.workspace_id,
                "bootstrap-1",
            ),
        )
        self.assertEqual(
            first.subject.kind,
            ExecutionSubjectKind.THREAD_BOOTSTRAP,
        )
        self.assertEqual(first.subject.ref, "bootstrap-1")
        self.assertEqual(self.service.get_by_thread("thread-upstream-1", self.actor), first)
        self.assertEqual(self.service.get_by_bootstrap("bootstrap-1", self.actor), first)
        self.assertEqual(self.service.list(self.actor), (first,))

    def test_binding_persists_initial_turn_lifecycle(self) -> None:
        binding = self._bind()

        self.assertTrue(binding.initial_turn_pending)
        updated = self.service.mark_initial_turn_started(
            binding.thread_id,
            self.actor,
        )

        self.assertFalse(updated.initial_turn_pending)
        self.assertFalse(
            self.service.get_by_thread(binding.thread_id, self.actor).initial_turn_pending
        )

    def test_rebind_preserves_pending_initial_turn(self) -> None:
        original = self._bind()

        rebound = self.service.rebind(
            bootstrap_id="bootstrap-2",
            thread_id=original.thread_id,
            execution_id="thread-bootstrap-exec-2",
            assignment_id="assignment-2",
            execution_workspace_id="execws-2",
            actor=self.actor,
        )

        self.assertTrue(rebound.initial_turn_pending)
        self.assertEqual(
            self.service.get_by_thread(original.thread_id, self.actor),
            rebound,
        )

    def test_rebind_started_thread_requires_resume(self) -> None:
        original = self._bind()
        self.service.mark_initial_turn_started(original.thread_id, self.actor)

        rebound = self.service.rebind(
            bootstrap_id="bootstrap-2",
            thread_id=original.thread_id,
            execution_id="thread-bootstrap-exec-2",
            assignment_id="assignment-2",
            execution_workspace_id="execws-2",
            actor=self.actor,
        )

        self.assertFalse(rebound.initial_turn_pending)

    def test_bootstrap_cannot_be_rebound_to_different_thread_or_execution(self) -> None:
        self._bind()

        for overrides in (
            {"thread_id": "thread-upstream-2"},
            {"execution_id": "different-execution"},
            {"assignment_id": "assignment-2"},
            {"execution_workspace_id": "execws-2"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ThreadBootstrapBindingConflictError):
                    self._bind(**overrides)

    def test_thread_cannot_be_bound_to_different_bootstrap(self) -> None:
        self._bind()

        with self.assertRaisesRegex(
            ThreadBootstrapBindingConflictError,
            "already bound to a different thread bootstrap",
        ):
            self._bind(
                bootstrap_id="bootstrap-2",
                execution_id="thread-bootstrap-exec-2",
                assignment_id="assignment-2",
                execution_workspace_id="execws-2",
            )

    def test_binding_lookup_is_tenant_scoped_and_fails_closed(self) -> None:
        first = self._bind()
        other = self.actor.model_copy(
            update={
                "organization_id": "other-org",
                "workspace_id": "other-workspace",
            }
        )

        self.assertEqual(self.service.list(other), ())
        with self.assertRaises(ThreadBootstrapBindingNotFoundError):
            self.service.get_by_thread(first.thread_id, other)
        with self.assertRaises(ThreadBootstrapBindingNotFoundError):
            self.service.get_by_bootstrap(first.bootstrap_id, other)

        other_binding = self._bind(actor=other)
        self.assertNotEqual(other_binding.id, first.id)
        self.assertEqual(other_binding.thread_id, first.thread_id)
        self.assertEqual(other_binding.bootstrap_id, first.bootstrap_id)

    def test_binding_requires_non_empty_canonical_identifiers(self) -> None:
        for field in (
            "bootstrap_id",
            "thread_id",
            "execution_id",
            "assignment_id",
            "execution_workspace_id",
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(RuntimeError, "is required"):
                    self._bind(**{field: " "})


if __name__ == "__main__":
    unittest.main()
