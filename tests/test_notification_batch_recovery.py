import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from codex_web.models import QueuedTurn
from codex_web.notification_batches import NotificationBatchRecoveryRequest
from codex_web.services.coordination import StateStoreCoordinationBackend
from codex_web.services.replicated_ownership import ReplicatedOwnershipService
from codex_web.services.notification_batches import NotificationBatchCoordinator
from codex_web.services.notification_batch_recovery import (
    NotificationBatchRecoveryService,
)
from codex_web.storage.thread_index import ThreadIndexRepository
from codex_web.storage.turn_queue import TurnQueueRepository
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class NotificationBatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        from tests.test_notification_batch_runtime import NotificationBatchRuntimeTests

        self.fixture = NotificationBatchRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.publish()
        self.queues = TurnQueueRepository(f.state, Path(f.temp.name) / "queues.json")
        self.unrelated = QueuedTurn(
            id="unrelated",
            thread_id="thread-a",
            project_id=f.project.id,
            source="web",
            message="Separate human request",
            created_at=10,
        )
        self.queues.put("thread-a", [*f.items, self.unrelated])
        self.coordinator = NotificationBatchCoordinator(
            queue=self.queues, preflight=f.preflight.store, clock=lambda: 100
        )
        self.batch = self.coordinator.prepare(
            f.plan(f.controls.resolve(f.items[0], f.project)),
            claim_id="fixture-batch-claim",
            current=lambda: True,
        )
        self.routes = ThreadIndexRepository(f.state, Path(f.temp.name) / "threads.json")
        self.ownership = ReplicatedOwnershipService(
            StateStoreCoordinationBackend(f.state, clock=lambda: 100),
            instance_id="fixture-recovery",
            clock=lambda: 100,
        )
        self.service = NotificationBatchRecoveryService(
            preflight=f.preflight.store,
            queues=self.queues,
            ownership=self.ownership,
            authority=f.authority,
            projects=f.controls.projects,
            resources=f.resources,
            replacement_resolver=self.routes.replacement_thread_id,
            actor_resolver=f.controls.actor_resolver,
            clock=lambda: 100,
        )

    def preview(self, **changes):
        values = dict(
            thread_id="thread-a", project=self.fixture.project, actor=self.fixture.actor
        )
        values.update(changes)
        return self.service.preview(self.batch.id, **values)

    def request(self):
        return NotificationBatchRecoveryRequest(
            expected_admission_sha256=self.preview()["expected_admission_sha256"],
            reason="Synthetic reviewed recovery",
        )

    def recover(self, request=None, **changes):
        values = dict(
            thread_id="thread-a", project=self.fixture.project, actor=self.fixture.actor
        )
        values.update(changes)
        return asyncio.run(
            self.service.recover(self.batch.id, request or self.request(), **values)
        )

    def test_preview_is_read_only_and_recovery_restores_every_original_without_native_start(
        self,
    ):
        revision = self.fixture.state.namespace_revision(
            self.fixture.preflight.store.NAMESPACE
        )
        preview = self.preview()
        self.assertTrue(preview["can_restore"])
        self.assertEqual(preview["original_count"], 3)
        self.assertEqual(
            self.fixture.state.namespace_revision(
                self.fixture.preflight.store.NAMESPACE
            ),
            revision,
        )
        request = self.request()
        result = self.recover(request)
        self.assertFalse(result["native_started"])
        self.assertEqual(
            self.queues.get("thread-a"), [*self.fixture.items, self.unrelated]
        )
        released = self.fixture.preflight.store.get_notification_batch(self.batch.id)
        self.assertEqual(released.phase, "released")
        self.assertEqual(released.recovery.reason, request.reason)
        for attempt_id in self.batch.attempt_ids:
            original = self.fixture.preflight.store.get(attempt_id)
            self.assertEqual(original.status, "queued")
            self.assertIsNone(original.notification_batch_id)
            self.assertIsNone(original.queued_start_receipt)
        self.assertEqual(self.recover(request), result)

    def test_unknown_native_outcome_is_never_recovered_from_absence(self):
        self.fixture.preflight.store.consume_notification_native_start(
            self.batch.id,
            claim_id=self.batch.claim_id,
            requested_execution_id=self.batch.id,
            thread_id="thread-a",
            message=self.batch.native_input(),
        )
        self.assertFalse(self.preview()["can_restore"])
        with self.assertRaises(HTTPException) as error:
            self.recover()
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.queues.get("thread-a"), [self.unrelated])

    def test_typed_preworker_refusal_permits_restoration(self):
        store = self.fixture.preflight.store
        store.consume_notification_native_start(
            self.batch.id,
            claim_id=self.batch.claim_id,
            requested_execution_id=self.batch.id,
            thread_id="thread-a",
            message=self.batch.native_input(),
        )
        store.record_notification_prestart_refusal(
            self.batch.id,
            claim_id=self.batch.claim_id,
            code="upgrade_maintenance_active",
        )
        self.recover()
        released = store.get_notification_batch(self.batch.id)
        self.assertEqual(released.prestart_refusal_code, "upgrade_maintenance_active")
        self.assertEqual(released.phase, "released")

    def test_reservation_fences_native_gate_and_crash_recovery_is_idempotent(self):
        request = self.request()
        with patch.object(
            self.fixture.preflight.store,
            "release_notification_recovery",
            side_effect=RuntimeError("Synthetic crash after queue restoration"),
        ):
            with self.assertRaises(HTTPException):
                self.recover(request)
        reserved = self.fixture.preflight.store.get_notification_batch(self.batch.id)
        self.assertEqual(reserved.recovery.state, "reserved")
        with self.assertRaises(ValueError):
            self.fixture.preflight.store.consume_notification_native_start(
                self.batch.id,
                claim_id=self.batch.claim_id,
                requested_execution_id=self.batch.id,
                thread_id="thread-a",
                message=self.batch.native_input(),
            )
        reopened = ExecutionPreflightStore(SQLiteStateStore(self.fixture.state.path))
        self.service.preflight = reopened
        self.recover(request)
        self.assertEqual(
            self.queues.get("thread-a"), [*self.fixture.items, self.unrelated]
        )
        self.assertEqual(
            reopened.get_notification_batch(self.batch.id).recovery.state, "released"
        )

    def test_changed_queue_copy_refuses_overwrite_and_keeps_reservation(self):
        changed = self.fixture.items[0].model_copy(
            update={"message": "Changed queue copy"}
        )
        self.queues.put("thread-a", [changed, self.unrelated])
        with self.assertRaises(HTTPException):
            self.recover()
        self.assertEqual(self.queues.get("thread-a"), [changed, self.unrelated])
        self.assertEqual(
            self.fixture.preflight.store.get_notification_batch(
                self.batch.id
            ).recovery.state,
            "reserved",
        )

    def test_stale_review_and_insufficient_assurance_refuse_before_mutation(self):
        request = self.request().model_copy(
            update={"expected_admission_sha256": "f" * 64}
        )
        with self.assertRaises(HTTPException) as error:
            self.recover(request)
        self.assertEqual(error.exception.status_code, 409)
        weak = self.fixture.actor.model_copy(update={"assurance": "primary"})
        with self.assertRaises(HTTPException) as error:
            self.preview(actor=weak)
        self.assertEqual(error.exception.status_code, 403)
        foreign = self.fixture.actor.model_copy(update={"workspace_id": "foreign"})
        with self.assertRaises(HTTPException) as error:
            self.preview(actor=foreign)
        self.assertEqual(error.exception.status_code, 404)
        self.assertIsNone(
            self.fixture.preflight.store.get_notification_batch(self.batch.id).recovery
        )

    def test_scoped_replacement_preserves_originals_and_routes_restored_copies(self):
        self.routes.remember_replacement(
            "thread-a",
            "replacement-thread",
            project_id=self.fixture.project.id,
            provenance={"organization_id": "local", "workspace_id": "default"},
        )
        priority = self.unrelated.model_copy(
            update={
                "id": "priority",
                "thread_id": "replacement-thread",
                "source": "web",
            }
        )
        self.queues.put("replacement-thread", [priority])
        result = self.recover()
        self.assertEqual(result["threadId"], "replacement-thread")
        restored = self.queues.get("replacement-thread")
        self.assertEqual(
            [item.id for item in restored],
            [priority.id, *[item.id for item in self.fixture.items]],
        )
        self.assertEqual(self.queues.get("thread-a"), [self.unrelated])
        for item in restored[1:]:
            attempt = self.fixture.preflight.store.get("preflight-queued-" + item.id)
            self.assertEqual(attempt.replacement_thread_id, "replacement-thread")
            self.assertEqual(attempt.queued_turn_snapshot["thread_id"], "thread-a")
            self.assertEqual(item.message, attempt.queued_turn_snapshot["message"])
        self.assertEqual(
            len(self.fixture.preflight.store.for_thread("replacement-thread")), 3
        )

    def test_foreign_replacement_provenance_cannot_restore_originals(self):
        self.routes.remember_replacement(
            "thread-a",
            "foreign-thread",
            project_id=self.fixture.project.id,
            provenance={"organization_id": "local", "workspace_id": "foreign"},
        )
        with self.assertRaises(HTTPException):
            self.preview()
        self.assertEqual(self.queues.get("thread-a"), [self.unrelated])

    def test_native_gate_winning_before_reservation_prevents_queue_restoration(self):
        request = self.request()
        reserve = self.fixture.preflight.store.reserve_notification_recovery

        def native_wins(*args, **kwargs):
            self.fixture.preflight.store.consume_notification_native_start(
                self.batch.id,
                claim_id=self.batch.claim_id,
                requested_execution_id=self.batch.id,
                thread_id="thread-a",
                message=self.batch.native_input(),
            )
            return reserve(*args, **kwargs)

        with patch.object(
            self.fixture.preflight.store,
            "reserve_notification_recovery",
            side_effect=native_wins,
        ):
            with self.assertRaises(HTTPException):
                self.recover(request)
        self.assertEqual(self.queues.get("thread-a"), [self.unrelated])
        self.assertEqual(
            self.fixture.preflight.store.get_notification_batch(self.batch.id).phase,
            "native_outcome_unknown",
        )

    def test_lost_queue_fence_prevents_restoration_and_claim_release(self):
        request = self.request()
        original = self.queues.restore_many_exact

        def fenced(*args, **kwargs):
            self.ownership.release("thread-queue:thread-a")
            return original(*args, **kwargs)

        with patch.object(self.queues, "restore_many_exact", side_effect=fenced):
            with self.assertRaises(HTTPException):
                self.recover(request)
        self.assertEqual(self.queues.get("thread-a"), [self.unrelated])
        self.assertEqual(
            self.fixture.preflight.store.get_notification_batch(
                self.batch.id
            ).recovery.state,
            "reserved",
        )

    def test_http_scope_assurance_stale_review_and_payload_boundaries(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from types import SimpleNamespace
        from codex_web.api.turns import build_turns_router
        from codex_web.models import IndexedThread
        from codex_web.services.projects import ProjectService
        from codex_web.services.thread_scope import ThreadScopeService
        from codex_web.storage.agent_sessions import AgentSessionStore

        self.routes.upsert(
            IndexedThread(
                id="thread-a",
                name="Synthetic fixture",
                project_id=self.fixture.project.id,
            )
        )
        scope = ThreadScopeService(
            ProjectService(SimpleNamespace(load=lambda: [self.fixture.project])),
            SimpleNamespace(store=AgentSessionStore(self.routes.store)),
            self.routes,
        )
        app = FastAPI()
        self.http_actor = self.fixture.actor

        @app.middleware("http")
        async def identity(request, call_next):
            request.state.identity_actor = self.http_actor
            return await call_next(request)

        app.include_router(
            build_turns_router(SimpleNamespace(), scope, batch_recovery=self.service)
        )
        with TestClient(app) as client:
            path = (
                "/api/threads/thread-a/notification-batches/"
                + self.batch.id
                + "/recovery"
            )
            preview = client.get(path)
            self.assertEqual(preview.status_code, 200)
            payload = {
                "expected_admission_sha256": preview.json()[
                    "expected_admission_sha256"
                ],
                "reason": "Reviewed fixture",
            }
            self.assertEqual(
                client.post(
                    path, json={**payload, "native_outcome": "unstarted"}
                ).status_code,
                422,
            )
            self.assertEqual(
                client.post(
                    path, json={**payload, "expected_admission_sha256": "f" * 64}
                ).status_code,
                409,
            )
            self.assertEqual(client.get(path + "?project_id=foreign").status_code, 404)
            self.http_actor = self.fixture.actor.model_copy(
                update={"assurance": "primary"}
            )
            self.assertEqual(client.get(path).status_code, 403)
            self.assertEqual(client.post(path, json=payload).status_code, 403)
            self.http_actor = self.fixture.actor
            self.assertEqual(client.post(path, json=payload).status_code, 200)
            self.assertEqual(client.post(path, json=payload).status_code, 200)
