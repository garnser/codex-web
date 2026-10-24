"""Closed preparation metadata never changes pre-start or provider authority."""
import json
import unittest
from unittest.mock import AsyncMock
from fastapi import HTTPException
from codex_web.services.threads import ThreadBootstrapPrestartRefusal,bootstrap_preparation_diagnostic
from tests import test_thread_bootstrap_routing as fixtures
from tests.thread_bootstrap_composition_harness import build_real_thread_create

class ThreadBootstrapDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_guard_timeout_preserves_closed_stage_without_error_text(self):
        host,planner,manager,_,service=fixtures.ThreadBootstrapCreateTests()._service()
        def guard():raise TimeoutError('private credential-like exception text')
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
            await service.create(project_id='p1',bootstrap_token='a'*32,admission_guard=guard)
        diagnostic=bootstrap_preparation_diagnostic(caught.exception)
        self.assertEqual(diagnostic,{'bootstrap_preparation_stage':'admission_before_reservation',
            'bootstrap_exception_type':'TimeoutError','provider_start_called':False})
        self.assertEqual(caught.exception.status_code,503)
        self.assertEqual(planner.calls,[]);self.assertEqual(manager.started,[])
        self.assertEqual(host.events,[{'type':'thread_bootstrap_prestart_failed',**diagnostic,
            'project_id':'p1','bootstrap_id':'bootstrap-'+'a'*32,'execution_id':'thread-bootstrap-'+'a'*32}])
        self.assertNotIn('private credential',json.dumps([caught.exception.detail,host.events]))

    async def test_planner_failure_has_reservation_stage_and_unknown_class_is_not_reflected(self):
        host,planner,manager,_,service=fixtures.ThreadBootstrapCreateTests()._service()
        private_error=type('PrivateTenantCredentialError',(Exception,),{})
        def fail(**kwargs):raise private_error('untrusted prose')
        planner.prepare_bootstrap=fail
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:await service.create(project_id='p1')
        self.assertEqual(bootstrap_preparation_diagnostic(caught.exception),{
            'bootstrap_preparation_stage':'bootstrap_reservation','bootstrap_exception_type':'Exception','provider_start_called':False})
        self.assertEqual(manager.started,[])
        self.assertNotIn('PrivateTenant',json.dumps(caught.exception.detail))
        self.assertNotIn('untrusted prose',json.dumps(host.events))

    async def test_actual_workspace_allocator_failure_preserves_existing_http_refusal(self):
        f=build_real_thread_create(self)
        def failed_allocator(*args,**kwargs):raise TypeError('private allocator material')
        f.workspaces.backend.provision_git=failed_allocator
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
            await f.thread_service.create(project_id=f.project.id,repository_resource_id=f.repository.id)
        self.assertIsNone(bootstrap_preparation_diagnostic(caught.exception))
        self.assertEqual(caught.exception.status_code,503)
        self.assertEqual(caught.exception.detail['code'],'execution_preflight_blocked')
        self.assertEqual(f.started_assignments,[])
        self.assertEqual(f.process_session.requests,[])
        self.assertEqual(f.transport_host.events,[])
        self.assertEqual(caught.exception.detail['message'],'private allocator material')

    async def test_worker_start_failure_is_distinct_and_event_failure_does_not_mask_it(self):
        host,_,manager,_,service=fixtures.ThreadBootstrapCreateTests()._service()
        manager.start=AsyncMock(side_effect=OSError('private process error'))
        service.event_sink=lambda _:(_ for _ in ()).throw(RuntimeError('telemetry failed'))
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:await service.create(project_id='p1')
        self.assertEqual(bootstrap_preparation_diagnostic(caught.exception),{
            'bootstrap_preparation_stage':'worker_start','bootstrap_exception_type':'OSError','provider_start_called':False})
        self.assertIsInstance(caught.exception.__cause__,OSError)
        self.assertEqual(host.events,[])

    async def test_provider_timeout_and_spoofed_refusal_never_gain_prestart_diagnostics(self):
        for error in (TimeoutError('provider unknown'),ThreadBootstrapPrestartRefusal(status_code=503,detail={
            'code':'bootstrap_prestart_failed','bootstrap_preparation_stage':'worker_start',
            'bootstrap_exception_type':'TimeoutError','provider_start_called':False})):
            host,_,manager,_,service=fixtures.ThreadBootstrapCreateTests()._service(session=fixtures._Session(error=error))
            with self.assertRaises(Exception) as caught:await service.create(project_id='p1')
            self.assertIsNone(bootstrap_preparation_diagnostic(caught.exception))
            self.assertEqual(host.events,[])
            self.assertEqual(manager.completed[0][1]['succeeded'],False)

    async def test_http_refusal_shape_is_unchanged_and_has_no_generic_diagnostic(self):
        _,_,_,_,service=fixtures.ThreadBootstrapCreateTests()._service()
        detail={'code':'current_authority_denied','message':'existing canonical refusal'}
        def guard():raise HTTPException(status_code=403,detail=detail)
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:await service.create(project_id='p1',admission_guard=guard)
        self.assertEqual(caught.exception.status_code,403);self.assertEqual(caught.exception.detail,detail)
        self.assertIsNone(bootstrap_preparation_diagnostic(caught.exception))

    def test_diagnostic_projection_refuses_untrusted_shapes_and_extra_fields(self):
        valid={'code':'bootstrap_prestart_failed','bootstrap_preparation_stage':'bootstrap_reservation',
            'bootstrap_exception_type':'RuntimeError','provider_start_called':False,'private':'never projected'}
        self.assertEqual(len(bootstrap_preparation_diagnostic(ThreadBootstrapPrestartRefusal(status_code=503,detail=valid))),3)
        for key,value in (('bootstrap_preparation_stage',[]),('bootstrap_exception_type','UntrustedName'),
                          ('provider_start_called',True),('code','provider_error')):
            with self.subTest(key=key):
                self.assertIsNone(bootstrap_preparation_diagnostic(ThreadBootstrapPrestartRefusal(status_code=503,detail={**valid,key:value})))
        self.assertIsNone(bootstrap_preparation_diagnostic(HTTPException(status_code=503,detail=valid)))
