from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_web.agent_runtime import AgentSession, AgentSessionState
from codex_web.storage.agent_sessions import (
    AgentSessionConflictError,
    AgentSessionStore,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


def _session(
    index: int,
    *,
    organization_id: str = "org-a",
    workspace_id: str = "workspace-a",
    native_id: str | None = None,
    session_id: str | None = None,
) -> AgentSession:
    return AgentSession(
        id=session_id or f"agent-session-{index}",
        organization_id=organization_id,
        workspace_id=workspace_id,
        provider_id="provider-a",
        runtime_id="runtime-a",
        runtime_type="test-runtime",
        provider_native_session_id=native_id or f"native-{index}",
        project_id="project-a",
        created_at=float(index),
        updated_at=float(index),
    )


class AgentSessionIndexScalingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_native_lookup_at_observed_scale_never_reads_unrelated_sessions(self) -> None:
        sessions = [_session(index) for index in range(2_000)]
        self.sqlite.put(
            AgentSessionStore.namespace,
            AgentSessionState(sessions=sessions).model_dump(mode="json"),
        )
        store = AgentSessionStore(self.sqlite)

        # Perform the one-time legacy migration before observing steady-state
        # lookup behavior, then install one malformed unrelated row. A point
        # lookup must neither enumerate nor validate it.
        migrated = store.find_unique_by_native_id(
            "native-1777",
            provider_id="provider-a",
            runtime_id="runtime-a",
        )
        self.assertEqual(migrated.id if migrated else None, "agent-session-1777")
        self.sqlite.record_apply(
            AgentSessionStore.records_namespace,
            upserts={"sessions:unrelated-malformed": {"not": "a session"}},
        )

        with (
            patch.object(
                self.sqlite,
                "record_items",
                side_effect=AssertionError("whole session history was read"),
            ),
            patch.object(
                self.sqlite,
                "record_page",
                wraps=self.sqlite.record_page,
            ) as page,
            patch.object(
                self.sqlite,
                "record_get",
                wraps=self.sqlite.record_get,
            ) as get,
        ):
            result = store.find_unique_by_native_id(
                "native-1777",
                provider_id="provider-a",
                runtime_id="runtime-a",
            )

        self.assertEqual(result.id if result else None, "agent-session-1777")
        self.assertEqual(page.call_count, 1)
        self.assertEqual(get.call_count, 1)

    def test_native_rotation_updates_index_and_checkpoint_is_explicit(self) -> None:
        original = _session(1, native_id="native-before")
        legacy = AgentSessionState(sessions=[original]).model_dump(mode="json")
        self.sqlite.put(AgentSessionStore.namespace, legacy)
        store = AgentSessionStore(self.sqlite)

        self.assertIsNotNone(
            store.find_unique_by_native_id(
                "native-before",
                provider_id="provider-a",
                runtime_id="runtime-a",
            )
        )
        updated = original.model_copy(
            update={"provider_native_session_id": "native-after"}
        )
        store.upsert(updated)

        self.assertIsNone(
            store.find_unique_by_native_id(
                "native-before",
                provider_id="provider-a",
                runtime_id="runtime-a",
            )
        )
        self.assertEqual(
            store.find_unique_by_native_id(
                "native-after",
                provider_id="provider-a",
                runtime_id="runtime-a",
            ).id,
            original.id,
        )
        self.assertEqual(self.sqlite.get(AgentSessionStore.namespace), legacy)

        store.flush_legacy_mirror()
        checkpoint = self.sqlite.get(AgentSessionStore.namespace)
        self.assertEqual(
            checkpoint["sessions"][0]["provider_native_session_id"],
            "native-after",
        )

    def test_ambiguous_native_identity_fails_closed_across_tenants(self) -> None:
        store = AgentSessionStore(self.sqlite)
        first = _session(1, native_id="shared-native")
        second = _session(
            2,
            organization_id="org-b",
            workspace_id="workspace-b",
            native_id="shared-native",
        )
        store.upsert(first)
        store.upsert(second)

        self.assertIsNone(
            store.find_unique_by_native_id(
                "shared-native",
                provider_id="provider-a",
                runtime_id="runtime-a",
            )
        )
        self.assertEqual(
            store.find_by_native_id(
                "shared-native",
                organization_id="org-a",
                workspace_id="workspace-a",
                provider_id="provider-a",
                runtime_id="runtime-a",
            ).id,
            first.id,
        )

    def test_partial_filters_skip_other_valid_native_id_aliases(self) -> None:
        store = AgentSessionStore(self.sqlite)
        target = _session(1, native_id="shared-native")
        other_provider = _session(2, native_id="shared-native").model_copy(
            update={"provider_id": "other-provider", "runtime_id": "other-runtime"}
        )
        store.upsert(other_provider)
        store.upsert(target)
        for filters in ({"provider_id": "provider-a"}, {"runtime_id": "runtime-a"}):
            with self.subTest(filters=filters):
                result = store.find_by_native_id(
                    "shared-native", organization_id="org-a",
                    workspace_id="workspace-a", **filters,
                )
                self.assertEqual(result.id, target.id)
        self.assertIsNone(store.find_by_native_id(
            "shared-native", organization_id="org-a", workspace_id="workspace-a",
            provider_id="missing-provider",
        ))

    def test_partial_filter_advances_past_full_page_of_other_providers(self) -> None:
        store = AgentSessionStore(self.sqlite)
        for index in range(101):
            store.upsert(_session(index, native_id="shared-native").model_copy(
                update={"provider_id": "a-other-provider"}
            ))
        target = _session(102, native_id="shared-native").model_copy(
            update={"provider_id": "z-target-provider"}
        )
        store.upsert(target)
        result = store.find_by_native_id(
            "shared-native", organization_id="org-a", workspace_id="workspace-a",
            provider_id="z-target-provider",
        )
        self.assertEqual(result.id, target.id)

    def test_alias_with_wrong_encoded_identity_still_fails_closed(self) -> None:
        store = AgentSessionStore(self.sqlite)
        target = _session(1, native_id="shared-native")
        other = _session(2, native_id="shared-native").model_copy(
            update={"provider_id": "other-provider"}
        )
        store.upsert(target)
        store.upsert(other)
        self.sqlite.record_apply(store.records_namespace, upserts={
            store._native_key(target): {"session_id": other.id},
        })
        with self.assertRaisesRegex(ValueError, "index target mismatch"):
            store.find_by_native_id(
                "shared-native", organization_id="org-a", workspace_id="workspace-a",
                provider_id="provider-a",
            )

    def test_canonical_id_collision_still_cannot_cross_tenant_scope(self) -> None:
        store = AgentSessionStore(self.sqlite)
        store.upsert(_session(1, session_id="canonical-id"))

        with self.assertRaises(AgentSessionConflictError):
            store.upsert(
                _session(
                    2,
                    organization_id="org-b",
                    workspace_id="workspace-b",
                    session_id="canonical-id",
                )
            )


if __name__ == "__main__":
    unittest.main()
