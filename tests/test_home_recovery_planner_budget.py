"""Bounded Home query execution settings; no database or application imports."""
from contextlib import contextmanager, nullcontext
import json
import unittest

from codex_web.storage.home_intent_query import HomeIntentRecoveryQuery, recovery_statement
from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.state_store import StorageOperationBudget, storage_operation_budget


class ConversionError(Exception):
    sqlstate = '22P02'


class ReadConnection:
    def __init__(self, jit, error=None):
        self.settings = {'jit': jit, 'statement_timeout': '30s'}
        self.error = error
        self.query_observations = []
        self.result = []

    @contextmanager
    def transaction(self):
        before = dict(self.settings)
        try:
            yield
        except BaseException:
            self.settings = before  # PostgreSQL savepoint GUC rollback.
            raise

    def cursor(self):
        return nullcontext(self)

    def execute(self, statement, parameters=None):
        if statement.startswith('SHOW '):
            self.result = [(self.settings[statement[5:]],)]
        elif statement == "SELECT set_config('jit', 'off', true)":
            self.settings['jit'] = 'off'
        elif statement.startswith("SELECT set_config('jit',"):
            self.settings['jit'] = parameters[0]
        elif statement.startswith("SELECT set_config('statement_timeout',"):
            self.settings['statement_timeout'] = parameters[0]
        else:
            budget = storage_operation_budget.get()
            self.query_observations.append((statement, parameters, self.settings['jit'],
                                            budget.cancelled, budget.remaining()))
            if self.error:
                raise self.error
            self.result = [('original-namespace', json.dumps({'original': 'UNKNOWN'}))]
        return self

    def fetchone(self):
        return self.result[0]

    def fetchall(self):
        return self.result


class HomeRecoveryPlannerBudgetTests(unittest.TestCase):
    def invoke(self, connection):
        store = object.__new__(PostgresStateStore)
        store._read_connection = lambda: nullcontext(connection)
        query = HomeIntentRecoveryQuery(organization_id='org', workspace_id='ws',
                                        execution_id='original-execution')
        return store.home_intent_recovery_rows('action_intents.records.v1', query), query

    def test_query_disables_compilation_without_changing_original_sql_or_parent_budget(self):
        for initial in ('on', 'off'):
            with self.subTest(initial=initial):
                connection = ReadConnection(initial)
                parent = StorageOperationBudget(.5)
                token = storage_operation_budget.set(parent)
                try:
                    rows, query = self.invoke(connection)
                    self.assertIs(storage_operation_budget.get(), parent)
                finally:
                    storage_operation_budget.reset(token)
                self.assertEqual(rows, [{'original': 'UNKNOWN'}])
                self.assertEqual(connection.settings, {'jit': initial, 'statement_timeout': '30s'})
                sql, params, jit, cancellation, remaining = connection.query_observations[0]
                self.assertEqual((sql, params), recovery_statement(
                    'action_intents.records.v1', query, postgres=True))
                self.assertEqual(jit, 'off')
                self.assertIs(cancellation, parent.cancelled)
                self.assertGreater(remaining, 0)
                self.assertLessEqual(remaining, .5)

    def test_conversion_refusal_rolls_back_settings_and_remains_a_refusal(self):
        connection = ReadConnection('on', ConversionError())
        with self.assertRaisesRegex(ValueError, 'original JSON unavailable'):
            self.invoke(connection)
        self.assertEqual(connection.query_observations[0][2], 'off')
        self.assertEqual(connection.settings, {'jit': 'on', 'statement_timeout': '30s'})

    def test_unclassified_query_error_is_not_masked_and_settings_roll_back(self):
        error = RuntimeError('query failure')
        connection = ReadConnection('on', error)
        with self.assertRaises(RuntimeError) as caught:
            self.invoke(connection)
        self.assertIs(caught.exception, error)
        self.assertEqual(connection.settings, {'jit': 'on', 'statement_timeout': '30s'})
