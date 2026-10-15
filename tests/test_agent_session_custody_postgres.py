"""Actual PostgreSQL custody transactions; isolated CI test DSN only.

The inherited acceptance cases use the same canonical services as SQLite.
Two independent pools share a fresh schema; no PostgreSQL storage/locks mocked.
Absent local test DSN means unqualified/skipped, never a PostgreSQL PASS.
"""
import hashlib
import os
import uuid
import unittest

from tests import test_agent_session_custody as custody


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL test DSN required')
class PostgresSessionCustodyTests(custody.SessionCustodyTests):
    # The composed real-database broker performs many transactional reads.
    # Bound the test observer independently of the short SQLite fixture default;
    # production request, lease and assignment deadlines remain unchanged.
    broker_response_timeout = 10

    def state_store_factory(self, path):
        import psycopg
        from psycopg import sql
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        self.test_dsn = os.environ['CODEX_WEB_TEST_POSTGRES_DSN']
        self.test_schema = 'session_custody_test_' + uuid.uuid4().hex
        with psycopg.connect(self.test_dsn) as connection:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.test_schema)))
        def drop_schema():
            with psycopg.connect(self.test_dsn) as connection:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.test_schema)))
        self.addCleanup(drop_schema)
        pool = ConnectionPool(self.test_dsn, min_size=1, max_size=6,
            kwargs={'options': f'-c search_path={self.test_schema}'})
        self.addCleanup(pool.close)
        backend = PostgresStateStore(self.test_dsn, connection_pool=pool)
        backend.path = path  # Existing queue fixture's local fallback path only.
        self.primary_pool = pool
        return backend

    def peer_backend(self):
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        pool = ConnectionPool(self.test_dsn, min_size=1, max_size=6,
            kwargs={'options': f'-c search_path={self.test_schema}'})
        self.addCleanup(pool.close)
        with self.primary_pool.connection() as first, pool.connection() as second:
            first_pid = first.execute('SELECT pg_backend_pid()').fetchone()[0]
            second_pid = second.execute('SELECT pg_backend_pid()').fetchone()[0]
            self.assertNotEqual(first_pid, second_pid)
        return PostgresStateStore(self.test_dsn, connection_pool=pool)

    async def test_database_event_write_failure_rolls_back_session_on_other_connection(self):
        from psycopg import sql
        from codex_web.storage.state_store import state_record_storage_key
        peer = self.peer_backend()
        proof = self.capture()
        before = self.custody_rows()
        event_id = 'workerevent-session-custody-' + hashlib.sha256(
            f'{self.original_session.id}:{self.assignment.id}:{self.assignment.fence}'.encode()).hexdigest()
        key = state_record_storage_key(self.f.workers.store.event_namespace, event_id)
        # A real database rejection AFTER the Session delta is applied must roll
        # back the complete transaction, including visibility on another connection.
        with self.primary_pool.connection() as connection:
            connection.execute(sql.SQL('''CREATE FUNCTION reject_custody_event() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN IF NEW.namespace = {} THEN
                RAISE EXCEPTION 'injected custody event storage failure'; END IF; RETURN NEW; END $$''').format(sql.Literal(key)))
            connection.execute('''CREATE TRIGGER reject_custody_event BEFORE INSERT OR UPDATE
                ON codex_state_documents FOR EACH ROW EXECUTE FUNCTION reject_custody_event()''')
        with self.assertRaisesRegex(Exception, 'injected custody event storage failure'):
            self.complete(proof)
        self.assertEqual(self.custody_rows(), before)
        other = self.custody_on_backend(peer)
        self.assertEqual(other.sessions.get(self.original_session.id, self.actor), before[0])
        self.assertEqual(peer.record_items(self.f.workers.store.event_namespace), before[1])
