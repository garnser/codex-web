"""Independent canonical retrievals overlap without abandoning their owners."""
import asyncio
import contextvars
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi import HTTPException
from codex_web.executive_roles import ExecutiveActivationCreate
from codex_web.services.executive_management import ExecutiveContextError
from codex_web.services.work_graph import WorkGraphService
from codex_web.storage.work_graph import WorkGraphStore
from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.state_store import StorageOperationBudget, storage_operation_budget, check_operation_budget
from tests import test_executive_management as fixtures
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.organizational_memory import OrganizationalMemoryService
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.organizational_memory import OrganizationalMemoryStore


class ExecutiveContextOverlapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixtures.ExecutiveManagementTests()
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)
        self.service, self.actor = self.f.service, self.f.actor
        self.backend = self.service.store.store
        self.graph = WorkGraphService(WorkGraphStore(self.backend), lambda: {})
        self.service.work_graph = self.graph
        self.memory = fixtures._Memory()
        self.memory.store = SimpleNamespace(store=self.backend)
        self.service.organizational_memory = self.memory
        self.payload = ExecutiveActivationCreate(subject='Current product priorities',
            request='Review the current governed project context.',
            project_id='project-a', requested_role_ids=('cpo',))

    async def test_real_prepare_retains_both_results_and_propagates_context(self):
        rendezvous = threading.Barrier(2)
        marker = contextvars.ContextVar('private-context-marker')
        observations = []
        original_graph = self.graph.snapshot
        original_memory = self.memory.search
        budget = StorageOperationBudget(3)
        def graph(project_id, *, scope):
            rendezvous.wait(1)
            observations.append((marker.get(), storage_operation_budget.get(), scope))
            return original_graph(project_id, scope=scope)
        def memory(query, *, actor):
            rendezvous.wait(1)
            return original_memory(query, actor=actor)
        self.graph.snapshot, self.memory.search = graph, memory
        token = marker.set('same-tenant-request')
        budget_token = storage_operation_budget.set(budget)
        try:
            prepared = self.service.prepare(self.payload, actor=self.actor)
        finally:
            marker.reset(token)
            storage_operation_budget.reset(budget_token)
        self.assertEqual(observations, [('same-tenant-request', budget, self.actor.tenant)])
        self.assertEqual(prepared.context.work_graphs[0]['progress']['total'], 0)
        self.assertEqual(prepared.context.memory_retrieval_ids, ('memory-retrieval-executive-a',))
        self.assertEqual(prepared.context.memory[0]['citation'], '[memory:knowledge-policy-a@v3]')
        query, actor = self.memory.queries[0]
        self.assertEqual(query.project_ids, ('project-a',))
        self.assertEqual(actor, self.actor)
        self.assertEqual(self.service.list(actor=self.actor), ())

    async def test_graph_failure_wins_after_real_memory_retrieval_is_joined(self):
        rendezvous = threading.Barrier(2)
        memory_finished = threading.Event()
        child_threads = []
        def graph():
            child_threads.append(threading.current_thread())
            rendezvous.wait(1)
            raise ValueError('canonical graph refusal')
        def memory():
            rendezvous.wait(1)
            memory_finished.set()
            raise LookupError('memory refusal')
        with self.assertRaisesRegex(ValueError, 'canonical graph refusal'):
            self.service._read_context_pair(graph, memory)
        self.assertTrue(memory_finished.is_set())
        self.assertFalse(child_threads[0].is_alive())

    async def test_memory_failure_waits_for_graph_and_releases_capacity(self):
        memory_failed = threading.Event()
        graph_finished = threading.Event()
        def graph():
            self.assertTrue(memory_failed.wait(1))
            graph_finished.set()
            return 'graph'
        def memory():
            memory_failed.set()
            raise LookupError('memory refusal')
        with self.assertRaisesRegex(LookupError, 'memory refusal'):
            self.service._read_context_pair(graph, memory)
        self.assertTrue(graph_finished.is_set())
        self.assertEqual(self.service._read_context_pair(lambda: 'g', lambda: 'm'), ('g', 'm'))

    async def test_saturation_and_unknown_storage_keep_sequential_order(self):
        order = []
        def graph():
            order.append('graph')
            return 'g'
        def memory():
            order.append('memory')
            return 'm'
        for _ in range(2):
            self.service._context_overlap_slots.acquire()
        try:
            self.assertEqual(self.service._read_context_pair(graph, memory), ('g', 'm'))
        finally:
            for _ in range(2):
                self.service._context_overlap_slots.release()
        self.assertEqual(order, ['graph', 'memory'])
        order.clear()
        self.memory.store = SimpleNamespace(store=object())
        self.assertEqual(self.service._read_context_pair(graph, memory), ('g', 'm'))
        self.assertEqual(order, ['graph', 'memory'])

    async def test_service_admits_only_two_extra_workers_without_a_queue(self):
        parent = contextvars.ContextVar('overlap-parent-thread')
        both_children = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        children, sequential = [], []
        def graph():
            if threading.get_ident() == parent.get():
                sequential.append(threading.get_ident())
                return 'g'
            with lock:
                children.append(threading.current_thread())
                if len(children) == 2:
                    both_children.set()
            self.assertTrue(release.wait(2))
            return 'g'
        def prepare():
            token = parent.set(threading.get_ident())
            try:
                return self.service._read_context_pair(graph, lambda: 'm')
            finally:
                parent.reset(token)
        with ThreadPoolExecutor(max_workers=2) as parents:
            futures = [parents.submit(prepare) for _ in range(2)]
            try:
                self.assertTrue(both_children.wait(2))
                self.assertEqual(prepare(), ('g', 'm'))
                self.assertEqual(len(sequential), 1)
            finally:
                release.set()
            self.assertEqual([f.result(timeout=2) for f in futures], [('g', 'm')] * 2)
        self.assertEqual(len(children), 2)
        self.assertTrue(all(not child.is_alive() for child in children))

    async def test_graph_refusal_keeps_audit_of_actual_memory_attempt_without_activation(self):
        registry = DefinitionRegistryService(DefinitionRegistryStore(self.backend))
        authority = install_authority_roles(registry)
        memory_store = OrganizationalMemoryStore(self.backend)
        self.service.organizational_memory = OrganizationalMemoryService(
            memory_store, DataGovernanceService(DataGovernanceStore(self.backend)), authority)
        self.assertEqual(memory_store.load().retrieval_runs, [])
        def refuse(project_id, *, scope):
            raise ValueError('canonical graph refusal')
        self.graph.snapshot = refuse
        with self.assertRaisesRegex(ExecutiveContextError, 'canonical WorkGraph is unavailable'):
            self.service.prepare(self.payload, actor=self.actor)
        runs = memory_store.load().retrieval_runs
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0].organization_id, runs[0].workspace_id, runs[0].actor_id),
                         (self.actor.organization_id, self.actor.workspace_id, self.actor.identity_id))
        self.assertEqual(runs[0].selected_knowledge_ids, ())
        self.assertEqual(runs[0].candidate_count, 0)
        self.assertEqual(self.service.list(actor=self.actor), ())
        self.assertEqual(self.f.gateway.requests, [])

    async def test_owning_postgres_transaction_is_never_forked(self):
        backend = object.__new__(PostgresStateStore)
        backend._callback_reads = threading.local()
        self.service.store = SimpleNamespace(store=backend)
        self.graph.store = SimpleNamespace(store=backend)
        self.memory.store = SimpleNamespace(store=backend)
        caller = threading.get_ident()
        observations = []
        def graph():
            observations.append(threading.get_ident())
            return 'g'
        self.assertTrue(backend.can_fork_context_reads())
        result = backend._run_updater(object(), lambda _: self.service._read_context_pair(
            graph, lambda: 'm'), None)
        self.assertEqual(result, ('g', 'm'))
        self.assertEqual(observations, [caller])
        self.assertTrue(backend.can_fork_context_reads())

    async def test_default_executor_with_one_parent_does_not_deadlock(self):
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        rendezvous = threading.Barrier(2)
        def graph():
            rendezvous.wait(1)
            return 'g'
        def memory():
            rendezvous.wait(1)
            return 'm'
        result = await asyncio.wait_for(asyncio.to_thread(
            self.service._read_context_pair, graph, memory), timeout=2)
        self.assertEqual(result, ('g', 'm'))

    async def test_retired_budget_is_seen_by_child_before_storage_work(self):
        budget = StorageOperationBudget(3)
        budget.cancelled.set()
        token = storage_operation_budget.set(budget)
        try:
            with self.assertRaises(TimeoutError):
                self.service._read_context_pair(check_operation_budget, lambda: None)
        finally:
            storage_operation_budget.reset(token)

    async def test_request_timeout_never_persists_after_both_branches_finish(self):
        graph_started = threading.Event()
        graph_finished = threading.Event()
        memory_finished = threading.Event()
        original_graph, original_memory = self.graph.snapshot, self.memory.search
        def graph(project_id, *, scope):
            graph_started.set()
            time.sleep(.06)
            result = original_graph(project_id, scope=scope)
            graph_finished.set()
            return result
        def memory(query, *, actor):
            self.assertTrue(graph_started.wait(1))
            time.sleep(.06)
            result = original_memory(query, actor=actor)
            memory_finished.set()
            return result
        self.graph.snapshot, self.memory.search = graph, memory
        endpoint, request = self.f._create_endpoint(timeout=.01)
        with self.assertRaises(HTTPException) as failure:
            await endpoint(self.payload, request)
        self.assertEqual(failure.exception.detail['code'], 'executive_context_timeout')
        await asyncio.to_thread(graph_finished.wait, 2)
        await asyncio.to_thread(memory_finished.wait, 2)
        self.assertTrue(graph_finished.is_set() and memory_finished.is_set())
        self.assertEqual(self.service.list(actor=self.actor), ())
        self.assertEqual(self.f.gateway.requests, [])
