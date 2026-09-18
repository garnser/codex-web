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
