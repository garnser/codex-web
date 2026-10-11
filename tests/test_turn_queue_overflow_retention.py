from __future__ import annotations

import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_web.models import BotReplyTarget
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.turn_queue_policy import TurnQueuePolicy
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository
from tests.test_turn_execution import _Host


class DurableQueueRetentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.service, self.repository = self.reconstruct()
        self.repository.get("thread-1")

    def reconstruct(self):
        repository = TurnQueueRepository(
            SQLiteStateStore(self.root / "state.sqlite3"),
            self.root / "turn_queue.json",
        )
        host = _Host()
        host._thread_queue_record = repository.get
        host._thread_queue = repository.get
        host._thread_queue_depth = lambda thread: len(repository.get(thread))
        host._update_thread_queue_record = repository.update
        host._max_thread_queue_depth = lambda: 2
        return TurnExecutionService(host), repository

    def admit(self, service, message):
        return service.enqueue_turn(
            thread_id="thread-1",
            project_id="project-1",
            message=message,
            source="gitlab",
            work_item_ref="veridataops/Worker#47",
            repository_resource_id="worker-repository",
            writable_repository_resource_ids=("worker-repository",),
            read_only_repository_resource_ids=("core-repository",),
            execution_profile_id="repository-write",
            agent_profile_id="james",
            agent_profile_revision=6,
            agent_profile_actor_id="james-identity",
            reply_target=BotReplyTarget(
                thread_id="thread-1",
                provider="slack",
                external_conversation_id="channel-1",
                external_thread_id="original-intake",
                message_id="original-message",
                updated_at=1,
            ),
        )

    def test_saturated_queue_survives_reconstruction_and_drains_fifo_with_scope(self):
        admitted = [self.admit(self.service, f"task-{index}") for index in range(15)]
        recovered, repository = self.reconstruct()
        self.assertEqual(repository.get("thread-1"), admitted)
        actual = [recovered.pop_next_queued_turn("thread-1") for _ in admitted]
        self.assertEqual(actual, admitted)
        self.assertIsNone(recovered.pop_next_queued_turn("thread-1"))
        self.assertEqual(repository.get("thread-1"), [])
        self.assertEqual(actual[-1].agent_profile_revision, 6)
        self.assertEqual(actual[-1].read_only_repository_resource_ids, ("core-repository",))
        self.assertEqual(actual[-1].reply_target.external_thread_id, "original-intake")

    def test_duplicate_overflow_reuses_retained_execution_after_reconstruction(self):
        admitted = [self.admit(self.service, f"task-{index}") for index in range(4)]
        recovered, repository = self.reconstruct()
        duplicate = self.admit(recovered, "task-3")
        self.assertEqual(duplicate, admitted[-1])
        self.assertEqual(repository.get("thread-1"), admitted)

    def test_concurrent_saturated_admission_retains_every_distinct_task(self):
        services = [self.reconstruct()[0] for _ in range(4)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            admitted = list(executor.map(
                lambda index: self.admit(services[index % 4], f"task-{index}"),
                range(32),
            ))
        _, repository = self.reconstruct()
        stored = repository.get("thread-1")
        self.assertEqual(len(stored), 32)
        self.assertEqual({item.id for item in stored}, {item.id for item in admitted})
        self.assertEqual({item.message for item in stored}, {f"task-{index}" for index in range(32)})

    def test_failed_storage_commit_never_reports_admission_or_loses_prior_tasks(self):
        admitted = [self.admit(self.service, f"task-{index}") for index in range(3)]
        with patch.object(self.repository.store, "record_update", side_effect=OSError("storage unavailable")):
            with self.assertRaisesRegex(OSError, "storage unavailable"):
                self.admit(self.service, "uncommitted-task")
        _, repository = self.reconstruct()
        self.assertEqual(repository.get("thread-1"), admitted)

    async def test_queue_status_exposes_retention_pressure_without_starting_a_turn(self):
        for index in range(4):
            self.admit(self.service, f"task-{index}")
        await self.service.publish_queue_status("thread-1")
        event = self.service.host.hub.events[-1]
        self.assertEqual(event["queueDepth"], 4)
        self.assertEqual(event["queueHighWaterMark"], 2)
        self.assertTrue(event["queueUnderPressure"])
        self.assertEqual(event["retainedBeyondHighWaterMark"], 2)
        self.service.host.codex.request.assert_not_called()

    def test_pressure_threshold_is_visible_at_boundary(self):
        self.assertEqual(TurnQueuePolicy.retention_status(12, 12), {
            "queueHighWaterMark": 12,
            "queueUnderPressure": True,
            "retainedBeyondHighWaterMark": 0,
        })

