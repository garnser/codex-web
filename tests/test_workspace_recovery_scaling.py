from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from codex_web.execution_workspaces import ExecutionWorkspaceLease, LeaseMode
from codex_web.identity import TenantScope
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def lease(identifier="lease", **updates):
    return ExecutionWorkspaceLease(
        id=identifier, execution_workspace_id="execution-workspace",
        organization_id="org", workspace_id="ws", work_item_ref="work:item",
        execution_id="execution", owner_identity_id="owner", resource_ids=("resource",),
        mode=LeaseMode.WRITE, acquired_at=10, expires_at=20,
    ).model_copy(update=updates).model_dump(mode="json")


class RecoveryProbeContract:
    def store(self):
        store = ExecutionWorkspaceStateStore(self.backend)
        store._ensure_records()
        return store

    def test_noop_recovery_does_not_read_or_write_history(self):
        store = self.store()
        self.backend.record_apply(store.lease_namespace, upserts={"lease": lease(expires_at=100)})
        # Retained history must be irrelevant to the no-op path, even at scale.
        self.backend.record_apply(store.event_namespace, upserts={str(i): {"retained": i} for i in range(5000)})
        original = self.backend.record_items
        def read(namespace):
            self.assertEqual(namespace, store.lease_namespace)
            return original(namespace)
        service = object.__new__(ExecutionWorkspaceService)
        service.store = store
        with patch.object(self.backend, "record_items", side_effect=read), \
             patch.object(self.backend, "record_apply", side_effect=AssertionError("write")), \
             patch.object(store, "update", side_effect=AssertionError("full update")), \
             patch.object(store, "load", side_effect=AssertionError("full load")):
            self.assertEqual(service.recover_expired(now=20, scope=TenantScope(organization_id="org", workspace_id="ws")), [])

    def test_boundary_scope_release_and_fresh_reads(self):
        store = self.store()
        scope = TenantScope(organization_id="org", workspace_id="ws")
        self.backend.record_apply(store.lease_namespace, upserts={"lease": lease()})
        self.assertFalse(store.has_expired_leases(now=19, scope=scope))
        self.assertTrue(store.has_expired_leases(now=20, scope=scope))
        self.assertFalse(store.has_expired_leases(now=20, scope=TenantScope(organization_id="other", workspace_id="ws")))
        self.assertFalse(store.has_expired_leases(now=20, scope=TenantScope(organization_id="org", workspace_id="other")))
        self.assertTrue(store.has_expired_leases(now=20))
        self.backend.record_apply(store.lease_namespace, upserts={"lease": lease(released_at=15)})
        self.assertFalse(store.has_expired_leases(now=20))
        # Writes through a separate facade are visible after a negative probe.
        other = ExecutionWorkspaceStateStore(self.backend)
        other.store.record_apply(other.lease_namespace, upserts={"new": lease("new")})
        self.assertTrue(store.has_expired_leases(now=20, scope=scope))

    def test_invalid_rows_fail_closed_even_after_expired_match(self):
        store = self.store()
        self.backend.record_apply(store.lease_namespace, upserts={"a": lease("a")})
        for raw in ({}, lease("wrong-id")):
            with self.subTest(raw=raw):
                self.backend.record_apply(store.lease_namespace, upserts={"z": raw})
                with self.assertRaises(ValueError):
                    store.has_expired_leases(now=20)

    def test_legacy_state_migrates_before_probe_and_markers_are_batched(self):
        store = ExecutionWorkspaceStateStore(self.backend)
        self.backend.put(store.namespace, {"schema_version": "1.0", "leases": [lease()]})
        with patch.object(self.backend, "record_collections_exist", wraps=self.backend.record_collections_exist) as batch, \
             patch.object(self.backend, "record_collection_exists", side_effect=AssertionError("individual marker read")):
            self.assertTrue(store.has_expired_leases(now=20))
            batch.assert_called_once_with((store.workspace_namespace, store.lease_namespace, store.event_namespace))
        self.assertEqual(store.load().leases[0].id, "lease")
        self.assertIsNone(self.backend.get(store.namespace))


class SQLiteRecoveryProbeTests(RecoveryProbeContract, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.backend = SQLiteStateStore(Path(directory.name) / "state.db")


@unittest.skipUnless(os.environ.get("CODEX_WEB_TEST_POSTGRES_DSN"), "PostgreSQL test DSN required")
class PostgresRecoveryProbeTests(RecoveryProbeContract, unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        dsn = os.environ["CODEX_WEB_TEST_POSTGRES_DSN"]
        schema = "workspace_probe_test_" + uuid.uuid4().hex
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        def cleanup():
            with psycopg.connect(dsn) as connection:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        self.addCleanup(cleanup)
        pool = ConnectionPool(dsn, min_size=1, max_size=4, kwargs={"options": f"-c search_path={schema}"})
        self.addCleanup(pool.close)
        self.backend = PostgresStateStore(dsn, connection_pool=pool)
