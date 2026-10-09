from __future__ import annotations

from contextlib import contextmanager
import unittest
from unittest.mock import Mock, patch

from codex_web.storage.postgres_state import PostgresStateStore, shared_postgres_connection_pool, close_postgres_connection_pools


class _Pool:
    def __init__(self):
        self.connection_value = Mock()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    @contextmanager
    def connection(self):
        try:
            yield self.connection_value
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1

    def close(self):
        self.closed = True


class PostgresConnectionPoolTests(unittest.TestCase):
    def test_pool_checkout_reuses_connection_without_opening_or_closing_per_operation(self):
        pool = _Pool()
        connect = Mock(side_effect=AssertionError("new connection opened"))
        with patch.object(PostgresStateStore, "_initialize"):
            store = PostgresStateStore("test-dsn", connect=connect, connection_pool=pool)
        self.addCleanup(store.close)
        for _ in range(3):
            with store._connection() as connection:
                self.assertIs(connection, pool.connection_value)
        self.assertEqual(pool.commits, 3)
        connect.assert_not_called()
        pool.connection_value.close.assert_not_called()
        store.close()
        self.assertTrue(pool.closed)

    def test_exception_reaches_pool_transaction_boundary_before_connection_reuse(self):
        pool = _Pool()
        with patch.object(PostgresStateStore, "_initialize"):
            store = PostgresStateStore("test-dsn", connect=Mock(), connection_pool=pool)
        self.addCleanup(store.close)
        with self.assertRaisesRegex(RuntimeError, "mutation failed"):
            with store._connection():
                raise RuntimeError("mutation failed")
        with store._connection():
            pass
        self.assertEqual(pool.rollbacks, 1)
        self.assertEqual(pool.commits, 1)

    def test_custom_connection_factory_retains_existing_context_and_cleanup(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connect = Mock(return_value=connection)
        with patch.object(PostgresStateStore, "_initialize"):
            store = PostgresStateStore("test-dsn", connect=connect)
        with store._connection() as current:
            self.assertIs(current, connection)
        connect.assert_called_once_with("test-dsn")
        connection.__enter__.assert_called_once()
        connection.__exit__.assert_called_once()
        connection.close.assert_called_once()

    def test_failed_schema_initialization_closes_owned_pool(self):
        pool = _Pool()
        with patch.object(PostgresStateStore, "_initialize", side_effect=RuntimeError("schema failed")):
            with self.assertRaisesRegex(RuntimeError, "schema failed"):
                PostgresStateStore("test-dsn", connect=Mock(), connection_pool=pool)
        self.assertTrue(pool.closed)

    def test_shared_pool_is_partitioned_by_dsn_and_bounds_then_closed_once(self):
        import sys
        from types import SimpleNamespace
        pools = []
        def create(**kwargs):
            pool = _Pool()
            pool.kwargs = kwargs
            pools.append(pool)
            return pool
        with patch.dict(sys.modules, {"psycopg_pool": SimpleNamespace(ConnectionPool=create)}):
            first = shared_postgres_connection_pool("pool-test-a", max_size=2)
            self.assertIs(shared_postgres_connection_pool("pool-test-a", max_size=2), first)
            self.assertIsNot(shared_postgres_connection_pool("pool-test-b", max_size=2), first)
            self.assertIsNot(shared_postgres_connection_pool("pool-test-a", max_size=3), first)
            close_postgres_connection_pools()
        self.assertEqual(len(pools), 3)
        self.assertTrue(all(pool.closed for pool in pools))

    def test_pool_limits_reject_nonpositive_values(self):
        for kwargs in ({"pool_max_size": 0}, {"pool_timeout_seconds": 0}):
            with self.assertRaises(ValueError):
                PostgresStateStore("test-dsn", connect=Mock(), **kwargs)


if __name__ == "__main__":
    unittest.main()
