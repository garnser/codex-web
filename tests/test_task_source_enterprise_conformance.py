from __future__ import annotations

import unittest

from codex_web.models import TaskSourceIdentity
from codex_web.services.task_source_conformance import (
    TaskSourceConformanceError,
    TaskSourceConformanceSuite,
)
from codex_web.services.task_sources import (
    TaskSourceCanonicalProjection,
    TaskSourceCapabilities,
    TaskSourceCapability,
    TaskSourceEvent,
    TaskSourcePage,
    TaskSourceReconciliationCursor,
    TaskSourceSnapshot,
    TaskSourceUserReference,
)


class _EnterpriseSource:
    source_type = "enterprise"
    source_instance = "https://tickets.example"
    capabilities = TaskSourceCapabilities(
        frozenset(
            {
                TaskSourceCapability.DISCOVERY,
                TaskSourceCapability.READ,
                TaskSourceCapability.EVENTS,
                TaskSourceCapability.PAGED_DISCOVERY,
                TaskSourceCapability.INCREMENTAL_RECONCILIATION,
                TaskSourceCapability.WORKFLOW_TRANSITIONS,
                TaskSourceCapability.PROVIDER_IDENTITIES,
                TaskSourceCapability.RICH_TEXT,
            }
        )
    )

    def __init__(self) -> None:
        self.snapshot = TaskSourceSnapshot(
            identity=TaskSourceIdentity(
                source_type=self.source_type,
                source_instance=self.source_instance,
                external_id="TASK-1",
                revision="2026-09-19T06:00:00Z",
            ),
            title="Investigate outage",
            body_text="Plain-text normalized provider description",
            source_state="active",
            owner_references=(
                TaskSourceUserReference(
                    provider_id="u-42",
                    display_name="Dana Operator",
                    username="dana",
                ),
            ),
            priority="high",
            category="incident",
        )

    async def discover(self, *, scope: str):
        return [self.snapshot]

    async def discover_page(self, *, scope: str, cursor=None, limit=100):
        if cursor is None:
            return TaskSourcePage(
                items=(self.snapshot,),
                next_cursor="page-2",
                watermark="2026-09-19T06:00:00Z",
                exhausted=False,
            )
        return TaskSourcePage(items=(), watermark="2026-09-19T06:00:00Z", exhausted=True)

    async def reconcile_since(self, *, scope: str, cursor=None, limit=100):
        return TaskSourcePage(
            items=(self.snapshot,),
            watermark="2026-09-19T06:00:00Z|TASK-1",
            exhausted=True,
        )

    async def read(self, identity):
        return self.snapshot

    async def normalize_event(self, payload):
        return TaskSourceEvent(
            identity=self.snapshot.identity,
            event_type="updated",
            snapshot=self.snapshot,
        )

    def project(self, snapshot, *, current_stage=None):
        return TaskSourceCanonicalProjection(
            identity=snapshot.identity,
            stage=current_stage or "implementation_active",
            source_state=snapshot.source_state,
        )

    async def write_owner(self, identity, owner):
        return self.snapshot

    async def write_state(self, identity, state):
        return self.snapshot

    async def add_comment(self, identity, body):
        return None

    async def attach_artifact(self, identity, url):
        return None

    async def available_transitions(self, identity):
        return ("start_progress", "resolve")

    async def transition(self, identity, transition):
        return self.snapshot


class EnterpriseTaskSourceConformanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.source = _EnterpriseSource()
        self.suite = TaskSourceConformanceSuite()

    def test_enterprise_capabilities_require_matching_optional_protocols(self) -> None:
        self.suite.validate_adapter(self.source)

    async def test_paged_discovery_is_bounded_and_validated(self) -> None:
        first = await self.source.discover_page(scope="ops", limit=1)
        self.suite.validate_page(self.source, first)
        self.assertFalse(first.exhausted)
        self.assertEqual(first.next_cursor, "page-2")

        second = await self.source.discover_page(scope="ops", cursor=first.next_cursor, limit=1)
        self.suite.validate_page(self.source, second)
        self.assertTrue(second.exhausted)

    async def test_incremental_reconciliation_exposes_stable_watermark(self) -> None:
        cursor = TaskSourceReconciliationCursor(
            watermark="2026-09-19T05:00:00Z",
            tiebreaker="TASK-0",
        )
        page = await self.source.reconcile_since(scope="ops", cursor=cursor, limit=100)
        self.suite.validate_page(self.source, page)
        self.assertEqual(page.watermark, "2026-09-19T06:00:00Z|TASK-1")

    def test_provider_user_reference_is_descriptive_not_canonical_identity(self) -> None:
        ref = self.source.snapshot.owner_references[0]
        self.assertEqual(ref.provider_id, "u-42")
        self.assertEqual(ref.username, "dana")
        self.assertFalse(hasattr(ref, "organization_id"))
        self.assertFalse(hasattr(ref, "permissions"))

    async def test_workflow_transitions_are_provider_terms_and_explicit(self) -> None:
        transitions = await self.source.available_transitions(self.source.snapshot.identity)
        self.assertEqual(
            self.suite.validate_transition_names(self.source, transitions),
            ("start_progress", "resolve"),
        )

    def test_transition_validation_rejects_duplicate_or_empty_names(self) -> None:
        with self.assertRaises(TaskSourceConformanceError) as duplicate:
            self.suite.validate_transition_names(self.source, ("resolve", "resolve"))
        self.assertEqual(duplicate.exception.code, "duplicate_workflow_transition")

        with self.assertRaises(TaskSourceConformanceError) as empty:
            self.suite.validate_transition_names(self.source, ("resolve", " "))
        self.assertEqual(empty.exception.code, "invalid_workflow_transition")


if __name__ == "__main__":
    unittest.main()
