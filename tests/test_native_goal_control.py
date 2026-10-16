import unittest
from pydantic import ValidationError
from codex_web.agent_runtime import NativeGoalControlRequest, NativeGoalControlUnavailable
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter

class Transport:
    def __init__(self, proof):
        self.proof, self.calls = proof, []
    async def request(self, method, params):
        self.calls.append((method, params))
        if method.endswith('capabilities'):
            return self.proof
        if method.endswith('commit'):
            raise TimeoutError('synthetic accepted/unknown response')
        return {'operationId': params['operationId'], 'outcome': 'committed'}

class NativeGoalControlTests(unittest.IsolatedAsyncioTestCase):
    def request(self):
        return NativeGoalControlRequest(operation_id='op', expected_goal_id='g', expected_revision=1, owner_epoch=2, status='active')

    async def test_unpatched_and_partial_contracts_refuse_without_raw_fallback(self):
        for proof in ({'error': {'code': -32601}}, {'contractVersion': '1.0', 'revisionCas': True}):
            transport=Transport(proof)
            with self.assertRaises(NativeGoalControlUnavailable):
                await CodexAgentRuntimeAdapter(transport).control_objective('synthetic', self.request())
            self.assertEqual(len(transport.calls),1)

    async def test_unknown_commit_is_not_retried_and_reconciliation_is_read_only(self):
        proof={'contractVersion':'1.0', **{k:True for k in ('allWriterBarrier','revisionCas','atomicReceipt','accountingFlush','allTransportAdmission','privateStateIsolation')}}
        transport=Transport(proof);adapter=CodexAgentRuntimeAdapter(transport)
        with self.assertRaises(TimeoutError):
            await adapter.control_objective('synthetic', self.request())
        params=transport.calls[-1][1]
        self.assertNotIn('objective',params);self.assertNotIn('tokenBudget',params)
        await adapter.read_objective_control('synthetic','op')
        self.assertEqual(sum(m.endswith('commit') for m,p in transport.calls),1)
        self.assertEqual(transport.calls[-1][0], 'thread/goal/control/receipt')

    async def test_caller_cannot_supply_origin_or_replace_objective(self):
        for key in ('origin','objective','token_budget'):
            with self.assertRaises(ValidationError):
                NativeGoalControlRequest(**self.request().model_dump(), **{key:'user'})
