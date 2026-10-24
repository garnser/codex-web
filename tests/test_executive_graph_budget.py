"""Canonical project graphs must not become unbounded Executive prompts."""
import json
import hashlib
import unittest
from types import SimpleNamespace

from codex_web.executive_roles import ExecutiveActivationCreate, ExecutiveReasoningBudget
from codex_web.models import WorkItemState
from codex_web.services.model_gateway import ModelGatewayService, ModelRoutingError
from codex_web.services.executive_management import ExecutiveContextError
from codex_web.services.work_graph import WorkGraphService, WorkGraphConflictError, WorkGraphScopeError
from codex_web.storage.work_graph import WorkGraphStore
from codex_web.work_graph import WorkGraphState, WorkGraphEdge, WorkGraphRelation
from tests import test_executive_management as fixtures


class BudgetGateway:
    def __init__(self):
        self.requests = []

    async def invoke(self, request, *, actor):
        estimate = ModelGatewayService._estimate_tokens(request)
        if estimate > request.max_input_tokens:
            raise ModelRoutingError(f"estimated input token budget exceeded: {estimate} > {request.max_input_tokens}")
        self.requests.append(request)
        return SimpleNamespace(text=json.dumps(dict(summary='Project overview reviewed.',
            recommendation='Resolve the canonical blockers first.', risks=[], assumptions=[],
            disagreement=[], context_refs=[], proposals=[])),
            invocation=SimpleNamespace(id='private-budget-invocation'))


class ExecutiveGraphBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.ExecutiveManagementTests()
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.service, self.actor = self.fixture.service, self.fixture.actor
        self.gateway = BudgetGateway()
        self.service.model_gateway = self.gateway
        self.items = {f'project:item:{i:04d}': WorkItemState(
            ref=f'project:item:{i:04d}', project_id='project-a',
            title=f'Canonical project work item {i}', current_stage='implementation_active',
            last_meaningful_update_at=100, created_at=100, updated_at=100)
            for i in range(1899)}
        self.graph = WorkGraphService(WorkGraphStore(self.service.store.store), lambda: self.items)
        self.service.work_graph = self.graph

    async def test_large_project_overview_consults_under_original_12000_token_budget(self):
        activation = self.service.create(ExecutiveActivationCreate(
            subject='Bounded product strategy', request='Assess project priorities and uncertainty.',
            project_id='project-a', requested_role_ids=('cpo',),
            budget=ExecutiveReasoningBudget(max_input_tokens=12000, max_output_tokens=2048,
                max_model_calls=1)), actor=self.actor)
        result = await self.service.consult(activation.id, actor=self.actor)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(self.gateway.requests), 1)
        request = self.gateway.requests[0]
        self.assertEqual(request.max_input_tokens, 12000)
        self.assertLessEqual(ModelGatewayService._estimate_tokens(request), 12000)
        self.assertEqual(activation.context.work_graphs[0]['progress']['total'], 1899)
        self.assertEqual(len(self.graph.snapshot('project-a', scope=self.actor.tenant).nodes), 1899)
        print(json.dumps({'canonical_nodes':1899, 'prompt_estimated_tokens':
            ModelGatewayService._estimate_tokens(request), 'unchanged_input_budget':12000}), flush=True)

    def snapshot(self):
        return self.graph.snapshot('project-a', scope=self.actor.tenant)

    def edge(self, source, target):
        edge = WorkGraphEdge(project_id='project-a', organization_id='local', workspace_id='default',
            source_ref=source, target_ref=target, relation=WorkGraphRelation.BLOCKS, created_by='owner')
        self.graph.store.update(lambda _: WorkGraphState(edges=[edge]))

    def test_project_metrics_source_digest_critical_blockers_and_omissions_are_exact(self):
        self.edge('project:item:1898', 'project:item:1897')
        self.items['project:item:1898'].terminal_outcome = 'failed'
        self.items['foreign'] = self.items['project:item:0001'].model_copy(update={
            'ref':'foreign', 'organization_id':'foreign'})
        snapshot = self.snapshot()
        before = snapshot.model_dump(mode='json')
        result = self.graph.executive_context(snapshot, required_refs=(), max_nodes=4, max_characters=10000)
        expected_digest = hashlib.sha256(json.dumps(before, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(result['source_snapshot_sha256'], expected_digest)
        self.assertEqual(result['progress'], before['progress'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['omitted']['nodes'], 1895)
        included = {row['ref']: row for row in result['nodes']}
        self.assertIn('project:item:1898', included)
        self.assertIn('project:item:1897', included)
        self.assertNotIn('foreign', included)
        self.assertEqual(result['failure_impacts'], before['failure_impacts'])
        self.assertEqual(result['critical_path']['node_count'], before['critical_path']['node_count'])
        self.assertEqual(before, self.snapshot().model_dump(mode='json'))

    def test_explicit_work_and_incoming_dependency_are_retained_or_refused_never_narrowed(self):
        self.edge('project:item:1898', 'project:item:1897')
        snapshot = self.snapshot()
        result = self.graph.executive_context(snapshot, required_refs=('project:item:1897',),
            max_nodes=2, max_characters=10000)
        self.assertEqual({n['ref'] for n in result['nodes']}, {'project:item:1897','project:item:1898'})
        self.assertEqual(result['edges'], snapshot.model_dump(mode='json')['edges'])
        with self.assertRaises(WorkGraphConflictError):
            self.graph.executive_context(snapshot, required_refs=('project:item:1897',),
                max_nodes=1, max_characters=10000)
        with self.assertRaises(WorkGraphScopeError):
            self.graph.executive_context(snapshot, required_refs=('foreign:missing',),
                max_nodes=10, max_characters=10000)

    def test_oversized_required_text_refuses_while_optional_omission_is_explicit(self):
        self.items['project:item:0000'].title = 'required canonical source ' * 1000
        snapshot = self.snapshot()
        with self.assertRaises(WorkGraphConflictError):
            self.graph.executive_context(snapshot, required_refs=('project:item:0000',),
                max_nodes=4, max_characters=2000)
        result = self.graph.executive_context(snapshot, required_refs=(), max_nodes=4, max_characters=2000)
        self.assertLessEqual(len(json.dumps(result, sort_keys=True, separators=(',', ':'))), 2000)
        self.assertFalse(result['complete'])
        self.assertNotIn('project:item:0000', {node['ref'] for node in result['nodes']})

    def test_required_budget_refusal_persists_no_activation_and_invokes_no_model(self):
        ref = 'project:item:0000'
        self.items[ref].title = 'explicit required text ' * 5000
        self.service.work_items.state_machine._work_item_state = lambda ref: self.items[ref]
        self.service.work_items.state_machine._work_item_state_public = lambda state: state.model_dump(mode='json')
        with self.assertRaises(ExecutiveContextError):
            self.service.create(ExecutiveActivationCreate(subject='Explicit work review', request='Assess this exact work.',
                project_id='project-a', work_item_refs=(ref,), requested_role_ids=('cpo',),
                budget=ExecutiveReasoningBudget(max_input_tokens=12000, max_model_calls=1)), actor=self.actor)
        self.assertEqual(self.service.list(actor=self.actor), ())
        self.assertEqual(self.gateway.requests, [])
