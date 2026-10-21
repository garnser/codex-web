from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.state_store import build_state_store
from tests import test_postgres_connection_pool as pool_fixture


class ExistingStateStoreTests(unittest.TestCase):
    def test_missing_sqlite_does_not_create_directories_or_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'absent'/'state.sqlite3'
            with self.assertRaisesRegex(RuntimeError, 'Existing canonical'):
                build_state_store(sqlite_path=path, backend='sqlite', initialize=False)
            self.assertFalse(path.parent.exists())

    def test_existing_empty_or_wrong_sqlite_is_not_initialized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'empty.sqlite3'
            path.touch()
            with self.assertRaises(sqlite3.OperationalError):
                SQLiteStateStore(path, initialize=False)
            self.assertEqual(path.read_bytes(), b'')
            self.assertEqual(list(path.parent.iterdir()), [path])
            ordinary = SQLiteStateStore(path)
            with ordinary._connection() as connection:
                connection.execute("UPDATE state_metadata SET value='999' WHERE key='schema_version'")
            with self.assertRaisesRegex(RuntimeError, 'unsupported'):
                SQLiteStateStore(path, initialize=False)

    def test_existing_sqlite_opens_without_schema_initialization_and_never_recreates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.sqlite3'
            ordinary = SQLiteStateStore(path)
            ordinary.put('retained', {'value': 'ordinary private fixture'})
            with patch.object(SQLiteStateStore, '_initialize', side_effect=AssertionError('schema write')):
                existing = SQLiteStateStore(path, initialize=False)
            self.assertEqual(existing.get('retained'), {'value': 'ordinary private fixture'})
            path.unlink()
            with self.assertRaises(sqlite3.OperationalError):
                existing.get('retained')
            self.assertFalse(path.exists())

    def test_postgres_existing_mode_uses_readonly_validation_without_bootstrap(self):
        for row in (('1',), None, ('999',)):
            with self.subTest(row=row):
                pool = pool_fixture._Pool()
                cursor = Mock()
                cursor.fetchone.return_value = row
                context = MagicMock()
                context.__enter__.return_value = cursor
                pool.connection_value.cursor.return_value = context
                with patch.object(PostgresStateStore, '_initialize', side_effect=AssertionError('schema write')) as initialize:
                    if row == ('1',):
                        store = PostgresStateStore('private-fixture-dsn', connect=Mock(),
                            connection_pool=pool, initialize=False)
                        store.close()
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'unsupported'):
                            PostgresStateStore('private-fixture-dsn', connect=Mock(),
                                connection_pool=pool, initialize=False)
                initialize.assert_not_called()
                pool.connection_value.execute.assert_called_once_with(
                    'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                self.assertTrue(all(call.args[0].startswith('SELECT ')
                    for call in cursor.execute.call_args_list))
                self.assertTrue(pool.closed)
