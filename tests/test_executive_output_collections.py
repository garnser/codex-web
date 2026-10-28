"""Executive output instructions and strict canonical consumption, no model calls."""
import copy
import json
from types import SimpleNamespace
import unittest

from tests import test_executive_management as fixtures
from codex_web.executive_roles import (
    ExecutiveActivationCreate, ExecutiveActivationStatus, ExecutiveProposalStatus,
    ExecutiveReasoningBudget,
)
from codex_web.services.executive_management import ExecutiveConsultationError


class _OutputGateway:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    async def invoke(self, request, *, actor):
        self.requests.append(request)
        return SimpleNamespace(text=json.dumps(self.payload),
            invocation=SimpleNamespace(id='synthetic-original-invocation'))


class ExecutiveOutputCollectionsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ExecutiveManagementTests.asyncSetUp
    asyncTearDown = fixtures.ExecutiveManagementTests.asyncTearDown

    @staticmethod
    def output():
        return {'summary': 'Controlled qualification remains advisory.',
            'recommendation': 'Keep the existing business holds.', 'risks': [],
            'assumptions': [], 'disagreement': [], 'context_refs': [], 'proposals': []}

    def activation(self, payload):
        gateway = _OutputGateway(payload)
        self.service.model_gateway = gateway
        activation = self.service.create(ExecutiveActivationCreate(
            subject='Controlled product qualification', request='Assess the product qualification evidence.',
            requested_role_ids=('cpo',), budget=ExecutiveReasoningBudget(
                max_input_tokens=8000, max_output_tokens=2000, max_model_calls=1, max_cost_usd=1)),
            actor=self.actor)
        return activation, gateway

    def parse(self, payload):
        role = self.roles.catalog(organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id).role_map['cpo']
        return self.service._parse_role_output(json.dumps(payload), role)

    async def test_public_consult_sends_typed_instructions_and_accepts_empty_arrays(self):
        activation, gateway = self.activation(self.output())
        result = await self.service.consult(activation.id, actor=self.actor)
        self.assertEqual(result.status, ExecutiveActivationStatus.COMPLETED)
        self.assertEqual(len(gateway.requests), 1)
        prompt = gateway.requests[0].system_prompt
        for field in ('summary', 'recommendation', 'risks', 'assumptions', 'disagreement', 'context_refs', 'proposals'):
            self.assertIn(field, prompt)
        self.assertIn('non-empty JSON strings', prompt)
        self.assertIn('arrays of strings', prompt)
        self.assertIn('never null', prompt)
        self.assertIn('use [] only when there are no items', prompt)
        self.assertIn('payload (an object', prompt)
        self.assertFalse(result.synthesis.escalation_required)
        self.assertEqual(result.consultations[0].output.disagreement, ())
        self.assertEqual(result.proposals, ())
        self.assertEqual(self.goals.created, [])
        self.assertEqual(self.authority.requests, [])

    async def test_public_consult_preserves_disagreement_and_original_advisory_payload(self):
        payload = self.output()
        payload['disagreement'] = ['The evidence does not establish readiness.']
        payload['proposals'] = [{'kind': 'goal', 'title': 'Track qualification',
            'rationale': 'Keep a governed outcome.', 'payload': {'title': 'Qualify controlled migration',
                'description': 'Record outstanding evidence while keeping holds.',
                'owner_identity_id': self.actor.identity_id}}]
        expected = copy.deepcopy(payload['proposals'][0]['payload'])
        activation, gateway = self.activation(payload)
        result = await self.service.consult(activation.id, actor=self.actor)
        self.assertEqual(result.consultations[0].output.disagreement, tuple(payload['disagreement']))
        self.assertTrue(result.synthesis.escalation_required)
        self.assertEqual(result.synthesis.disagreement, tuple(payload['disagreement']))
        self.assertEqual(len(result.proposals), 1)
        self.assertEqual(result.proposals[0].payload, expected)
        self.assertEqual(result.proposals[0].status, ExecutiveProposalStatus.PROPOSED)
        self.assertEqual(len(gateway.requests), 1)
        self.assertEqual(self.goals.created, [])
        self.assertEqual(self.authority.requests, [])

    async def test_public_null_disagreement_fails_once_without_normalization_or_retry(self):
        payload = self.output()
        payload['disagreement'] = None
        activation, gateway = self.activation(payload)
        with self.assertRaisesRegex(ExecutiveConsultationError, 'schema validation'):
            await self.service.consult(activation.id, actor=self.actor)
        retained = self.service.get(activation.id, actor=self.actor)
        self.assertEqual(retained.status, ExecutiveActivationStatus.FAILED)
        self.assertEqual(retained.consultations, ())
        self.assertEqual(retained.proposals, ())
        with self.assertRaises(ExecutiveConsultationError):
            await self.service.consult(activation.id, actor=self.actor)
        self.assertEqual(len(gateway.requests), 1)
        self.assertEqual(self.goals.created, [])
        self.assertEqual(self.decisions.items, [])

    async def test_collection_null_scalar_and_invalid_elements_still_refused(self):
        for field in ('risks', 'assumptions', 'disagreement', 'context_refs'):
            for bad in (None, 'not an array', {}, [None], [1]):
                with self.subTest(field=field, bad=bad):
                    payload = self.output()
                    payload[field] = bad
                    with self.assertRaisesRegex(ExecutiveConsultationError, 'schema validation'):
                        self.parse(payload)
        for bad in (None, {}, [None], [{'kind': 'goal', 'title': 'Title', 'rationale': 'Reason', 'payload': []}]):
            payload = self.output()
            payload['proposals'] = bad
            with self.assertRaisesRegex(ExecutiveConsultationError, 'schema validation'):
                self.parse(payload)

    async def test_required_scalars_and_extra_fields_stay_strict(self):
        for field in ('summary', 'recommendation'):
            for bad in (None, '', [], 1):
                payload = self.output()
                payload[field] = bad
                with self.assertRaisesRegex(ExecutiveConsultationError, 'schema validation'):
                    self.parse(payload)
        with self.assertRaisesRegex(ExecutiveConsultationError, 'schema validation'):
            self.parse({**self.output(), 'approved': True})

    async def test_public_disallowed_proposal_and_unknown_citation_keep_authority_boundaries(self):
        payload = self.output()
        payload['proposals'] = [{'kind': 'work', 'title': 'Unapproved work',
            'rationale': 'Must not execute.', 'payload': {}}]
        activation, gateway = self.activation(payload)
        with self.assertRaisesRegex(ExecutiveConsultationError, 'disallowed kinds'):
            await self.service.consult(activation.id, actor=self.actor)
        self.assertEqual(len(gateway.requests), 1)
        payload = self.output()
        payload['context_refs'] = ['[memory:not-supplied@v1]']
        activation, gateway = self.activation(payload)
        with self.assertRaisesRegex(ExecutiveConsultationError, 'unavailable canonical context'):
            await self.service.consult(activation.id, actor=self.actor)
        self.assertEqual(len(gateway.requests), 1)
        self.assertEqual(self.goals.created, [])
        self.assertEqual(self.decisions.items, [])
        self.assertEqual(self.decision_work.calls, [])
        self.assertEqual(self.authority.requests, [])

    async def test_existing_omitted_optional_defaults_remain_compatible(self):
        output = self.parse({'summary': 'Summary', 'recommendation': 'Recommendation'})
        self.assertEqual((output.risks, output.assumptions, output.disagreement,
                          output.context_refs, output.proposals), ((), (), (), (), ()))
