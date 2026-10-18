"""Fixed public setting refusal and real restricted PostgreSQL preview."""
import os
import importlib.util
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

PSYCOPG_AVAILABLE = importlib.util.find_spec('psycopg') is not None
if PSYCOPG_AVAILABLE:
    from psycopg.errors import InsufficientPrivilege

from codex_web.services.recovery_preservation_operations import (
    PreservationDatabaseSettingDenied, preservation_database_directory)
from tests import test_recovery_storage_operator as existing
from tests import test_action_intents as intent_fixture
from tests.test_recovery_scheduled_postgres import _PostgresFixture


@unittest.skipUnless(PSYCOPG_AVAILABLE, 'PostgreSQL driver extras required; selected by distributed-backends CI')
class SettingReadTests(unittest.TestCase):
    def test_denied_setting_uses_fixed_payload_free_error(self):
        connection = Mock()
        connection.execute.side_effect = InsufficientPrivilege('PRIVATE DRIVER SENTINEL /private/database')
        with self.assertRaises(PreservationDatabaseSettingDenied) as denied:
            preservation_database_directory(connection)
        self.assertEqual(denied.exception.code, 'preservation_database_setting_read_denied')
        self.assertNotIn('SENTINEL', str(denied.exception))
        self.assertNotIn('/private', str(denied.exception))
        self.assertTrue(denied.exception.__suppress_context__)
        connection.execute.assert_called_once_with('SHOW data_directory')

    def test_unrelated_database_error_is_not_reclassified(self):
        connection = Mock()
        original = RuntimeError('unrelated synthetic database failure')
        connection.execute.side_effect = original
        with self.assertRaises(RuntimeError) as denied:
            preservation_database_directory(connection)
        self.assertIs(denied.exception, original)

    def test_readable_setting_is_returned_without_override(self):
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = ('/synthetic/actual',)
        self.assertEqual(preservation_database_directory(connection), '/synthetic/actual')
        connection.execute.assert_called_once_with('SHOW data_directory')


@unittest.skipUnless(PSYCOPG_AVAILABLE, 'PostgreSQL driver extras required; selected by distributed-backends CI')
class PreviewSettingRefusalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = existing.StorageOperatorTests.asyncSetUp
    asyncTearDown = existing.StorageOperatorTests.asyncTearDown
    publish = existing.StorageOperatorTests.publish
    insert_opaque = existing.StorageOperatorTests.insert_opaque
    client = existing.StorageOperatorTests.client
    body = existing.StorageOperatorTests.body
    committed = existing.StorageOperatorTests.committed
    assert_original = existing.StorageOperatorTests.assert_original

    async def _assert_public_refusal(self):
        before = self.artifacts.store.load()
        async with self.client() as client:
            response = await client.post('/api/recovery/storage-preservation/preview', json=self.body())
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()['detail']
        self.assertEqual(detail['code'], 'preservation_database_setting_read_denied')
        self.assertEqual(detail['required_setting'], 'data_directory')
        self.assertFalse(detail['capacity_verified'])
        self.assertIn('operator must', detail['message'])
        self.assertNotIn('PRIVATE DRIVER SENTINEL', response.text)
        self.assertNotIn('/private/database', response.text)
        self.assertEqual(self.artifacts.store.load(), before)
        self.assertFalse(self.committed())
        self.assert_original()

    async def test_preview_returns_actionable_redacted_refusal_without_publication(self):
        connection = Mock()
        connection.execute.side_effect = InsufficientPrivilege('PRIVATE DRIVER SENTINEL /private/database')
        def denied_inventory():
            return preservation_database_directory(connection)
        with patch.object(self.operator.storage_provider.operations, 'inventory', side_effect=denied_inventory):
            await self._assert_public_refusal()
        connection.execute.assert_called_once_with('SHOW data_directory')


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL DSN required')
class PostgresPreviewSettingRefusalTests(_PostgresFixture, PreviewSettingRefusalTests):
    async def asyncSetUp(self):
        with patch.object(intent_fixture, 'SQLiteStateStore', self.state_store_factory):
            await super().asyncSetUp()
        self.addCleanup(self.sqlite.close)

    async def test_real_read_only_role_refuses_setting_without_grant_or_source_mutation(self):
        operations = self.operator.storage_provider.operations
        inventory = operations.inventory
        original_connection = self.sqlite._connection
        @contextmanager
        def restricted_connection(*args, **kwargs):
            with original_connection(*args, **kwargs) as connection:
                # PostgreSQL's existing read-only role can read synthetic
                # tables but has no pg_read_all_settings privilege. No role
                # creation, persistent GRANT, credential or live state change.
                connection.execute('SET LOCAL ROLE pg_read_all_data')
                yield connection
        def restricted_inventory():
            with patch.object(self.sqlite, '_connection', side_effect=restricted_connection):
                return inventory()
        with patch.object(operations, 'inventory', side_effect=restricted_inventory):
            await self._assert_public_refusal()
