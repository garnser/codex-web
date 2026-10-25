"""Canonical Memory/Executive/assignment proof, disposable SQLite only."""
import unittest
from types import SimpleNamespace as NS
from fastapi import HTTPException
from pydantic import ValidationError

import tests.test_assignment_reasoning_broker as assignment_fixture
import tests.test_organizational_memory as memory_fixture
from codex_web.services.executive_management import ExecutiveContextError
from codex_web.organizational_memory import KnowledgeCreate, KnowledgeRevise, KnowledgeProvenance, KnowledgeQuery
from codex_web.resources import ResourceCreate, ResourceUpdate
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.organizational_memory import KnowledgeAuthorizationError, KnowledgeValidationError
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.definition_registry import DefinitionRegistryStore


class AssignmentMemoryResourceTests(unittest.IsolatedAsyncioTestCase):
    call = assignment_fixture.AssignmentReasoningTests.call
    request = assignment_fixture.AssignmentReasoningTests.request
    async def asyncSetUp(self):
        await assignment_fixture.AssignmentReasoningTests.asyncSetUp(self)
        self.memory_fixture = memory_fixture.OrganizationalMemoryTests(); self.memory_fixture.setUp()
        self.memory = self.memory_fixture.service
        sqlite = self.memory_fixture.store.store
        self.resources = ResourceCatalogService(ResourceCatalogStore(sqlite))
        registry = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        self.memory.authority = install_authority_roles(registry, self.resources)
        self.actor = self.memory_fixture.actor
        self.assignment.created_by = self.live.created_by = self.actor.identity_id
        self.broker._requester_actor.return_value = self.actor
        self.fixture.service.organizational_memory = self.memory
        self.project = NS(id='project-a', organization_id='local', workspace_id='default')
        self.repo = self.resources.create(ResourceCreate(resource_type='repository', name='MR repository'), actor=self.actor)
        self.resources.bind_project(project=self.project, resource_id=self.repo.id, actor=self.actor)
        self.assignment.resource_ids = self.live.resource_ids = (self.repo.id,)

    async def asyncTearDown(self):
        self.memory_fixture.tearDown()
        await assignment_fixture.AssignmentReasoningTests.asyncTearDown(self)

    def record(self, key='scoped', **kw):
        return self.memory.create(KnowledgeCreate(logical_key=key, object_type='repository',
            title='Priority exception technical architecture', summary='Review a bounded technical priority.',
            content='Current engineering architecture decision for priority exception.', project_id='project-a',
            provenance=KnowledgeProvenance(source_kind='manual', source_ref='private:reviewed'), **kw), actor=self.actor)

    async def test_real_memory_assignment_create_read_and_consult_scope(self):
        record = self.record(resource_ids=(self.repo.id,))
        created = await self.call('executive.activation.create', self.request())
        memory = created['item']['context']['memory']
        self.assertEqual(memory[0]['knowledge_id'], record.id)
        self.assertEqual(memory[0]['resource_ids'], [self.repo.id])
        self.assertEqual(memory[0]['content_sha256'], record.content_sha256)
        self.assertEqual((await self.call('executive.activation.read'))['count'], 1)
        result = await self.call('executive.activation.consult', target=created['item']['id'])
        self.assertEqual(result['item']['status'], 'completed')
        self.assertTrue(self.fixture.gateway.requests)

    async def test_legacy_memory_still_refuses_without_creating_activation(self):
        self.record()
        with self.assertRaises(ExecutiveContextError):
            await self.call('executive.activation.create', self.request())
        self.assertEqual(self.fixture.service.list(actor=self.actor), ())
        self.assertEqual(self.fixture.gateway.requests, [])

    async def test_foreign_resource_and_changed_snapshot_refuse(self):
        record = self.record(resource_ids=(self.repo.id,))
        created = await self.call('executive.activation.create', self.request())
        item = self.fixture.service.get(created['item']['id'], actor=self.actor)
        operation = next(op for op in assignment_fixture.OPERATIONS if op.id == 'executive.activation.read')
        for field, value in [('resource_ids', ['foreign']), ('version', 99), ('content_sha256', '0'*64), ('context_excerpt', 'forged private content')]:
            forged = item.model_copy(deep=True)
            forged.context.memory[0][field] = value
            with self.assertRaises(HTTPException):
                self.adapter.prepared_context(forged, self.live, self.actor, operation)
        self.live.resource_ids = self.assignment.resource_ids = ('foreign',)
        with self.assertRaises(HTTPException):
            self.adapter.prepared_context(item, self.live, self.actor, operation)
        self.assertEqual(self.fixture.gateway.requests, [])

    async def test_unknown_empty_unbound_and_disabled_resource_ingestion_refuse(self):
        with self.assertRaises(ValidationError):
            self.record(resource_ids=())
        with self.assertRaises(KnowledgeAuthorizationError):
            self.record(resource_ids=('missing',))
        other = self.resources.create(ResourceCreate(resource_type='repository', name='Unbound'), actor=self.actor)
        with self.assertRaises(KnowledgeAuthorizationError):
            self.record(resource_ids=(other.id,))
        self.resources.update(self.repo.id, ResourceUpdate(lifecycle='disabled'), actor=self.actor)
        with self.assertRaises(KnowledgeAuthorizationError):
            self.record(resource_ids=(self.repo.id,))

    async def test_derived_union_and_unknown_source_not_laundered(self):
        first = self.record(resource_ids=(self.repo.id,))
        derived = self.record('derived', relationships=({'target_knowledge_id': first.id, 'relationship_type': 'derived_from'},))
        self.assertEqual(derived.resource_ids, first.resource_ids)
        legacy = self.record('legacy')
        with self.assertRaises(KnowledgeValidationError):
            self.record('laundered', resource_ids=(self.repo.id,), relationships=({'target_knowledge_id': legacy.id, 'relationship_type': 'derived_from'},))
        other = self.resources.create(ResourceCreate(resource_type='repository', name='Other governed'), actor=self.actor)
        self.resources.bind_project(project=self.project, resource_id=other.id, actor=self.actor)
        with self.assertRaises(KnowledgeValidationError):
            self.memory.revise(first.id, KnowledgeRevise(resource_ids=(other.id,), provenance=first.provenance, reason='Cannot drop existing scope'), actor=self.actor)

    async def test_revoked_resource_denies_current_read_and_consult(self):
        self.record(resource_ids=(self.repo.id,))
        created = await self.call('executive.activation.create', self.request())
        self.resources.update(self.repo.id, ResourceUpdate(lifecycle='disabled'), actor=self.actor)
        with self.assertRaises(HTTPException):
            await self.call('executive.activation.consult', target=created['item']['id'])
        self.assertEqual(self.fixture.gateway.requests, [])

    async def test_legacy_migration_does_not_infer_or_accept_injected_resource_proof(self):
        from codex_web.compatibility import ContractSpec, ContractCompatibilityError
        legacy = self.record()
        state = self.memory.store.store.get(self.memory.store.namespace)
        state['schema_version'] = '1.2'
        state['records'][0]['resource_ids'] = [self.repo.id]
        self.memory.store.store.put(self.memory.store.namespace, state)
        restored = self.memory.store.load()
        self.assertEqual(restored.schema_version, '1.3')
        self.assertIsNone(restored.records[0].resource_ids)
        with self.assertRaises(ContractCompatibilityError):
            ContractSpec('organizational-memory-state', '1.2', ('1.0', '1.1', '1.2')).require('1.3')
        with self.assertRaises(KnowledgeValidationError):
            self.memory.revise(legacy.id, KnowledgeRevise(resource_ids=(self.repo.id,),
                provenance=legacy.provenance, reason='Unknown lineage cannot become proof'), actor=self.actor)

    async def test_retained_governed_unknown_source_cannot_be_omitted_to_relabel(self):
        source = self.record('unknown-source')
        derived = self.memory.create(
            KnowledgeCreate(logical_key='governed-derived', object_type='other', title='Derived note',
                summary='Derived note', content='Retained derived content', project_id='project-a',
                provenance=KnowledgeProvenance(source_kind='manual', source_ref='private:derived',
                    source_governance_record_ids=(source.governance_record_id,))), actor=self.actor)
        with self.assertRaises(KnowledgeValidationError):
            self.memory.revise(derived.id, KnowledgeRevise(resource_ids=(self.repo.id,),
                provenance=KnowledgeProvenance(source_kind='manual', source_ref='private:review'),
                reason='Omitting retained sources cannot grant Resource proof'), actor=self.actor)

    async def test_mixed_legacy_scoped_foreign_memory_retains_real_context_and_denials(self):
        legacy = self.record('legacy-unproven')
        allowed = self.record('allowed', resource_ids=(self.repo.id,))
        other = self.resources.create(ResourceCreate(resource_type='repository', name='Other'), actor=self.actor)
        self.resources.bind_project(project=self.project, resource_id=other.id, actor=self.actor)
        foreign = self.record('foreign-scope', resource_ids=(other.id,))
        result = await self.call('executive.activation.create', self.request())
        self.assertEqual([row['knowledge_id'] for row in result['item']['context']['memory']], [allowed.id])
        run_id = result['item']['context']['memory_retrieval_ids'][0]
        run = next(row for row in self.memory.store.load().retrieval_runs if row.id == run_id)
        self.assertEqual(run.filters['resource_ids'], [self.repo.id])
        self.assertEqual({row.knowledge_id: row.reason for row in run.denied}, {
            legacy.id: 'resource_scope:unknown', foreign.id: 'resource_scope:outside_requested_resources'})
        self.assertEqual(run.selected_knowledge_ids, (allowed.id,))
        # Ordinary human retrieval still uses its original governed full project scope.
        human = self.memory.search(KnowledgeQuery(text='Priority exception', project_ids=('project-a',)), actor=self.actor)
        self.assertEqual({row.knowledge_id for row in human.items}, {legacy.id, allowed.id, foreign.id})

    async def test_real_evidence_and_artifact_resources_cannot_be_dropped(self):
        from codex_web.artifact_evidence import ArtifactCreate, EvidenceCreate
        from codex_web.services.artifact_evidence import ArtifactEvidenceService
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        owner = ArtifactEvidenceService(ArtifactEvidenceStore(self.memory.store.store),
            resources=self.resources, governance=self.memory.governance)
        self.memory.artifact_evidence = owner
        other = self.resources.create(ResourceCreate(resource_type='repository', name='Artifact source'), actor=self.actor)
        self.resources.bind_project(project=self.project, resource_id=other.id, actor=self.actor)
        artifact = owner.create_artifact(ArtifactCreate(project_id='project-a', resource_ids=(other.id,),
            artifact_type='report', name='Canonical source report'), actor=self.actor)
        evidence = owner.create_evidence(EvidenceCreate(project_id='project-a', resource_ids=(self.repo.id,),
            evidence_type='review', result='pass', artifact_ids=(artifact.id,)), actor=self.actor)
        provenance = KnowledgeProvenance(source_kind='evidence', source_ref='private:evidence',
            origin='human_observed', evidence_ids=(evidence.id,))
        payload = dict(logical_key='evidence-derived', object_type='other', title='Observed source',
            summary='Observed source', content='Observed source', project_id='project-a', provenance=provenance)
        with self.assertRaises(KnowledgeValidationError):
            self.memory.create(KnowledgeCreate(**payload, resource_ids=(self.repo.id,)), actor=self.actor)
        record = self.memory.create(KnowledgeCreate(**payload, resource_ids=(self.repo.id, other.id)), actor=self.actor)
        self.assertEqual(set(record.resource_ids), {self.repo.id, other.id})
        self.assertIn(artifact.governance_record_id, record.provenance.source_governance_record_ids)
        self.assertIn(evidence.governance_record_id, record.provenance.source_governance_record_ids)

    async def test_scratch_admin_uses_only_current_published_administrative_envelope(self):
        from unittest.mock import Mock
        self.record(resource_ids=(self.repo.id,))
        self.assignment.execution_profile_id = self.live.execution_profile_id = 'orchestration-admin'
        self.assignment.resource_ids = self.live.resource_ids = ()
        self.broker.authority = self.memory.authority
        profile = NS(control_plane_operations=self.allowed, administrative_repository_ids=(self.repo.id,))
        self.broker.execution_profiles.resolve = lambda *args, **kwargs: (profile, 'exact-ref')
        self.broker.execution_profiles.authorize_administrative_target = Mock()
        result = await self.call('executive.activation.create', self.request())
        self.assertEqual(result['item']['context']['memory'][0]['resource_ids'], [self.repo.id])
        self.assertTrue(self.broker.execution_profiles.authorize_administrative_target.called)
        for call in self.broker.execution_profiles.authorize_administrative_target.call_args_list:
            self.assertEqual(call.kwargs['repository_id'], self.repo.id)
            self.assertIs(call.kwargs['actor'], self.actor)

    async def test_bound_revision_preserves_resource_union_and_source_provenance(self):
        first = self.record(resource_ids=(self.repo.id,))
        revised = self.memory.revise(first.id, KnowledgeRevise(summary='Updated reviewed summary',
            provenance=first.provenance, reason='Retain governing Resource envelope'), actor=self.actor)
        self.assertEqual(revised.resource_ids, first.resource_ids)
        self.assertIn(first.governance_record_id, revised.provenance.source_governance_record_ids)
        self.assertEqual(revised.previous_version_id, first.id)
        self.assertEqual(self.memory.store.get(first.id).resource_ids, first.resource_ids)

    async def test_governance_denial_cannot_become_success_by_empty_memory(self):
        record = self.record(resource_ids=(self.repo.id,), deny_model_context=True)
        with self.assertRaises(ExecutiveContextError):
            await self.call('executive.activation.create', self.request())
        self.assertEqual(self.fixture.service.list(actor=self.actor), ())
        self.assertEqual(self.fixture.gateway.requests, [])
        run = self.memory.store.load().retrieval_runs[-1]
        self.assertTrue(any(row.knowledge_id == record.id and row.reason.startswith('governance:') for row in run.denied))
