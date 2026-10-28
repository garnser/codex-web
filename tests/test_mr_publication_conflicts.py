"""Expected MR observation refusals stay precise without replay or partial proofs."""
import hashlib
import json
import time
import unittest
from urllib.parse import quote
from unittest.mock import patch

from codex_web.mr_continuation_input import METHOD
from codex_web.services.worker_mr_verifications import WorkerMRVerificationAdapter, MRObservationRequest
from tests import test_worker_mr_continuation_inputs as fixtures


class MRPublicationConflictTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixtures.MRContinuationInputTests()
        await self.f.asyncSetUp()
        self.payload = {**self.f.request, 'method': METHOD, 'request_id': 'd' * 32}
        self.before = self.f.evidence.store.load().model_dump(mode='json')
        self.governance = self.f.evidence.governance.store.load().model_dump(mode='json')

    async def asyncTearDown(self):
        await self.f.asyncTearDown()

    async def assert_refusal_and_reconcile(self, status, body, code):
        self.assertEqual(status, 409, body)
        error = body['error']
        self.assertEqual(set(error), {'code', 'message', 'correlation_id', 'mr_observation'})
        self.assertEqual(error['code'], code)
        self.assertEqual(error['message'], 'Original MR observation refused; reconcile exact source without replay')
        self.assertTrue(error['correlation_id'])
        detail = error['mr_observation']
        self.assertEqual(set(detail), {'assignment_id', 'execution_id', 'fence', 'method',
            'source', 'request_sha256', 'retryable', 'reconcile_without_replay'})
        assignment = self.f.workers.store.assignment(self.f.assignment.id)
        expected = WorkerMRVerificationAdapter.source(None, assignment, MRObservationRequest.model_validate(self.payload))
        self.assertEqual(detail['source'], expected)
        self.assertEqual(detail['assignment_id'], assignment.id)
        self.assertEqual(detail['execution_id'], assignment.execution_id)
        self.assertEqual(detail['fence'], assignment.fence)
        self.assertEqual(detail['method'], METHOD)
        expected_digest = hashlib.sha256(json.dumps(self.payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(detail['request_sha256'], expected_digest)
        self.assertFalse(detail['retryable'])
        self.assertTrue(detail['reconcile_without_replay'])
        event = next(e for e in reversed(self.f.service.audit.load().events)
            if e.operation_id == 'verification.create' and e.response_status == 409)
        retained = json.loads(event.denial_reason)
        self.assertEqual(retained['code'], code)
        self.assertEqual(set(retained), {'code', 'mr_observation'})
        self.assertEqual(retained['mr_observation'], {key: detail[key] for key in
            ('method', 'source', 'request_sha256', 'retryable', 'reconcile_without_replay')})
        self.assertLessEqual(len(event.denial_reason), 500)
        self.assertEqual(event.assignment_id, detail['assignment_id'])
        self.assertEqual(event.execution_id, detail['execution_id'])
        self.assertEqual(event.fence, detail['fence'])
        self.assertNotIn('fixture-token', json.dumps(error))
        self.assertEqual(self.f.evidence.store.load().model_dump(mode='json'), self.before)
        self.assertEqual(self.f.evidence.governance.store.load().model_dump(mode='json'), self.governance)
        calls = len(self.f.server.calls)
        status, _, found = await self.f._request('GET', '/api/verifications?method=' + METHOD + '&source=' + quote(expected, safe=''))
        self.assertEqual(status, 200, found)
        self.assertEqual(found['outcome'], 'unknown')
        self.assertIsNone(found['original'])
        self.assertTrue(found['absence_is_not_permission_to_replay'])
        self.assertEqual(len(self.f.server.calls), calls)

    async def test_expired_after_provider_checkpoints_is_closed_conflict_before_append(self):
        from codex_web.services.broker_merge_requirements import read_merge_requirements
        async def read(*args, **kwargs):
            result = await read_merge_requirements(*args, **kwargs)
            self.f.service._clock = lambda: result['item']['expires_at']
            return result
        with patch('codex_web.services.broker_merge_requirements.read_merge_requirements', side_effect=read):
            status, _, body = await self.f.post(self.payload)
        await self.assert_refusal_and_reconcile(status, body, 'mr_observation_head_changed_or_expired')

    async def test_expired_after_content_attachment_rolls_back_all_canonical_domains(self):
        original = self.f.evidence.attach_artifact_content
        attached = []
        original_clock = self.f.service._clock
        def attach(*args, **kwargs):
            item = original(*args, **kwargs)
            attached.append(item)
            return item
        def now():
            return time.time() + 31 if attached else original_clock()
        with patch.object(self.f.evidence, 'attach_artifact_content', side_effect=attach), patch.object(self.f.service, '_clock', side_effect=now):
            status, _, body = await self.f.post(self.payload)
        self.assertEqual(len(attached), 1)
        self.assertIsNotNone(attached[0].content)
        await self.assert_refusal_and_reconcile(status, body, 'mr_observation_head_changed_or_expired')
        # Managed content can be unreferenced after refusal. Never delete a
        # shared digest or claim the provider/read/blob had no effects.

    async def test_head_future_and_check_head_refusals_are_closed_not_generic_errors(self):
        from codex_web.services.broker_merge_requirements import read_merge_requirements
        original_clock = self.f.service._clock
        for kind, code in (('head', 'mr_observation_head_changed_or_expired'),
                           ('future', 'mr_observation_timestamp_in_future'),
                           ('check', 'mr_observation_check_head_mismatch')):
            async def read(*args, **kwargs):
                result = await read_merge_requirements(*args, **kwargs)
                if kind == 'head':
                    result['item']['changed'] = True
                    result['item']['complete'] = False
                elif kind == 'future':
                    self.f.service._clock = lambda: result['item']['observed_at'] - 1
                else:
                    result['item']['observed_checks'][0]['revision'] = 'c' * 40
                return result
            with self.subTest(kind=kind):
                with patch('codex_web.services.broker_merge_requirements.read_merge_requirements', side_effect=read):
                    status, _, body = await self.f.post(self.payload)
                self.f.service._clock = original_clock
                await self.assert_refusal_and_reconcile(status, body, code)

    async def test_unexpected_value_error_is_not_misclassified_or_retried(self):
        with patch.object(self.f.evidence, 'create_artifact', side_effect=ValueError('controlled unexpected storage failure')):
            status, _, body = await self.f.post(self.payload)
        self.assertEqual(status, 500, body)
        self.assertEqual(body['error']['code'], 'control_plane_broker_denied')
        self.assertNotIn('mr_observation', body['error'])
        self.assertNotIn('source', body['error'])
        self.assertEqual(self.f.evidence.store.load().model_dump(mode='json'), self.before)
        self.assertEqual(self.f.evidence.governance.store.load().model_dump(mode='json'), self.governance)
