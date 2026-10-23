"""Synthetic pre-turn capture; never invokes the installed SDK or live queue."""
import asyncio
import copy
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError, create_model
from codex_web.models import ExecutionPreflightAttempt
from codex_web.services.queued_native_resume import QueuedNativeResumeCapture
from codex_web.services.queued_native_start import NativeStartAdmissionRefused
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from tests import test_queued_native_start as fixtures
from tests.test_execution_preflight import actor


class ResumeCases:
    def setUp(self):
        self.f = fixtures.QueuedNativeStartTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.service = self.f.service
        self.capture = QueuedNativeResumeCapture(self.service, self.f.attempt.id, self.f.claim,
            'requested', 'thread', 'admin').bind_assignment(self.f.assignment, worker_id='worker',
                fence=7, validator=lambda:self.f.assignment)
        self.runtime = self.f.runtime
        self.runtime.native_runtime_version = '0.160.0'

    def saved(self):
        return self.service.store.get(self.f.attempt.id)

    def prepare(self):
        return self.capture.prepare(generation='generation', rpc_request_id='resume-original',
            native_session_id='thread', runtime_version='0.160.0')

    def negative(self):
        return {'id':'resume-original', 'error':{'code':-32600,
            'message':'session thread is archived. Run `codex unarchive thread` to unarchive it first.'}}

    async def test_exact_negative_survives_restart_and_explicit_mfa_consumption(self):
        before = copy.deepcopy(self.saved().queued_turn_snapshot)
        c = self.prepare()
        self.assertTrue(self.capture.observe(c, generation='generation', message=self.negative()))
        self.assertEqual(self.saved().status, 'reconciliation_required')
        restarted = ExecutionPreflightService(ExecutionPreflightStore(self.service.store.store))
        result = restarted.reconcile_prestart_refusal(self.f.attempt.id, actor=actor(admin=True))
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.queued_resume_evidence.outcome, 'archived_refusal')
        self.assertEqual(result.queued_turn_snapshot, before)
        self.assertIsNone(result.queued_start_receipt)
        self.assertEqual(restarted.reconcile_prestart_refusal(result.id, actor=actor(admin=True)), result)
        self.assertTrue(restarted.recorded_prestart_refusal(result))
        self.runtime._send.assert_not_called()

    async def test_unknown_absent_error_generation_id_version_remain_unknown(self):
        c=self.prepare()
        variants=[{'id':'resume-original'}, {'id':'resume-original','error':{'code':-32600,'message':'other'}},
            {**self.negative(),'result':{'thread':{'id':'thread'}}},
            {'id':'wrong','error':self.negative()['error']},
            {'id':'resume-original','error':{**self.negative()['error'],'message':self.negative()['error']['message'].replace('thread','foreign')}}]
        for message in variants:
            self.assertFalse(self.capture.observe(c, generation='generation', message=message))
        self.assertFalse(self.capture.observe(c,generation='old',message=self.negative()))
        with self.assertRaises(HTTPException):
            self.service.reconcile_prestart_refusal(self.f.attempt.id,actor=actor(admin=True))
        self.assertEqual(self.saved().status,'reconciliation_required')
        self.assertIsNone(self.saved().queued_resume_evidence)

    async def test_changed_snapshot_refuses_observation(self):
        c=self.prepare()
        self.service.store.update(self.f.attempt.id,lambda x:x.model_copy(update={
            'queued_turn_snapshot':{**x.queued_turn_snapshot,'message':'changed'}}))
        with self.assertRaises(ValueError):
            self.capture.observe(c,generation='generation',message=self.negative())
        self.assertIsNone(self.saved().queued_resume_evidence)

    async def test_transport_reader_observes_negative_before_error_caller(self):
        self.runtime.generation='generation'
        async def send(message,**kwargs):
            kwargs['start_guard']()
            await self.runtime._dispatch_message({**self.negative(),'id':message['id']},generation='generation')
        self.runtime._send=AsyncMock(side_effect=send)
        with self.assertRaises(RuntimeError):
            await self.runtime.request('thread/resume',{'threadId':'thread'},start_capture=self.capture)
        self.assertEqual(self.saved().queued_resume_evidence.outcome,'archived_refusal')
        self.assertEqual(self.saved().queued_resume_correlation.rpc_method,'thread/resume')
        self.assertFalse(self.runtime._start_captures)

    async def test_positive_resume_allows_one_start_and_blocks_refusal_consumption(self):
        c=self.prepare()
        self.assertTrue(self.capture.observe(c,generation='generation',message={
            'id':'resume-original','result':{'thread':{'id':'thread'}}}))
        self.f.capture.prepare(generation='generation',rpc_request_id='start',native_session_id='thread')
        with self.assertRaises(HTTPException):
            self.service.reconcile_prestart_refusal(self.f.attempt.id,actor=actor(admin=True))
        self.assertIsNone(self.saved().queued_start_receipt)

    async def test_refusal_never_allows_start_before_explicit_recovery(self):
        c=self.prepare();self.capture.observe(c,generation='generation',message=self.negative())
        with self.assertRaises(ValueError):
            self.f.capture.prepare(generation='generation',rpc_request_id='start',native_session_id='thread')
        self.assertIsNone(self.saved().queued_start_correlation)

    async def test_changed_send_fence_and_unsupported_version_never_send(self):
        with self.assertRaises(ValueError):
            self.capture.prepare(generation='generation',rpc_request_id='wrong-version',
                native_session_id='thread',runtime_version='0.161.0')
        self.assertIsNone(self.saved().queued_resume_correlation)
        c=self.prepare()
        original=copy.deepcopy(self.saved().queued_turn_snapshot)
        self.f.assignment=self.f.assignment.model_copy(update={'fence':8})
        with self.assertRaises(NativeStartAdmissionRefused):self.capture.validate_before_send(c)
        refused=self.saved()
        self.assertEqual(refused.status,'queued')
        self.assertEqual(refused.queued_turn_snapshot,original)
        self.assertIsNone(refused.queued_resume_correlation)
        self.assertIsNone(refused.queued_start_receipt)
        self.assertEqual(refused.blockers[0].code,'native_preinput_admission_refused')
        self.runtime._send.assert_not_called()

    async def test_old_reader_refuses_new_capture_and_unknown_version(self):
        old_fields={name:(field.annotation,field) for name,field in ExecutionPreflightAttempt.model_fields.items()
            if not name.startswith('queued_resume_')}
        old=create_model('BeforeResumeCapture',__config__={'extra':'forbid'},**old_fields)
        old.model_validate(self.saved().model_dump(mode='json'))
        self.prepare();payload=self.saved().model_dump(mode='json')
        with self.assertRaises(ValidationError):old.model_validate(payload)
        payload['queued_resume_correlation']['runtime_version']='future'
        with self.assertRaises(ValidationError):ExecutionPreflightAttempt.model_validate(payload)

    async def test_lost_response_before_explicit_consumption_and_cas_race(self):
        c=self.prepare()
        self.capture.observe(c,generation='generation',message=self.negative())
        # Response/projection loss changes no provider operation: a new service
        # consumes the committed observation, never a reconstructed error string.
        restarted=ExecutionPreflightService(ExecutionPreflightStore(self.service.store.store))
        with patch.object(restarted.store,'update',side_effect=RuntimeError('store unavailable')):
            with self.assertRaisesRegex(RuntimeError,'store unavailable'):
                restarted.reconcile_prestart_refusal(self.f.attempt.id,actor=actor(admin=True))
        self.assertEqual(self.saved().status,'reconciliation_required')
        restarted.store.update(self.f.attempt.id,lambda x:x.model_copy(update={
            'queued_admission_claim_id':'changed-claim'}))
        with self.assertRaises(HTTPException):
            restarted.reconcile_prestart_refusal(self.f.attempt.id,actor=actor(admin=True))
        self.runtime._send.assert_not_called()

    async def test_member_cannot_consume_and_prior_unknown_does_not_become_proof(self):
        c=self.prepare();self.capture.observe(c,generation='generation',message=self.negative())
        with self.assertRaises(HTTPException):
            self.service.reconcile_prestart_refusal(self.f.attempt.id,actor=actor(admin=False))
        self.assertEqual(self.saved().status,'reconciliation_required')
        self.assertIsNone(self.saved().queued_start_receipt)

    async def test_explicit_new_claim_preserves_consumed_resume_history(self):
        c=self.prepare();self.capture.observe(c,generation='generation',message=self.negative())
        self.service.reconcile_prestart_refusal(self.f.attempt.id,actor=actor(admin=True))
        # Simulate only the already-tested queue retry/admission state transition;
        # no old provider request is retried by this capture test.
        self.service.store.update(self.f.attempt.id,lambda x:x.model_copy(update={
            'status':'reconciliation_required','queued_admission_claim_id':'new-claim'}))
        capture=QueuedNativeResumeCapture(self.service,self.f.attempt.id,'new-claim',
            'requested','thread','admin').bind_assignment(self.f.assignment,worker_id='worker',
                fence=7,validator=lambda:self.f.assignment)
        capture.prepare(generation='new-generation',rpc_request_id='new-rpc',
            native_session_id='thread',runtime_version='0.160.0')
        saved=self.saved()
        self.assertEqual(saved.queued_resume_history[0].correlation,c)
        self.assertEqual(saved.queued_resume_history[0].evidence.outcome,'archived_refusal')
        self.assertEqual(saved.queued_resume_correlation.admission_claim_id,'new-claim')
        self.assertIsNone(saved.queued_resume_evidence)
        self.runtime._send.assert_not_called()

    async def test_positive_resume_only_local_controller_witness_releases_preinput(self):
        c=self.prepare();self.capture.observe(c,generation='generation',message={
            'id':'resume-original','result':{'thread':{'id':'thread'}}})
        unknown=self.service.queued_disposition(self.f.attempt.id,status='blocked',
            claim_id=self.f.claim,detail={'code':'project_delegation_preinput_refused'})
        self.assertEqual(unknown.status,'reconciliation_required')
        self.assertEqual(unknown.queued_resume_correlation,c)
        refused=self.service.queued_disposition(self.f.attempt.id,status='blocked',
            claim_id=self.f.claim,preinput_refusal=True)
        self.assertEqual(refused.status,'blocked')
        self.assertIsNone(refused.queued_resume_correlation)
        self.assertEqual(refused.queued_resume_history[0].correlation,c)
        self.assertEqual(refused.queued_resume_history[0].evidence.outcome,'resumed')
        self.assertEqual(refused.queued_turn_snapshot,unknown.queued_turn_snapshot)
        self.runtime._send.assert_not_called()

    async def test_local_witness_cannot_release_uncertain_resume_or_prepared_turn(self):
        c=self.prepare()
        unknown=self.service.queued_disposition(self.f.attempt.id,status='queued',
            claim_id=self.f.claim,preinput_refusal=True)
        self.assertEqual(unknown.status,'reconciliation_required')
        self.capture.observe(c,generation='generation',message={
            'id':'resume-original','result':{'thread':{'id':'thread'}}})
        start=self.f.capture.prepare(generation='generation',rpc_request_id='start',native_session_id='thread')
        unknown=self.service.queued_disposition(self.f.attempt.id,status='blocked',
            claim_id=self.f.claim,preinput_refusal=True)
        self.assertEqual(unknown.status,'reconciliation_required')
        self.assertEqual(unknown.queued_start_correlation,start)
        self.assertFalse(unknown.queued_resume_history)

    async def test_shared_controlled_session_captures_real_positive_reader_observation(self):
        from tests import test_turn_execution as turn_fixtures
        from types import SimpleNamespace
        session=turn_fixtures._Session()
        session.assignment=self.f.assignment
        session.status=lambda:SimpleNamespace(worker_id='worker',fence=7)
        response=await session.request_with_start_capture('thread/resume',{'threadId':'thread'},self.capture)
        self.assertEqual(response['thread']['id'],'thread')
        saved=self.saved()
        self.assertEqual(saved.queued_resume_evidence.outcome,'resumed')
        self.assertEqual(saved.queued_resume_correlation.runtime_generation,session.capture_runtime.generation)
        self.assertEqual(saved.queued_resume_evidence.rpc_request_id,saved.queued_resume_correlation.rpc_request_id)
        self.assertEqual(session.requests,[('thread/resume',{'threadId':'thread'})])
        self.assertEqual(saved.status,'reconciliation_required')
        self.assertIsNone(saved.queued_start_receipt)

    async def test_adapter_without_capture_transport_still_refuses_before_native_request(self):
        from types import SimpleNamespace
        from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
        transport=SimpleNamespace(request=AsyncMock())
        request=SimpleNamespace(workspace_cwd=None,sandbox=None,sandbox_policy=None,
            approval_policy=None,approval_reviewer=None,model=None,developer_instructions=None)
        with self.assertRaisesRegex(RuntimeError,'Assignment-bound resume capture transport unavailable'):
            await CodexAgentRuntimeAdapter(transport).resume_session('thread',request,resume_capture=self.capture)
        transport.request.assert_not_awaited()

    def refusing_capture(self):
        from dataclasses import replace
        from codex_web.services.queued_operator_admission import OperatorAdmissionPrestartRefusal
        def refuse():
            raise OperatorAdmissionPrestartRefusal(status_code=409,detail={
                'code':'operator_admission_refused','message':'synthetic current authority changed'})
        return replace(self.capture,admission_validator=refuse)

    async def test_resume_writer_refusal_commits_exact_no_write_witness(self):
        from codex_web.services.queued_native_start import NativeStartAdmissionRefused
        from codex_web.services.queued_operator_admission import OperatorAdmissionPrestartRefusal
        capture=self.refusing_capture()
        c=capture.prepare(generation='generation',rpc_request_id='resume-original',
            native_session_id='thread',runtime_version='0.160.0')
        before=copy.deepcopy(self.saved().queued_turn_snapshot)
        with self.assertRaises(NativeStartAdmissionRefused) as caught:
            capture.validate_before_send(c)
        self.assertEqual(caught.exception.correlation,c)
        self.assertIsInstance(caught.exception.__cause__,OperatorAdmissionPrestartRefusal)
        confirmed=capture.confirm_prewrite_refusal(c)
        self.assertEqual(confirmed.status,'queued')
        self.assertEqual(confirmed.queued_turn_snapshot,before)
        self.assertIsNone(confirmed.queued_resume_correlation)
        self.assertIsNone(confirmed.queued_resume_evidence)
        self.assertIsNone(confirmed.queued_start_receipt)
        self.assertEqual(confirmed.blocker_history[-1].blockers[0].model_dump(mode='json')['correlation'],
            c.model_dump(mode='json'))
        with self.assertRaises(ValueError):
            capture.confirm_prewrite_refusal(c.model_copy(update={'rpc_request_id':'foreign'}))
        self.runtime._send.assert_not_called()

    async def test_failed_prewrite_bookkeeping_retains_unknown_and_emits_no_witness(self):
        capture=self.refusing_capture()
        c=capture.prepare(generation='generation',rpc_request_id='resume-original',
            native_session_id='thread',runtime_version='0.160.0')
        update=self.service.store.update
        def failing(attempt_id,callback):
            if callback.__name__=='refuse':
                raise RuntimeError('synthetic no-write bookkeeping unavailable')
            return update(attempt_id,callback)
        with patch.object(self.service.store,'update',side_effect=failing):
            with self.assertRaisesRegex(RuntimeError,'synthetic no-write bookkeeping unavailable'):
                capture.validate_before_send(c)
        self.assertEqual(self.saved().status,'reconciliation_required')
        self.assertEqual(self.saved().queued_resume_correlation,c)
        with self.assertRaises(ValueError):capture.confirm_prewrite_refusal(c)
        self.assertIsNone(self.saved().queued_start_receipt)
        self.runtime._send.assert_not_called()

    async def test_possible_turn_and_positive_start_win_over_earlier_local_no_write_witness(self):
        from codex_web.services.queued_native_start import NativeStartAdmissionRefused
        from codex_web.models import QueuedNativeStartCorrelation
        capture=self.refusing_capture()
        c=capture.prepare(generation='generation',rpc_request_id='resume-original',
            native_session_id='thread',runtime_version='0.160.0')
        with self.assertRaises(NativeStartAdmissionRefused):capture.validate_before_send(c)
        # A conflicting canonical possible-input reservation must win even over
        # an earlier actual local witness. Its subsequent reader result is owned.
        start=QueuedNativeStartCorrelation.model_validate({
            **c.model_dump(exclude={'rpc_method','runtime_version'}),'rpc_request_id':'possible-turn-rpc'})
        self.service.store.update(self.f.attempt.id,lambda x:x.model_copy(update={
            'queued_start_correlation':start,'status':'reconciliation_required'}))
        with self.assertRaises(ValueError):capture.confirm_prewrite_refusal(c)
        unknown=self.service.queued_disposition(self.f.attempt.id,status='blocked',
            claim_id=self.f.claim,preinput_refusal=True)
        self.assertEqual(unknown.status,'reconciliation_required')
        self.assertEqual(unknown.queued_start_correlation,start)
        self.assertTrue(self.f.capture.observe(start,generation='generation',message={
            'id':'possible-turn-rpc','result':{'turn':{'id':'actual-positive','threadId':'thread'}}}))
        result=self.service.queued_disposition(self.f.attempt.id,status='blocked',
            claim_id=self.f.claim,preinput_refusal=True)
        self.assertEqual(result.status,'started')
        self.assertEqual(result.queued_start_receipt.provider_native_turn_id,'actual-positive')
        self.runtime._send.assert_not_called()


class QueuedNativeResumeTests(ResumeCases,unittest.IsolatedAsyncioTestCase):
    pass
