from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_web.identity import TenantScope
from codex_web.models import WorkItemState
from codex_web.services.work_items import WorkItemService
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.work_item_list_index import WorkItemListIndex
from tests import test_work_item_async_reads as async_fixture


def _state(**kwargs):
    return WorkItemState(created_at=1, last_meaningful_update_at=1, **{
        "updated_at": 1, **kwargs,
    })


class WorkItemBatchReadsTests(unittest.TestCase):
    def test_sparse_owner_1728_records_uses_batches_and_preserves_canonical_filter(self):
        fixture = async_fixture.WorkItemAsyncReadTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = fixture.service
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStateStore(Path(tmp) / 'state.sqlite3')
            repository = ModelMapRepository(store, namespace='states',
                legacy_path=Path(tmp) / 'states.json', model=WorkItemState)
            states = {f'group/app#{i}': _state(ref=f'group/app#{i}',
                project_id='project-a', current_owner='james', updated_at=float(i+1))
                for i in range(1728)}
            store.record_replace('states', {key: state.model_dump(mode='json') for key,state in states.items()})
            service.work_item_list_index = WorkItemListIndex(store)
            service.work_item_list_index.rebuild(states)
            service.work_items.get_state = repository.get
            service.work_items.get_states = repository.get_many
            with patch.object(store, '_connect', wraps=store._connect) as connections, \
                 patch.object(store, 'record_get', wraps=store.record_get) as point, \
                 patch.object(store, 'record_get_many', wraps=store.record_get_many) as batch, \
                 patch.object(store, 'record_items', side_effect=AssertionError('catalog read')):
                result = service._list_sync(project_id='project-a', owner='larry',
                    stage=None, release_gate=None, q=None, scope=TenantScope(), limit=20, cursor=None)
            self.assertEqual(result['items'], [])
            self.assertFalse(result['hasMore'])
            self.assertEqual(point.call_count, 0)
            self.assertEqual(batch.call_count, 18)
            self.assertLess(connections.call_count, 100)
            self.assertTrue(all(len(call.args[1]) <= 100 for call in batch.call_args_list))

    def test_old_dependency_composition_uses_only_exact_canonical_getter_fallback(self):
        fixture = async_fixture.WorkItemAsyncReadTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with tempfile.TemporaryDirectory() as tmp:
            repository = ModelMapRepository(SQLiteStateStore(Path(tmp)/'state.sqlite3'),
                namespace='states', model=WorkItemState, legacy_path=Path(tmp)/'states.json')
            repository.store.record_replace('states', {
                key:state.model_dump(mode='json') for key,state in fixture.states.items()})
            service = fixture.service
            service.work_items.get_state = repository.get
            args = dict(project_id='project-a', owner='james', stage=None,
                        release_gate=None, scope=TenantScope(), limit=1)
            with patch.object(repository, 'get_many', wraps=repository.get_many) as batch:
                service._list_sync(**args)
                batch.assert_called_once()
            class CustomRepository(ModelMapRepository):
                def get(self, key):
                    return fixture.states.get(key)
            custom = CustomRepository(repository.store, namespace='states', model=WorkItemState,
                legacy_path=Path(tmp)/'states.json')
            service.work_items.get_state = custom.get
            with patch.object(custom, 'get_many', side_effect=AssertionError('custom getter inferred')):
                self.assertEqual(len(service._list_sync(**args)['items']), 1)

    def test_corrupt_unvisited_batch_row_does_not_fail_early_page_but_visited_row_raises(self):
        from pydantic import ValidationError
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStateStore(Path(tmp)/'state.sqlite3')
            repository = ModelMapRepository(store, namespace='states', model=WorkItemState,
                legacy_path=Path(tmp)/'states.json')
            valid = _state(ref='valid', project_id='p', updated_at=2)
            invalid = _state(ref='invalid', project_id='p', updated_at=1)
            store.record_replace('states', {'valid': valid.model_dump(mode='json'), 'invalid': {'bad': True}})
            index = WorkItemListIndex(store)
            index.rebuild({'valid': valid, 'invalid': invalid})
            args = dict(scope=TenantScope(), project_id='p', limit=1,
                get_state=repository.get, predicate=lambda _: True, get_states=repository.get_many)
            page, cursor, _ = index.page(**args, after=None)
            self.assertEqual(page, [valid])
            with self.assertRaises(ValidationError):
                index.page(**args, after=cursor)
            batch = repository.get_many(('valid', 'invalid'))
            self.assertIs(batch['valid'], batch['valid'])
            with self.assertRaises(ValidationError):
                batch['invalid']

    def test_selected_model_reads_skip_unrequested_invalid_records_and_filtered_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStateStore(Path(tmp)/'state.sqlite3')
            good = _state(ref='allowed', project_id='project-a')
            store.record_replace('states', {'allowed': good.model_dump(mode='json'), 'invalid': {'invalid':True}})
            repository = ModelMapRepository(store, namespace='states', model=WorkItemState,
                legacy_path=Path(tmp)/'legacy.json', key_filter=lambda key: key == 'allowed')
            self.assertEqual(repository.get_many(('allowed','invalid','missing','allowed')), {'allowed':good})
            with patch.object(store, 'record_collection_exists', side_effect=AssertionError('empty IO')):
                self.assertEqual(repository.get_many(()), {})
                self.assertEqual(repository.get_many(('invalid',)), {})
                with self.assertRaises(ValueError):
                    repository.get_many(('allowed',)*1001)

    def test_historical_store_without_batch_api_keeps_selected_point_read_fallback(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStateStore(Path(tmp)/'state.sqlite3')
            good = _state(ref='good', project_id='p')
            store.record_replace('states', {'good': good.model_dump(mode='json')})
            legacy = SimpleNamespace(record_collection_exists=store.record_collection_exists,
                record_get=store.record_get)
            repository = ModelMapRepository(legacy, namespace='states', model=WorkItemState,
                legacy_path=Path(tmp)/'legacy.json')
            self.assertEqual(dict(repository.get_many(('good', 'missing'))), {'good':good})

    def test_batch_page_matches_point_page_for_filters_budget_order_and_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStateStore(Path(tmp)/'state.sqlite3')
            index = WorkItemListIndex(store)
            states = {f'app#{i}': _state(ref=f'app#{i}', project_id='p',
                updated_at=float(i), current_owner='james' if i%7 else 'larry') for i in range(350)}
            index.rebuild(states)
            def batch(keys): return {key:states[key] for key in keys if key in states}
            args = dict(scope=TenantScope(), project_id='p', limit=10,
                get_state=states.get, predicate=lambda state: state.current_owner=='larry', scan_budget=25)
            cursor = None
            while True:
                expected=index.page(**args, after=cursor)
                actual=index.page(**args, after=cursor, get_states=batch)
                self.assertEqual(actual,expected)
                cursor=actual[1]
                if cursor is None: break

    def test_batch_page_repairs_missing_and_cross_scope_records_before_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            index=WorkItemListIndex(SQLiteStateStore(Path(tmp)/'state.sqlite3'))
            old=_state(ref='moved', project_id='p', updated_at=3)
            missing=_state(ref='missing', project_id='p', updated_at=2)
            valid=_state(ref='valid', project_id='p', updated_at=1)
            index.rebuild({state.ref:state for state in (old,missing,valid)})
            moved=old.model_copy(update={'organization_id':'other'})
            states={'moved':moved,'valid':valid}
            page,_,_=index.page(scope=TenantScope(),project_id='p',after=None,limit=20,
                get_state=lambda _: self.fail('point read'),predicate=lambda _:True,
                get_states=lambda keys:{key:states[key] for key in keys if key in states})
            self.assertEqual(page,[valid])
            self.assertIsNone(index.store.record_get(index.LOOKUP_NAMESPACE,'missing'))
            self.assertEqual(len(index.store.record_items(index.namespace(TenantScope(),'p'))),1)

    def test_sqlite_batch_one_connection_legacy_keyed_missing_and_escaped_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=SQLiteStateStore(Path(tmp)/'state.sqlite3')
            for keyed in (False,True):
                namespace='keyed' if keyed else 'legacy'
                payload={'a/%?':{'value':1},'unrequested':{'value':2}}
                if keyed: store.record_replace(namespace,payload)
                else: store.put(namespace,payload)
                with patch.object(store,'_connect',wraps=store._connect) as connect:
                    self.assertEqual(store.record_get_many(namespace,('a/%?','missing','a/%?')),{'a/%?':{'value':1}})
                    self.assertEqual(connect.call_count,1)
            with patch.object(store,'_connect',side_effect=AssertionError('IO')):
                self.assertEqual(store.record_get_many('keyed',()),{})
                with self.assertRaises(ValueError): store.record_get_many('keyed',('a',)*1001)
