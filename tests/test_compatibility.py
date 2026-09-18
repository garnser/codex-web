from __future__ import annotations

import unittest

from codex_web.action_providers import ACTION_PROVIDER_CONTRACT
from types import SimpleNamespace

from codex_web.compatibility import (
    CANONICAL_EVENT_CONTRACT,
    CanonicalEventEnvelope,
    ContractCompatibilityError,
    ContractSpec,
    ContractVersion,
    MigrationRegistry,
    negotiate,
)
from codex_web.models import TaskSourceIdentity, WorkItemState
from codex_web.services.action_providers import ActionProviderRegistry
from codex_web.services.task_source_runtime import TaskSourceRegistry, TaskSourceResolutionError
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.services.task_sources import TaskSourceCapabilities, TaskSourceEvent


class CompatibilityTests(unittest.TestCase):
    def test_contract_versions_are_exact_and_fail_closed(self) -> None:
        spec = ContractSpec(
            name="example",
            current="1.1",
            supported=("1.0", "1.1"),
            deprecated=("1.0",),
        )

        negotiated = negotiate("payload", version="1.0", spec=spec)

        self.assertEqual(negotiated.version, "1.0")
        self.assertTrue(negotiated.deprecated)
        with self.assertRaises(ContractCompatibilityError):
            spec.require("1.2")
        with self.assertRaises(ContractCompatibilityError):
            spec.require("2.0")

    def test_action_provider_1_1_keeps_legacy_1_0_migration_window(self) -> None:
        self.assertEqual(ACTION_PROVIDER_CONTRACT.current, "1.1")
        self.assertEqual(ACTION_PROVIDER_CONTRACT.supported, ("1.0", "1.1"))
        self.assertEqual(ACTION_PROVIDER_CONTRACT.deprecated, ("1.0",))
        self.assertEqual(str(ACTION_PROVIDER_CONTRACT.require("1.0")), "1.0")
        self.assertEqual(str(ACTION_PROVIDER_CONTRACT.require("1.1")), "1.1")
        with self.assertRaises(ContractCompatibilityError):
            ACTION_PROVIDER_CONTRACT.require("1.2")

    def test_version_parser_normalizes_and_orders(self) -> None:
        self.assertEqual(str(ContractVersion.parse("1")), "1.0")
        self.assertLess(ContractVersion.parse("1.9"), ContractVersion.parse("2.0"))
        with self.assertRaises(ValueError):
            ContractVersion.parse("1.2.3")

    def test_canonical_event_rejects_incompatible_schema(self) -> None:
        envelope = CanonicalEventEnvelope(
            event_id="evt-1",
            event_type="work_item.updated",
            occurred_at=1.0,
            source="test",
            payload={"ref": "TASK-1"},
        )
        self.assertEqual(envelope.schema_version, CANONICAL_EVENT_CONTRACT.current)

        with self.assertRaisesRegex(ValueError, "Unsupported canonical-event version '2.0'"):
            CanonicalEventEnvelope(
                schema_version="2.0",
                event_id="evt-2",
                event_type="work_item.updated",
                occurred_at=2.0,
                source="test",
            )

    def test_task_source_event_rejects_future_version(self) -> None:
        identity = TaskSourceIdentity(
            source_type="reference",
            source_instance="fixture",
            external_id="TASK-1",
        )
        self.assertEqual(
            TaskSourceEvent(identity=identity, event_type="updated").schema_version,
            "1.0",
        )
        with self.assertRaises(ContractCompatibilityError):
            TaskSourceEvent(
                identity=identity,
                event_type="updated",
                schema_version="9.0",
            )

    def test_migrations_are_ordered_and_target_idempotent(self) -> None:
        registry = MigrationRegistry("example-record")
        registry.register(
            "0.0",
            "0.5",
            lambda payload: {**payload, "owner": payload.get("owner") or "unassigned"},
        )
        registry.register(
            "0.5",
            "1.0",
            lambda payload: {**payload, "schema_version": "1.0"},
        )

        migrated = registry.assert_idempotent(
            {"id": "item-1"},
            from_version="0.0",
            to_version="1.0",
        )

        self.assertEqual(
            migrated,
            {"id": "item-1", "owner": "unassigned", "schema_version": "1.0"},
        )
        with self.assertRaises(ContractCompatibilityError):
            registry.migrate({}, from_version="9.0", to_version="10.0")

    def test_action_provider_contract_is_exact_and_registry_rejects_future_version(self) -> None:
        self.assertEqual(ACTION_PROVIDER_CONTRACT.current, "1.1")

        class IncompatibleProvider:
            contract_version = "2.0"
            provider_type = "future"
            provider_instance = "test"

            @staticmethod
            def actions():
                return ()

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            registry = ActionProviderRegistry(
                ActionProviderStateStore(
                    SQLiteStateStore(Path(directory) / "state.sqlite3")
                )
            )
            with self.assertRaises(ContractCompatibilityError):
                registry.register(IncompatibleProvider())

    def test_task_source_registry_accepts_legacy_v1_adapter_and_rejects_incompatible_one(self) -> None:
        state = WorkItemState(
            ref="TASK-1",
            source_identity=TaskSourceIdentity(
                source_type="reference",
                source_instance="fixture",
                external_id="TASK-1",
            ),
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        registry = TaskSourceRegistry()
        legacy = SimpleNamespace(
            source_type="reference",
            source_instance="fixture",
            capabilities=TaskSourceCapabilities(),
        )
        registry.register("reference", lambda _: legacy)
        self.assertIs(registry.resolve(state, required=True), legacy)

        incompatible = SimpleNamespace(
            source_type="reference",
            source_instance="fixture",
            contract_version="2.0",
            capabilities=TaskSourceCapabilities(),
        )
        registry.register("reference", lambda _: incompatible)
        with self.assertRaises(TaskSourceResolutionError):
            registry.resolve(state, required=True)


if __name__ == "__main__":
    unittest.main()
