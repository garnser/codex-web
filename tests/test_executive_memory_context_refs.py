"""Executive output cites exact supplied memory without replacing business evidence."""
import json
import unittest
from types import SimpleNamespace

from codex_web.definitions import DefinitionReference
from codex_web.executive_roles import (
    ExecutiveActivation, ExecutiveActivationCreate, ExecutiveCanonicalContext,
    ExecutiveObjectType, ExecutiveReasoningBudget, ExecutiveRoleDefinition,
)
from codex_web.services.executive_management import (
    ExecutiveConsultationError, ExecutiveManagementService,
)

MEMORY = "[memory:knowledge-policy-a@v3]"
BUSINESS = "[business-entity:customer-a]"


class ExecutiveMemoryContextRefsTests(unittest.IsolatedAsyncioTestCase):
    async def consultation(self, refs, *, business=False, memory_visible=True,
                           business_visible=True):
        observed = [ExecutiveObjectType.BUSINESS_ENTITY] if business_visible else []
        if memory_visible:
            observed.append(ExecutiveObjectType.MEMORY)
        role = ExecutiveRoleDefinition(id="cpo", name="CPO", title="Product",
            description="Review product context", responsibilities=("product",),
            observable_information=tuple(observed), instructions="Use supplied context.")
        activation = ExecutiveActivation(organization_id="local", workspace_id="default",
            initiated_by="owner", subject="Product", request="Review supplied context",
            trigger_kind="request", selections=(), budget=ExecutiveReasoningBudget(),
            role_catalog=DefinitionReference(definition_id="executive.roles", kind="executive.roles",
                revision=1, record_id="roles-v1", checksum="a"*64, definition_schema_version="1.0"),
            context=ExecutiveCanonicalContext(memory=({"citation": MEMORY},),
                business_entities=({"citation": BUSINESS},) if business else ()))
        requests = []
        async def invoke(request, *, actor):
            requests.append(request)
            return SimpleNamespace(text=json.dumps({"summary": "Reviewed", "recommendation": "Wait",
                "context_refs": refs, "proposals": []}), invocation=SimpleNamespace(id="invocation-a"))
        service = object.__new__(ExecutiveManagementService)
        service.clock = lambda: 100.0
        service.model_gateway = SimpleNamespace(invoke=invoke)
        service.native_invocation_authorizer = None
        result = await service._consult_role(activation, role, actor=None,
            max_input_tokens=12000, max_output_tokens=2000, max_cost_usd=1)
        self.assertEqual(len(requests), 1)
        return result, requests[0]

    async def test_exact_supplied_memory_citation_is_accepted(self):
        result, request = await self.consultation([MEMORY])
        self.assertEqual(result.output.context_refs, (MEMORY,))
        self.assertIn(MEMORY, request.messages[0].content)

    async def test_unknown_id_or_version_is_rejected(self):
        for ref in ("[memory:invented@v3]", "[memory:knowledge-policy-a@v4]"):
            with self.subTest(ref=ref), self.assertRaisesRegex(
                    ExecutiveConsultationError, "unavailable canonical context"):
                await self.consultation([ref])

    async def test_role_hidden_memory_is_unavailable(self):
        with self.assertRaisesRegex(ExecutiveConsultationError, "unavailable canonical context"):
            await self.consultation([MEMORY], memory_visible=False)

    async def test_memory_alone_does_not_satisfy_required_business_citation(self):
        with self.assertRaisesRegex(ExecutiveConsultationError, "must cite at least one supplied governed"):
            await self.consultation([MEMORY], business=True)

    async def test_exact_business_and_memory_citations_are_accepted(self):
        result, _ = await self.consultation([BUSINESS, MEMORY], business=True)
        self.assertEqual(result.output.context_refs, (BUSINESS, MEMORY))

    async def test_business_citation_alone_remains_sufficient(self):
        result, _ = await self.consultation([BUSINESS], business=True)
        self.assertEqual(result.output.context_refs, (BUSINESS,))

    async def test_filtered_business_does_not_create_a_requirement(self):
        result, request = await self.consultation([MEMORY], business=True, business_visible=False)
        self.assertEqual(result.output.context_refs, (MEMORY,))
        self.assertNotIn(BUSINESS, request.messages[0].content)

    async def public_consultation(self, *, memory_only=False):
        from tests import test_executive_business_context as business
        from tests import test_executive_management as management
        fixture = business.ExecutiveBusinessContextTests()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        fixture.service.organizational_memory = management._Memory()
        entity = fixture.entity()
        original = fixture.gateway.invoke
        async def cite(request, *, actor):
            response = await original(request, actor=actor)
            payload = json.loads(response.text)
            payload["context_refs"] = ([MEMORY] if memory_only
                                       else [*payload["context_refs"], MEMORY])
            response.text = json.dumps(payload)
            return response
        fixture.gateway.invoke = cite
        activation = fixture.service.create(ExecutiveActivationCreate(
            subject="Revenue context", request="Review customer context and policy",
            requested_role_ids=("cro",), business_entity_ids=(entity.id,),
            max_roles=1, budget=ExecutiveReasoningBudget(max_model_calls=1)), actor=fixture.actor)
        self.assertTrue(activation.context.business_entities)
        self.assertEqual(activation.context.memory[0]["citation"], MEMORY)
        return await fixture.service.consult(activation.id, actor=fixture.actor)

    async def test_public_consultation_preserves_business_and_memory_refs(self):
        result = await self.public_consultation()
        refs = result.consultations[0].output.context_refs
        self.assertEqual(result.status.value, "completed")
        self.assertIn(MEMORY, refs)
        self.assertTrue(any(ref.startswith("[business-entity:") for ref in refs))

    async def test_public_consultation_requires_business_even_with_valid_memory(self):
        with self.assertRaisesRegex(ExecutiveConsultationError, "must cite at least one supplied governed"):
            await self.public_consultation(memory_only=True)
