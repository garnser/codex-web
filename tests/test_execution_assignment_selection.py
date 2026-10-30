"""Fresh execution lookup keeps every candidate and canonical ambiguity rules."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from codex_web.execution_workers import ExecutionAssignment
from codex_web.identity import AuthenticationActor
from codex_web.services.execution_workers import (
    AssignmentNotFoundError, ExecutionWorkerService, WorkerConflictError,
)
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.state_store import state_record_storage_key
from tests import test_autonomy_append_scaling as fixtures


class AssignmentSelectionContract:
    def prepare(self):
        self.actor = AuthenticationActor(identity_id='requester', principal_kind='human',
            organization_id='local', workspace_id='default', assurance='mfa')
        self.store = ExecutionWorkerStore(self.backend)
        self.store._ensure_records()
        self.service = ExecutionWorkerService(self.store)

    @staticmethod
    def row(identity='selected', execution='current', **changes):
        value = dict(id=identity, organization_id='local', workspace_id='default',
            subject={'kind':'thread', 'ref':'private-thread'}, execution_id=execution,
            resource_ids=(), execution_contract_version='1.0', required_capabilities=(),
            sandbox='read-only', approval_policy='never', network={}, limits={},
            created_by='requester', created_at=1.0, updated_at=1.0)
        value.update(changes)
        return ExecutionAssignment.model_validate(value).model_dump(mode='json')

    def put(self, *rows):
        self.backend.record_replace(self.store.assignment_namespace,
                                    {str(row['id']):row for row in rows})

    def lookup(self, execution='current'):
        return self.service.assignment_for_execution(execution, actor=self.actor)

    def raw(self, key, text):
        pg = isinstance(self.backend, PostgresStateStore)
        table, bind = ('codex_state_documents','%s') if pg else ('state_documents','?')
        self.backend.record_apply(self.store.assignment_namespace, upserts={key:{}})
        with self.backend._connection() as connection:
            connection.execute(f'UPDATE {table} SET payload={bind} WHERE namespace={bind}',
                (text, state_record_storage_key(self.store.assignment_namespace, key)))

    def test_scoped_lookup_validates_only_matching_execution_candidates(self):
        # Old list_assignments validates all 159 unrelated complete records;
        # selected lookup must validate both matches, not merely return newest.
        rows = [self.row('historical-'+str(i), 'history-'+str(i)) for i in range(159)]
        rows += [self.row('older', created_at=10, status='lost', project_id='other-project'),
                 self.row('newer', created_at=20, status='running', project_id='current-project')]
        self.put(*rows)
        seen = []
        validate = ExecutionAssignment.model_validate
        def observed(raw, *args, **kwargs):
            seen.append(raw['id'])
            return validate(raw, *args, **kwargs)
        with patch.object(ExecutionAssignment, 'model_validate', side_effect=observed):
            result = self.lookup()
        self.assertEqual(result.id, 'newer')
        self.assertCountEqual(seen, ('older','newer'))

    def test_all_statuses_projects_and_ties_keep_existing_sort_semantics(self):
        self.put(self.row('a', status='succeeded', project_id='first', created_at=10),
                 self.row('b', status='cancelled', project_id='second', created_at=10),
                 self.row('old', status='lost', created_at=9),
                 self.row('foreign', organization_id='foreign', created_at=999),
                 self.row('workspace', workspace_id='foreign', created_at=999),
                 self.row('different-execution', execution='other', created_at=999))
        self.assertEqual(self.lookup().id, 'b')
        rows = self.store.assignments_for_execution('current', organization_id='local', workspace_id='default')
        self.assertCountEqual([r.id for r in rows], ('a','b','old'))

    def test_every_matching_repository_scope_and_target_must_agree(self):
        scope = dict(organization_id='local', workspace_id='default', project_id='p',
                     writable_repository_ids=['repo'], source='explicit')
        target = dict(organization_id='local', workspace_id='default', project_id='p',
                      mutable_repository_id='repo', source='explicit')
        for field, value in (('repository_scope',scope), ('repository_target',target)):
            with self.subTest(field=field):
                self.put(self.row('newest', created_at=20),
                         self.row('old-conflict', created_at=1, status='failed', **{field:value}))
                with self.assertRaisesRegex(WorkerConflictError, 'disagree on repository authority'):
                    self.lookup()
                self.put(self.row('older', created_at=1, **{field:value}),
                         self.row('newer', created_at=20, **{field:value}))
                self.assertEqual(self.lookup().id, 'newer')

    def test_missing_and_nonstring_execution_keep_original_not_found(self):
        self.put(self.row())
        for identity in ('missing', None, [], {'not':'an ID'}):
            with self.subTest(identity=identity), self.assertRaises(AssignmentNotFoundError):
                self.lookup(identity)

    def test_selected_complete_model_and_invalid_selector_headers_fail_closed(self):
        self.put(self.row())
        selected = self.row('broken-selected')
        selected.pop('sandbox')
        self.backend.record_apply(self.store.assignment_namespace, upserts={'broken-selected':selected})
        with self.assertRaises(ValidationError):
            self.lookup()
        for field, value in (('id',None), ('execution_id',17),
                             ('organization_id',None), ('workspace_id',[])):
            with self.subTest(field=field):
                self.put(self.row())
                broken = self.row('unrelated', execution='other')
                broken[field] = value
                self.backend.record_apply(self.store.assignment_namespace, upserts={'malformed':broken})
                with self.assertRaises(ValidationError):
                    self.lookup()
        self.put(self.row())
        self.raw('not-object', '[1,2]')
        with self.assertRaises(ValidationError):
            self.lookup()

    def test_unselected_body_is_not_a_global_integrity_scan(self):
        self.put(self.row())
        unrelated = self.row('unrelated', execution='other')
        unrelated.pop('sandbox')
        self.backend.record_apply(self.store.assignment_namespace, upserts={'unrelated':unrelated})
        self.assertEqual(self.lookup().id, 'selected')
        with self.assertRaises(ValidationError):
            self.store.list_assignments()

    def test_each_call_observes_current_conflicts_and_removal(self):
        self.put(self.row())
        self.assertEqual(self.lookup().id, 'selected')
        conflict = self.row('new-conflict', created_at=20,
            repository_target=dict(organization_id='local', workspace_id='default', project_id='p',
                                   mutable_repository_id='repo', source='explicit'))
        self.backend.record_apply(self.store.assignment_namespace, upserts={conflict['id']:conflict})
        with self.assertRaises(WorkerConflictError):
            self.lookup()
        self.backend.record_replace(self.store.assignment_namespace, {})
        with self.assertRaises(AssignmentNotFoundError):
            self.lookup()

    def test_custom_list_owners_and_backend_readers_retain_their_checks(self):
        self.put(self.row())
        custom = ExecutionAssignment.model_validate(self.row('custom', created_at=99))
        with patch.object(self.store, 'list_assignments', return_value=[custom]) as owner:
            self.assertEqual(self.lookup().id, 'custom')
            owner.assert_called_once_with()
        with patch.object(ExecutionWorkerStore, 'list_assignments', return_value=[custom]) as owner:
            self.assertEqual(self.lookup().id, 'custom')
            owner.assert_called_once_with()
        with patch.object(self.backend, 'record_items', return_value={'custom':custom.model_dump(mode='json')}) as owner:
            self.assertEqual(self.lookup().id, 'custom')
            owner.assert_called_once_with(self.store.assignment_namespace)
        other = ExecutionWorkerStore(self.backend)
        with patch.object(self.store, 'list_assignments', other.list_assignments):
            self.assertEqual(self.lookup().id, 'selected')

    def test_backend_without_explicit_capability_uses_original_list(self):
        self.put(self.row())
        with patch.object(self.backend, 'supports_execution_assignment_selection', False), \
                patch.object(self.backend, 'execution_assignment_rows', side_effect=AssertionError('unsupported fast path')):
            self.assertEqual(self.lookup().id, 'selected')

    def test_legacy_state_migration_remains_full_and_canonical(self):
        legacy = {'schema_version':'1.14', 'workers':[], 'assignments':[self.row()],
                  'events':[], 'enrollments':[]}
        self.backend.put(self.store.namespace, legacy)
        self.assertEqual(self.lookup().id, 'selected')
        self.assertIsNone(self.backend.get(self.store.namespace))
        self.assertEqual(self.store.assignment('selected').execution_id, 'current')

    def test_duplicate_selector_keys_follow_original_json_last_value(self):
        self.put(self.row())
        row = self.row('duplicate', execution='current', created_at=20)
        raw = json.dumps(row).replace('"execution_id": "current"',
                                     '"execution_id":"other","execution_id":"current"')
        self.raw('duplicate', raw)
        self.assertEqual(self.lookup().id, 'duplicate')
        raw = json.dumps(row).replace('"execution_id": "current"',
                                     '"execution_id":"current","execution_id":"other"')
        self.raw('duplicate', raw)
        self.assertEqual(self.lookup().id, 'selected')

    def test_legacy_json_conversion_fallback_preserves_enclosing_owner(self):
        self.put(self.row(), self.row('legacy', execution='other', failure_message='before\0after'))
        def update(state):
            self.assertEqual(self.lookup().id, 'selected')
            return {'committed':True}
        self.backend.update('selector-sentinel', update, default={})
        self.assertEqual(self.backend.get('selector-sentinel'), {'committed':True})


class SQLiteExecutionAssignmentSelectionTests(AssignmentSelectionContract, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.backend = SQLiteStateStore(Path(directory.name)/'state.sqlite')
        self.prepare()


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'PostgreSQL test DSN required')
class PostgresExecutionAssignmentSelectionTests(AssignmentSelectionContract, unittest.TestCase):
    def setUp(self):
        fixtures.PostgresAutonomyAppendTests.setUp(self)
        self.prepare()

    def test_nonconversion_database_refusals_are_not_reinterpreted_or_retried(self):
        from contextlib import contextmanager
        self.put(self.row())
        for sqlstate in ('57014', '08006', None):
            class Refusal(RuntimeError):
                pass
            refusal = Refusal('synthetic closed storage refusal')
            refusal.sqlstate = sqlstate
            with self.subTest(sqlstate=sqlstate), self.backend._connection() as connection:
                class Cursor:
                    def __init__(self, cursor):
                        self.cursor = cursor
                    def __enter__(self):
                        self.cursor.__enter__()
                        return self
                    def __exit__(self, *args):
                        return self.cursor.__exit__(*args)
                    def __getattr__(self, name):
                        return getattr(self.cursor, name)
                    def execute(self, statement, parameters):
                        if 'WITH parsed AS MATERIALIZED' in statement:
                            raise refusal
                        return self.cursor.execute(statement, parameters)
                class Connection:
                    def cursor(self):
                        return Cursor(connection.cursor())
                    def transaction(self):
                        return connection.transaction()
                @contextmanager
                def read():
                    yield Connection()
                with patch.object(self.backend, '_read_connection', read):
                    # Call the backend directly: enclosing domain migration
                    # probes are unrelated to this exact failure boundary.
                    with self.assertRaises(Refusal) as caught:
                        self.backend.execution_assignment_rows(self.store.assignment_namespace,
                            'current', organization_id='local', workspace_id='default')
                self.assertIs(caught.exception, refusal)
