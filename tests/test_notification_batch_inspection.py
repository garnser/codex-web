import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import (
    Project,
    QueuedTurn,
    QueuedNativeStartReceipt,
    IndexedThread,
)
from codex_web.notification_batches import select_notification_batch
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.notification_batch_inspection import NotificationBatchInspection
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class NotificationBatchInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite")
        self.store = ExecutionPreflightStore(self.state)
        self.preflight = ExecutionPreflightService(self.store)
        self.inspector = NotificationBatchInspection(self.store, clock=lambda: 100)
        self.actor = AuthenticationActor(
            identity_id="actor-a",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.project = Project(
            id="project-a", name="Synthetic fixture", path="/fixture"
        )
        self.items = [
            QueuedTurn(
                id="original-" + str(i),
                execution_id="requested-" + str(i),
                thread_id="thread-a",
                project_id=self.project.id,
                source="gitlab",
                message="Full synthetic notification " + str(i),
                created_at=i,
                work_item_ref="fixture/repo#" + str(i),
                execution_profile_id="orchestration-only",
                agent_profile_id="agent-a",
                agent_profile_revision=6,
                agent_profile_actor_id="actor-a",
            )
            for i in (1, 2)
        ]
        attempts = {
            item.id: self.preflight.retain_queued(
                item, organization_id="local", workspace_id="default"
            )
            for item in self.items
        }
        self.plan = select_notification_batch(
            self.items,
            anchor_id=self.items[0].id,
            attempts_by_original_id=attempts,
            verified_notification_ids=frozenset(attempts),
            authority_scope_sha256_by_original_id={key: "a" * 64 for key in attempts},
            organization_id="local",
            workspace_id="default",
            notification_source_origin="gitlab",
            max_members=2,
            max_context_bytes=65536,
        )
        self.batch = self.store.claim_notification_batch(
            self.plan,
            claim_id="fixture-claim",
            now=10,
            queue_order=tuple(item.id for item in self.items),
        )

    def detail(self, **changes):
        arguments = dict(thread_id="thread-a", project=self.project, actor=self.actor)
        arguments.update(changes)
        return self.inspector.detail(self.batch.id, **arguments)

    def history(self, **changes):
        arguments = dict(thread_id="thread-a", project=self.project, actor=self.actor)
        arguments.update(changes)
        return self.inspector.list(**arguments)

    def test_read_only_detail_retains_originals_and_scope_without_inventing_outcomes(
        self,
    ):
        revision = self.state.namespace_revision(self.store.NAMESPACE)
        detail = self.detail()
        self.assertEqual(detail["original_count"], 2)
        self.assertEqual(detail["oldest_created_at"], 1)
        self.assertEqual(detail["oldest_age_seconds"], 99)
        self.assertEqual(
            detail["recovery_state"], "prepared_recovery_requires_current_authority"
        )
        self.assertEqual(
            [item["original"] for item in detail["originals"]],
            [item.model_dump(mode="json") for item in self.items],
        )
        self.assertTrue(
            all(item["outcome"] == "unresolved" for item in detail["originals"])
        )
        self.assertTrue(
            all(
                item["retention_integrity"] == "matches_original"
                for item in detail["originals"]
            )
        )
        self.assertIsNone(detail["native_receipt"])
        self.assertEqual(self.history()["items"][0]["id"], self.batch.id)
        self.assertEqual(self.state.namespace_revision(self.store.NAMESPACE), revision)

    def test_tenant_project_thread_and_cursor_isolation(self):
        for changes in (
            {"actor": self.actor.model_copy(update={"workspace_id": "foreign"})},
            {"actor": self.actor.model_copy(update={"organization_id": "foreign"})},
            {"project": self.project.model_copy(update={"id": "foreign"})},
            {"thread_id": "foreign"},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(HTTPException) as error,
            ):
                self.detail(**changes)
            self.assertEqual(error.exception.status_code, 404)
        with self.assertRaises(HTTPException) as error:
            self.history(after="bt/another-scope/entry")
        self.assertEqual(error.exception.status_code, 422)

    def test_released_history_survives_restart_without_member_pointer(self):
        self.store.transition_notification_batch(
            self.batch.id, claim_id=self.batch.claim_id, phase="released"
        )
        self.inspector = NotificationBatchInspection(
            ExecutionPreflightStore(SQLiteStateStore(self.state.path)),
            clock=lambda: 100,
        )
        history = self.history()
        self.assertEqual(history["items"][0]["phase"], "released")
        self.assertEqual(
            history["items"][0]["recovery_state"], "released_to_original_queue"
        )
        detail = self.detail()
        self.assertTrue(
            all(item["current_batch_id"] is None for item in detail["originals"])
        )
        self.assertTrue(
            all(item["outcome"] == "unresolved" for item in detail["originals"])
        )

    def test_bounded_history_paginates_without_repeating_claims(self):
        self.store.transition_notification_batch(
            self.batch.id, claim_id=self.batch.claim_id, phase="released"
        )
        second = self.store.claim_notification_batch(
            self.plan,
            claim_id="second-claim",
            now=20,
            queue_order=tuple(item.id for item in self.items),
        )
        first_page = self.history(limit=1)
        self.assertEqual([item["id"] for item in first_page["items"]], [second.id])
        second_page = self.history(limit=1, after=first_page["next_cursor"])
        self.assertEqual([item["id"] for item in second_page["items"]], [self.batch.id])
        self.assertIsNone(second_page["next_cursor"])

    def test_missing_retention_is_visible_without_erasing_the_original_or_claiming_success(
        self,
    ):
        self.state.record_apply(
            self.store.NAMESPACE,
            upserts={},
            deletes=("a/" + self.batch.attempt_ids[0],),
        )
        detail = self.detail()
        original = detail["originals"][0]
        self.assertEqual(original["original"], self.items[0].model_dump(mode="json"))
        self.assertEqual(original["retained_status"], "missing")
        self.assertEqual(original["retention_integrity"], "changed_or_missing")
        self.assertEqual(original["outcome"], "unresolved")

    def test_native_receipt_is_shared_and_original_completion_stays_unresolved(self):
        self.store.consume_notification_native_start(
            self.batch.id,
            claim_id=self.batch.claim_id,
            requested_execution_id=self.batch.id,
            thread_id="thread-a",
            message=self.batch.native_input(),
        )
        self.store.record_notification_native_start(
            self.batch.id,
            QueuedNativeStartReceipt(
                admission_claim_id=self.batch.claim_id,
                requested_execution_id=self.batch.id,
                execution_id="fixture-execution",
                thread_id="thread-a",
                provider_native_session_id="fixture-session",
                provider_native_turn_id="fixture-turn",
                provider_id="fixture-provider",
                runtime_id="fixture-runtime",
                observed_at=20,
            ),
        )
        detail = self.detail()
        self.assertEqual(detail["phase"], "native_input_started")
        self.assertEqual(
            detail["native_receipt"]["provider_native_turn_id"], "fixture-turn"
        )
        self.assertEqual(detail["per_original_completion"], "unresolved")
        self.assertTrue(
            all(
                item["retained_status"] == "reconciliation_required"
                for item in detail["originals"]
            )
        )

    def test_all_individual_recovery_actions_are_hidden_and_refused_for_members(self):
        attempt_id = self.batch.attempt_ids[0]
        attempt = self.store.get(attempt_id)
        public = self.preflight.public(attempt, actor=self.actor)
        for flag in (
            "can_retry",
            "can_recover_bootstrap",
            "can_reconcile_prestart",
            "can_retire_scheduling",
        ):
            self.assertFalse(public[flag])
        for action in (
            self.preflight.authorize_retry,
            self.preflight.authorize_scheduling_retirement,
            self.preflight.reconcile_prestart_refusal,
        ):
            with self.subTest(action=action), self.assertRaises(HTTPException) as error:
                action(attempt_id, actor=self.actor)
            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual(
                error.exception.detail["code"],
                "notification_batch_reconciliation_required",
            )
        self.assertEqual(self.store.get(attempt_id), attempt)

    def test_http_routes_enforce_thread_scope_and_never_write_on_refresh(self):
        from codex_web.api.turns import build_turns_router
        from codex_web.services.projects import ProjectService
        from codex_web.services.thread_scope import ThreadScopeService
        from codex_web.storage.thread_index import ThreadIndexRepository

        index = ThreadIndexRepository(self.state, Path(self.temp.name) / "threads.json")
        index.upsert(
            IndexedThread(id="thread-a", name="Fixture", project_id=self.project.id)
        )
        scope = ThreadScopeService(
            ProjectService(SimpleNamespace(load=lambda: [self.project])),
            SimpleNamespace(store=SimpleNamespace(list=lambda: [])),
            index,
        )
        app = FastAPI()

        @app.middleware("http")
        async def identity(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(
            build_turns_router(
                SimpleNamespace(), scope, notification_batches=self.inspector
            )
        )
        revision = self.state.namespace_revision(self.store.NAMESPACE)
        with TestClient(app) as client:
            path = "/api/threads/thread-a/notification-batches"
            self.assertEqual(client.get(path).status_code, 200)
            self.assertEqual(client.get(path + "/" + self.batch.id).status_code, 200)
            self.assertEqual(client.get(path + "?project_id=foreign").status_code, 404)
            self.assertEqual(client.post(path + "/" + self.batch.id).status_code, 405)
            self.actor = self.actor.model_copy(update={"workspace_id": "foreign"})
            self.assertEqual(client.get(path + "/" + self.batch.id).status_code, 404)
        self.assertEqual(self.state.namespace_revision(self.store.NAMESPACE), revision)
