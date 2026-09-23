from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.services.work_item_business_data_source import (
    WORK_ITEM_BUSINESS_DATA_SOURCE_INSTANCE,
    WORK_ITEM_BUSINESS_DATA_SOURCE_TYPE,
    WORK_ITEM_BUSINESS_OBJECT_TYPE,
    WorkItemBusinessDataSource,
)


class WorkItemBusinessDataSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.states = {
            "open": SimpleNamespace(
                ref="group/repo#1",
                project_id="project-a",
                current_stage="implementation_active",
                closed_at=None,
                updated_at=20.0,
            ),
            "closed": SimpleNamespace(
                ref="group/repo#2",
                project_id="project-a",
                current_stage="closed",
                closed_at=15.0,
                updated_at=15.0,
            ),
            "other": SimpleNamespace(
                ref="other/repo#1",
                project_id="project-b",
                current_stage="implementation_active",
                closed_at=None,
                updated_at=30.0,
            ),
        }
        self.source = WorkItemBusinessDataSource(
            source_instance=WORK_ITEM_BUSINESS_DATA_SOURCE_INSTANCE,
            project_id="project-a",
            load_states=lambda: self.states,
            load_projects=lambda: [
                SimpleNamespace(id="project-a", name="Project A")
            ],
        )

    async def test_discovery_projects_one_bounded_open_count(self) -> None:
        page = await self.source.discover_page(scope="project-a", limit=100)

        self.assertTrue(page.exhausted)
        self.assertIsNone(page.next_cursor)
        self.assertEqual(len(page.items), 1)
        snapshot = page.items[0]
        self.assertEqual(snapshot.object_type, WORK_ITEM_BUSINESS_OBJECT_TYPE)
        self.assertEqual(snapshot.external_id, "project-a")
        self.assertEqual(snapshot.entity_name, "Project A")
        self.assertEqual(snapshot.fields[0].source_field, "open_delivery_work_items")
        self.assertEqual(snapshot.fields[0].value, 1)
        self.assertEqual(snapshot.source_updated_at, 20.0)
        self.assertTrue(snapshot.source_revision)

    async def test_revision_changes_with_canonical_work_item_state(self) -> None:
        first = (await self.source.discover_page(scope="project-a")).items[0]
        self.states["open"].current_stage = "closed"
        self.states["open"].closed_at = 21.0
        self.states["open"].updated_at = 21.0

        second = (await self.source.discover_page(scope="project-a")).items[0]

        self.assertEqual(second.fields[0].value, 0)
        self.assertNotEqual(first.source_revision, second.source_revision)
        self.assertEqual(second.source_updated_at, 21.0)

    async def test_polling_ignores_previous_checkpoint_cursor(self) -> None:
        first = await self.source.discover_page(scope="project-a")

        repeated = await self.source.discover_page(
            scope="project-a",
            cursor=first.checkpoint,
        )

        self.assertEqual(len(repeated.items), 1)
        self.assertEqual(repeated.checkpoint, first.checkpoint)

    async def test_scope_and_record_identity_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "scope must match"):
            await self.source.discover_page(scope="project-b")
        with self.assertRaisesRegex(ValueError, "unsupported business object"):
            await self.source.read(object_type="account", external_id="project-a")
        with self.assertRaisesRegex(ValueError, "record not found"):
            await self.source.read(
                object_type=WORK_ITEM_BUSINESS_OBJECT_TYPE,
                external_id="project-b",
            )

    def test_declares_read_only_provider_contract(self) -> None:
        self.assertEqual(self.source.source_type, WORK_ITEM_BUSINESS_DATA_SOURCE_TYPE)
        self.assertFalse(self.source.credential_required)
        capabilities = {item.value for item in self.source.capabilities.supported}
        self.assertEqual(capabilities, {"discovery", "paged_discovery", "read"})
