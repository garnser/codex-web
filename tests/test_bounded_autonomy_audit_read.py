"""Bound actual canonical audit reads without inducing an OOM (#2037)."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_web.autonomy_audit import AutonomyAuditPayload, AutonomyAuditState
from codex_web.services.autonomy_audit import AutonomyAuditService, AutonomyAuditError
from codex_web.storage.autonomy_audit import AutonomyAuditStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class BoundedAuditReadContract:
    """Also inherited by the existing real PostgreSQL CI qualification class."""
    def audit_fixture(self):
        store = AutonomyAuditStore(self.backend)
        service = AutonomyAuditService(store)
        selected = []
        for number in range(40):
            selected.append(store.append(AutonomyAuditPayload(kind='cycle',
                organization_id='org-a', workspace_id='ws-a', cycle_id=f'a-{number}',
                occurred_at=float(number), actor_identity_id='actor-a', details={'observed':'safe'})))
            store.append(AutonomyAuditPayload(kind='cycle', organization_id='foreign',
                workspace_id='ws-a', cycle_id=f'b-{number}', occurred_at=float(number)))
            store.append(AutonomyAuditPayload(kind='cycle', organization_id='org-a',
                workspace_id='other', cycle_id=f'c-{number}', occurred_at=float(number)))
        return store, service, selected

    def no_history(self):
        stack = ExitStack()
        stack.enter_context(patch.object(self.backend,'get',side_effect=AssertionError('unbounded namespace hydration')))
        stack.enter_context(patch.object(self.backend,'record_items',side_effect=AssertionError('unbounded record scan')))
        return stack

    def test_list_scopes_and_orders_before_hydration_without_loading_history(self):
        store, service, selected = self.audit_fixture()
        # OLD uses the same existing public service call and fails this real
        # canonical-read tripwire, before any candidate-only method is invoked.
        with self.no_history():
            rows = service.list_records(organization_id='org-a',workspace_id='ws-a',limit=2)
        self.assertEqual(rows,tuple(reversed(selected[-2:])))
        self.assertEqual(service.list_records(organization_id='missing',workspace_id='ws-a',limit=2),())

    def test_public_redaction_and_point_read_never_reload_history(self):
        store, service, selected = self.audit_fixture()
        for _ in range(3):
            store.append(AutonomyAuditPayload(kind='redaction',organization_id='org-a',workspace_id='ws-a',
                occurred_at=100.0,target_audit_record_id=selected[-1].id))
        # Foreign redaction cannot redact another partition's record.
        store.append(AutonomyAuditPayload(kind='redaction',organization_id='foreign',workspace_id='ws-a',
            occurred_at=100.0,target_audit_record_id=selected[-2].id))
        with self.no_history():
            original = service.get(selected[-1].id,organization_id='org-a',workspace_id='ws-a')
            page = service.public_records((original,selected[-2]))
            with self.assertRaises(AutonomyAuditError):
                service.get(original.id,organization_id='foreign',workspace_id='ws-a')
        self.assertEqual(original,selected[-1])
        self.assertTrue(page[0]['redacted'])
        self.assertIsNone(page[0]['payload']['actor_identity_id'])
        self.assertEqual(page[0]['payload']['details'],{'redacted':True})
        self.assertEqual(page[0]['record_hash'],original.record_hash)
        self.assertFalse(page[1]['redacted'])


class BoundedAutonomyAuditReadTests(BoundedAuditReadContract,unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.backend=SQLiteStateStore(Path(temp.name)/'audit.sqlite')

    def test_sqlite_query_decodes_only_selected_payloads(self):
        store,service,selected=self.audit_fixture()
        with patch.object(self.backend,'_decode',wraps=self.backend._decode) as decode:
            rows=service.list_records(organization_id='org-a',workspace_id='ws-a',limit=3)
        self.assertEqual(rows,tuple(reversed(selected[-3:])))
        # schema/meta controls plus bounded returned rows, not all120audit rows.
        self.assertLessEqual(decode.call_count,6)

    def test_limit_clamping_and_empty_namespace_do_not_initialize_history(self):
        store=AutonomyAuditStore(self.backend);service=AutonomyAuditService(store)
        with self.no_history():
            self.assertEqual(service.list_records(organization_id='org-a',workspace_id='ws-a'),())
        self.assertFalse(self.backend.contains(store.namespace))
        store,service,selected=self.audit_fixture()
        self.assertEqual(service.list_records(organization_id='org-a',workspace_id='ws-a',limit=0),(selected[-1],))
        self.assertEqual(len(service.list_records(organization_id='org-a',workspace_id='ws-a',limit=99999)),40)

    def test_legacy_envelope_refuses_without_reading_or_migrating_it(self):
        store=AutonomyAuditStore(self.backend);service=AutonomyAuditService(store)
        self.backend.put(store.namespace,AutonomyAuditState().model_dump(mode='json'))
        with self.no_history(),patch.object(store,'_decode',side_effect=AssertionError('legacy hydration')):
            with self.assertRaisesRegex(ValueError,'keyed canonical'):
                service.list_records(organization_id='org-a',workspace_id='ws-a',limit=1)
        self.assertFalse(self.backend.record_collection_exists(store.namespace))

    def test_selected_row_scope_mismatch_and_schema_are_not_accepted(self):
        store,service,selected=self.audit_fixture()
        key=store._record_key(117)  # selected a39 in interleaved a,b,c appends.
        raw=self.backend.record_get(store.namespace,key)
        self.assertEqual(raw['id'],selected[-1].id)
        raw['payload']['organization_id']='foreign'
        self.backend.record_apply(store.namespace,upserts={key:raw})
        with self.assertRaisesRegex(ValueError,'result changed'):
            service.list_records(organization_id='org-a',workspace_id='ws-a',limit=1)
        self.backend.record_apply(store.namespace,upserts={'schema_version':'999.0'})
        with self.assertRaises(ValueError):
            service.list_records(organization_id='org-a',workspace_id='ws-a',limit=1)

    def test_duplicate_exact_id_is_ambiguous_not_arbitrary_first(self):
        store,service,selected=self.audit_fixture()
        raw=self.backend.record_get(store.namespace,store._record_key(117))
        self.backend.record_apply(store.namespace,upserts={store._record_key(999):raw})
        with self.assertRaises(ValueError):
            service.get(selected[-1].id,organization_id='org-a',workspace_id='ws-a')

    def test_control_reads_do_not_load_record_history_and_chain_verification_remains(self):
        store,service,selected=self.audit_fixture()
        before=store.load()
        with self.no_history():
            self.assertEqual(service.list_checkpoints(organization_id='org-a',workspace_id='ws-a'),())
            self.assertEqual(service.list_signals(organization_id='org-a',workspace_id='ws-a'),())
        self.assertEqual(store.load(),before)
        self.assertEqual(service.verify(organization_id='org-a',workspace_id='ws-a').status,'verified')
        raw=self.backend.record_get(store.namespace,store._record_key(117))
        raw['payload']['details']={'changed':'tamper'}
        self.backend.record_apply(store.namespace,upserts={store._record_key(117):raw})
        self.assertEqual(service.verify(organization_id='org-a',workspace_id='ws-a').status,'failed')

    def test_actual_http_list_serialization_is_bounded_and_authorization_unchanged(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from codex_web.api.autonomy_audit import build_autonomy_audit_router
        from codex_web.identity import AuthenticationActor,AuthenticationAssurance,MembershipRole,PrincipalKind
        store,service,selected=self.audit_fixture()
        actor=AuthenticationActor(identity_id='admin-a',principal_kind=PrincipalKind.HUMAN,
            organization_id='org-a',workspace_id='ws-a',roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA)
        app=FastAPI()
        @app.middleware('http')
        async def authenticate(request,call_next):
            if request.headers.get('x-private-actor')=='admin':request.state.identity_actor=actor
            elif request.headers.get('x-private-actor')=='member':request.state.identity_actor=actor.model_copy(update={'roles':()})
            return await call_next(request)
        app.include_router(build_autonomy_audit_router(service))
        with TestClient(app) as client,self.no_history():
            self.assertEqual(client.get('/api/autonomy/audit?limit=2').status_code,401)
            self.assertEqual(client.get('/api/autonomy/audit?limit=2',headers={'x-private-actor':'member'}).status_code,403)
            response=client.get('/api/autonomy/audit?limit=2',headers={'x-private-actor':'admin'})
            self.assertEqual(response.status_code,200)
            self.assertEqual([x['id'] for x in response.json()['items']],[x.id for x in reversed(selected[-2:])])
            self.assertEqual(response.json()['count'],2)
            store.append(AutonomyAuditPayload(kind='redaction',organization_id='org-a',workspace_id='ws-a',
                occurred_at=100.0,target_audit_record_id=selected[-1].id))
            point=client.get('/api/autonomy/audit/'+selected[-1].id,headers={'x-private-actor':'admin'})
            self.assertEqual(point.status_code,200)
            self.assertTrue(point.json()['item']['redacted'])
            self.assertEqual(point.json()['item']['payload']['details'],{'redacted':True})
