"""Ordinary-data acceptance for the bounded Upgrade drain reader.

Run only against the reviewed candidate source, never application/production state.
"""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError
from codex_web.action_intents import ActionIntent, ActionIntentStatus
from codex_web.execution_workers import AssignmentStatus, ExecutionAssignment
from codex_web.services.upgrades import UpgradeService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_action_intents as fixtures
from tests import test_execution_workers as worker_fixtures


class ActiveIntentPagesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.ActionIntentTests()
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.original = self.fixture._create()
        self.state = self.fixture.sqlite
        self.store = self.fixture.service.store

    def seed(self, intents):
        changes = {"intents:" + item.id: item.model_dump(mode="json") for item in intents}
        # Deliberately no status/lease aliases: the canonical intent rows suffice.
        self.state.record_mutate(self.store.records_namespace, tuple(changes), lambda _: changes)

    async def test_all_four_active_statuses_across_scopes_without_indexes_or_history(self):
        expected = {ActionIntentStatus.CLAIMED, ActionIntentStatus.EXECUTING,
                    ActionIntentStatus.UNCERTAIN, ActionIntentStatus.REQUIRES_RECONCILIATION}
        items = []
        for scope in (("local", "default"), ("local", "other"), ("other", "elsewhere")):
            for status in ActionIntentStatus:
                item = self.original.model_copy(update={
                    "id": f"independent-{scope[0]}-{scope[1]}-{status.value}",
                    "organization_id": scope[0], "workspace_id": scope[1],
                    "status": status, "lease": None})
                items.append(ActionIntent.model_validate(item.model_dump(mode="json")))
        self.seed(items)
        service = object.__new__(UpgradeService)
        service.action_intents = SimpleNamespace(store=self.store)
        with patch.object(self.store, "load", side_effect=AssertionError("whole ledger read")), \
                patch.object(self.state, "record_items", side_effect=AssertionError("whole collection read")):
            self.assertEqual(service._active_action_count(self.fixture.actor, service_wide=True), 12)
            # Cursor advancement must visit every full validated row, including terminal ones.
            seen, cursor = [], None
            while True:
                page, cursor = self.store.intent_page(after=cursor, limit=2)
                self.assertTrue(all(isinstance(item, ActionIntent) for item in page))
                seen.extend(item.id for item in page)
                if cursor is None:
                    break
            self.assertEqual(set(seen), {self.original.id, *(item.id for item in items)})
            self.assertEqual(len(seen), len(set(seen)))
            self.assertEqual(sum(item.status in expected for item in items), 12)

    async def test_malformed_full_intent_body_is_not_hidden_by_valid_terminal_status(self):
        raw = self.original.model_dump(mode="json")
        raw.update(id="independent-malformed", status="succeeded")
        del raw["action_id"]
        self.state.record_mutate(self.store.records_namespace, ("intents:independent-malformed",),
                                 lambda _: {"intents:independent-malformed": raw})
        with self.assertRaises(ValidationError):
            self.store.intent_page(limit=100)

    async def test_invalid_status_and_scalar_rows_fail_closed(self):
        for raw in ({**self.original.model_dump(mode="json"), "status": "invented"}, 3):
            with self.subTest(kind=type(raw).__name__):
                self.state.record_mutate(self.store.records_namespace, ("intents:independent-invalid",),
                                         lambda _: {"intents:independent-invalid": raw})
                with self.assertRaises((ValidationError, ValueError)):
                    self.store.intent_page(limit=100)

    async def test_null_record_tombstone_matches_existing_full_state_reader(self):
        self.state.record_apply(self.store.records_namespace, upserts={"intents:absent": None})
        self.assertIn("intents:absent", self.state.record_items(self.store.records_namespace))
        expected = self.store.load().intents
        page, cursor = self.store.intent_page(limit=100)
        self.assertIsNone(cursor)
        self.assertEqual(page, expected)

    async def test_unsupported_legacy_contract_does_not_become_zero_actions(self):
        state = SQLiteStateStore(Path(self.fixture.temp.name) / "unsupported-legacy.sqlite")
        state.put("action_intents", {"schema_version": "unsupported", "intents": []})
        with self.assertRaises(ValueError):
            ActionIntentStore(state).intent_page(limit=2)


class ActiveAssignmentPagesTests(unittest.TestCase):
    def setUp(self):
        self.fixture = worker_fixtures.ExecutionWorkerServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.original = self.fixture._assignment()
        self.store = self.fixture.service.store
        self.state = self.store.store

    def test_only_claimed_and_running_count_across_all_hosted_scopes_without_history(self):
        items = []
        for org, workspace in (("local", "default"), ("local", "other"), ("other", "elsewhere")):
            for status in AssignmentStatus:
                raw = self.original.model_dump(mode="json")
                raw.update(id=f"independent-{org}-{workspace}-{status.value}",
                           organization_id=org, workspace_id=workspace, status=status.value)
                items.append(ExecutionAssignment.model_validate(raw))
        changes = {item.id: item.model_dump(mode="json") for item in items}
        self.state.record_mutate(self.store.assignment_namespace, tuple(changes), lambda _: changes)
        service = object.__new__(UpgradeService)
        service.workers = SimpleNamespace(store=self.store)
        record_items = self.state.record_items
        def workers_only(namespace):
            self.assertEqual(namespace, self.store.worker_namespace,
                             "only the small worker collection may be read as a whole")
            return record_items(namespace)
        with patch.object(self.store, "load", side_effect=AssertionError("worker history read")), \
                patch.object(self.state, "record_items", side_effect=workers_only):
            self.assertEqual(service._active_assignment_count(self.fixture.admin, service_wide=True), 6)

    def test_malformed_terminal_assignment_is_fully_validated(self):
        raw = self.original.model_dump(mode="json")
        raw.update(id="independent-invalid", status="succeeded")
        del raw["execution_id"]
        self.state.record_mutate(self.store.assignment_namespace, ("independent-invalid",),
                                 lambda _: {"independent-invalid": raw})
        with self.assertRaises(ValidationError):
            self.store.assignment_page(limit=100)
