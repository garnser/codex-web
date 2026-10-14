"""Real ActionIntent-to-provider boundary, not a fake discovery return value."""
import time
import unittest
from unittest.mock import patch

from codex_web.action_intents import ActionDecisionSnapshot, ActionIntentCreate, ActionIntentStatus
from codex_web.action_providers import ActionProviderBindingCreate, ActionRequest, ActionResult
from codex_web.resources import ResourceCreate, ResourceOperationalContext, ResourceType
from codex_web.services.action_providers import ActionRequirementError
from codex_web.services.procedure_discovery import ProcedureDiscoveryService, ProcedureExecutionCapabilityService
from codex_web.services.skills import SkillService
from codex_web.skills import SkillCreate, SkillPublish, SkillUpdate
from tests import test_action_intents as actions
from tests import test_skill_procedures as procedures
from codex_web.services.skill_security import SkillSecurityService
from codex_web.services.skill_scanners import SkillScannerRegistry
from codex_web.storage.skill_security import SkillSecurityStore
from tests.test_skill_security import FakeScanner


class LiveOpsProvider(procedures.MetadataProvider):
    def __init__(self):
        self.calls = []

    async def execute(self, request, *, binding, credential=None):
        self.calls.append(request.action_id)
        return ActionResult(provider_binding_id=binding.id, action_id=request.action_id,
            status="succeeded", started_at=time.time(), completed_at=time.time())


class ProcedureActionAdmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = actions.ActionIntentTests.asyncTearDown
    _execute = actions.ActionIntentTests._execute

    async def asyncSetUp(self):
        await actions.ActionIntentTests.asyncSetUp(self)
        self.ops = LiveOpsProvider()
        self.registry.register(self.ops, tenant_scope=self.actor.tenant)
        self.target = self.resources.create(ResourceCreate(resource_type=ResourceType.SERVICE, name="GitLab",
            operational_context=ResourceOperationalContext(environment="staging", service_versions={"gitlab": "18.0.0"})), actor=self.actor)
        self.ops_binding = self.registry.bind(ActionProviderBindingCreate(provider_type="ops", provider_instance="test",
            resource_ids=(self.target.id,)), actor=self.actor, resources=self.resources)
        self.skills = SkillService(self.definition_registry, clock=lambda: self.now)
        self.now = 100
        self.skills.bind_procedure_contracts(actions=self.registry, resources=self.resources, scope_actor=lambda record: self.actor)
        scanners = SkillScannerRegistry(); scanners.register(FakeScanner())
        self.skills.bind_security(SkillSecurityService(SkillSecurityStore(self.sqlite), self.definition_registry, scanners, clock=lambda: self.now))
        from codex_web.execution_workers import (ExecutionWorkerRegister, ExecutionAssignmentCreate,
            ExecutionRuntimeBinding, WorkerCapability, AssignmentClaimRequest, AssignmentStartRequest)
        from codex_web.execution_subjects import ExecutionSubject, ExecutionSubjectKind
        from codex_web.services.execution_workers import ExecutionWorkerService
        from codex_web.storage.execution_workers import ExecutionWorkerStore
        from codex_web.services.agent_runtime import AgentRuntimeRegistry
        from codex_web.services.agent_providers import AgentProviderService
        from codex_web.storage.agent_providers import AgentProviderStore
        from codex_web.agent_providers import AgentProviderUpsert
        from tests.test_agent_runtime import _Runtime
        self.procedure_worker_actor = self.identity.bootstrap_service_actor(identity_id='procedure-worker',
            name='Procedure worker', scope=self.actor.tenant, service_scopes=('execution-worker:run',))
        self.workers = ExecutionWorkerService(ExecutionWorkerStore(self.sqlite), identity=self.identity)
        self.procedure_worker = self.workers.register(ExecutionWorkerRegister(service_identity_id='procedure-worker',
            version='1.0.0', max_concurrency=4, capabilities=(WorkerCapability.COMMAND_EXECUTION, WorkerCapability.NETWORK, WorkerCapability.GIT)), actor=self.actor)
        self.runtimes = AgentRuntimeRegistry()
        self.adapter = _Runtime()
        self.runtimes.register(self.adapter, capability_revision=7)
        self.providers = AgentProviderService(AgentProviderStore(self.sqlite))
        self.provider_config = AgentProviderUpsert(id=self.adapter.provider_id, display_name='Procedure runtime',
            declared_capabilities=self.adapter.capabilities, granted_capabilities=self.adapter.capabilities, health='healthy')
        self.providers.upsert(self.provider_config, actor=self.actor)
        pending = self.workers.create_assignment(ExecutionAssignmentCreate(
            subject=ExecutionSubject(kind=ExecutionSubjectKind.THREAD_BOOTSTRAP, ref='procedure-bootstrap'),
            execution_id='procedure-execution', resource_ids=(self.target.id,), execution_contract_version='1.0',
            required_capabilities=(WorkerCapability.COMMAND_EXECUTION,),
            runtime_binding=ExecutionRuntimeBinding(provider_id=self.adapter.provider_id, runtime_id=self.adapter.runtime_id,
                capability_revision=7)), actor=self.actor)
        claimed = self.workers.claim(self.procedure_worker.id, AssignmentClaimRequest(lease_seconds=3600),
            actor=self.procedure_worker_actor, assignment_id=pending.id)
        self.assignment = self.workers.start(self.procedure_worker.id, pending.id,
            AssignmentStartRequest(fence=claimed.fence, lease_token=claimed.lease.lease_token), actor=self.procedure_worker_actor)
        self.discovery = ProcedureDiscoveryService(self.skills, self.resources,
            execution_capabilities=ProcedureExecutionCapabilityService(self.workers, self.runtimes, self.providers))
        self.service.execution_workers = self.workers
        self.service.procedure_discovery = self.discovery
        self.execution.procedure_admission = self.service.procedure_provider_admission
        self.content = procedures.procedure()
        self.content['required_provider_capabilities'] = ['agent_execution']
        self.skill = self.skills.create(SkillCreate(skill_id="gitlab-procedure", name="GitLab logs", instructions="Consult the published bounded procedure.",
            operational_procedure=self.content), actor=self.actor)
        self.published = self.skills.publish(self.skill['skillId'], self.skill['recordId'], SkillPublish(), actor=self.actor)

    def create(self, **changes):
        values = dict(action_id="service.rotate", organization_id="local", workspace_id="default", resource_ids=(self.target.id,))
        values.update(changes)
        return self.service.create(ActionIntentCreate(binding_id=self.ops_binding.id, execution_id=self.assignment.execution_id, request=ActionRequest(**values),
            policy_decision=ActionDecisionSnapshot(source="canonical:test-existing-user-authorization", outcome="allow")), actor=self.actor)

    async def test_creation_and_immediate_provider_gate_pin_and_preserve_authorization(self):
        intent = self.create()
        self.assertEqual(intent.status, ActionIntentStatus.PENDING)
        self.assertEqual(intent.procedure_discoveries[0].outcome, "matched")
        self.assertEqual(self.ops.calls, [])
        completed = await self._execute(intent)
        self.assertEqual(completed.status, ActionIntentStatus.SUCCEEDED, completed.last_error)
        self.assertEqual(self.ops.calls, ['service.rotate'])
        self.assertEqual(self.content['required_worker_capabilities'], ['network'])
        self.assertTrue(any('worker' in step for step in self.content['steps']))
        self.assertNotIn('network', self.assignment.required_capabilities)
        self.assertIn('network', self.procedure_worker.capabilities)
        self.assertFalse(self.assignment.network.enabled)
        self.assertTrue(any(source.startswith('runtime:provider-a/runtime-a@7')
            for source in completed.procedure_discoveries[-1].capability_sources))
        self.assertEqual(len(completed.procedure_discoveries), 2)
        self.assertEqual(completed.procedure_discoveries[0].selected, completed.procedure_discoveries[1].selected)
        self.assertTrue(all(d.policy_source == "canonical:test-existing-user-authorization" for d in completed.procedure_discoveries))
        self.assertTrue(completed.authority_recheck.definition_refs)

    async def test_discovery_failure_at_mutation_is_a_known_precondition_failure(self):
        intent = self.create()
        with patch.object(self.skills.definitions, 'list_records', side_effect=OSError('unavailable')):
            completed = await self._execute(intent)
        self.assertEqual(completed.status, ActionIntentStatus.FAILED)
        self.assertEqual(completed.procedure_discoveries[-1].outcome, 'failed')
        self.assertEqual(self.ops.calls, [])
        self.assertNotIn('unknown', [r['outcome'] for r in self.service.history(intent.id, self.actor)['receipts']])

    async def test_expired_procedure_cannot_execute_after_queueing(self):
        intent = self.create()
        self.now = 201
        completed = await self._execute(intent)
        self.assertEqual(completed.status, ActionIntentStatus.FAILED)
        self.assertIn('procedure_expired', completed.procedure_discoveries[-1].blockers)
        self.assertEqual(self.ops.calls, [])

    async def test_supersession_requires_new_admission_and_keeps_old_pins(self):
        intent = self.create()
        newer = dict(self.content); newer['precedence'] = 11
        updated = self.skills.update(self.skill['skillId'], SkillUpdate(operational_procedure=newer, reason='Reviewed changed procedure'), actor=self.actor)
        self.skills.publish(self.skill['skillId'], updated['recordId'], SkillPublish(), actor=self.actor)
        completed = await self._execute(intent)
        self.assertEqual(completed.status, ActionIntentStatus.FAILED)
        self.assertEqual(completed.procedure_discoveries[0].selected[0].record_id, self.published['recordId'])
        self.assertNotEqual(completed.procedure_discoveries[0].selected, completed.procedure_discoveries[-1].selected)
        self.assertEqual(self.ops.calls, [])

    async def test_direct_provider_execution_cannot_bypass_intent_gate(self):
        intent = self.create()
        with self.assertRaisesRegex(ActionRequirementError, 'procedure_unsupported_enforcement'):
            await self.execution.execute(self.ops_binding.id, intent.request, actor=self.actor)
        self.assertEqual(self.ops.calls, [])

    async def test_existing_policy_denial_is_not_overridden_by_procedure(self):
        intent = self.service.create(ActionIntentCreate(binding_id=self.ops_binding.id,
            request=ActionRequest(action_id='service.rotate', organization_id='local', workspace_id='default', resource_ids=(self.target.id,)),
            policy_decision=ActionDecisionSnapshot(source='canonical:existing-policy', outcome='deny')), actor=self.actor)
        self.assertEqual(intent.status, ActionIntentStatus.CANCELLED)
        self.assertFalse(intent.procedure_discoveries[0].admitted)
        self.assertEqual(self.ops.calls, [])

    async def test_provider_grant_revocation_between_creation_and_mutation_blocks(self):
        intent = self.create()
        self.assertEqual(intent.status, ActionIntentStatus.PENDING)
        self.providers.upsert(self.provider_config.model_copy(update={'granted_capabilities': ()}), actor=self.actor)
        completed = await self._execute(intent)
        self.assertEqual(completed.status, ActionIntentStatus.FAILED)
        self.assertIn('provider_capabilities_missing', completed.procedure_discoveries[-1].blockers)
        self.assertEqual(self.ops.calls, [])

    async def test_unattributed_capabilities_remain_unknown(self):
        intent = self.service.create(ActionIntentCreate(binding_id=self.ops_binding.id,
            request=ActionRequest(action_id='service.rotate', organization_id='local', workspace_id='default', resource_ids=(self.target.id,)),
            policy_decision=ActionDecisionSnapshot(source='canonical:existing-policy', outcome='allow')), actor=self.actor)
        self.assertEqual(intent.status, ActionIntentStatus.CANCELLED)
        self.assertIn('provider_capabilities_unresolved', intent.procedure_discoveries[0].blockers)
        self.assertIn('worker_capabilities_unresolved', intent.procedure_discoveries[0].blockers)
        self.assertEqual(self.ops.calls, [])

    async def test_runtime_revision_skew_is_unknown_and_cannot_execute(self):
        from codex_web.services.agent_runtime import AgentRuntimeRegistry
        intent = self.create()
        changed = AgentRuntimeRegistry()
        changed.register(self.adapter, capability_revision=8)
        self.discovery.execution_capabilities.runtimes = changed
        completed = await self._execute(intent)
        self.assertEqual(completed.status, ActionIntentStatus.FAILED)
        self.assertIn('provider_capabilities_unresolved', completed.procedure_discoveries[-1].blockers)
        self.assertIn('provider:pinned_runtime_revision_changed', completed.procedure_discoveries[-1].blockers)
        self.assertEqual(self.ops.calls, [])

    async def test_quarantined_worker_is_unknown_and_cannot_execute(self):
        from codex_web.execution_workers import WorkerLifecycle
        intent = self.create()
        self.workers.set_lifecycle(self.procedure_worker.id, WorkerLifecycle.QUARANTINED, reason='Test quarantine', actor=self.actor)
        completed = await self._execute(intent)
        self.assertEqual(completed.status, ActionIntentStatus.FAILED)
        self.assertIn('worker_capabilities_unresolved', completed.procedure_discoveries[-1].blockers)
        self.assertEqual(self.ops.calls, [])

    async def test_assignment_discovery_failure_is_typed_and_never_no_match(self):
        with patch.object(self.workers, 'assignment_for_execution', side_effect=OSError('sensitive transport')):
            intent = self.create()
        self.assertEqual(intent.status, ActionIntentStatus.CANCELLED)
        self.assertEqual(intent.procedure_discoveries[0].outcome, 'failed')
        self.assertIn('procedure_assignment_lookup_failed', intent.procedure_discoveries[0].blockers)
        self.assertNotIn('sensitive', str(intent.procedure_discoveries[0]))
        self.assertEqual(self.ops.calls, [])

    async def test_assignment_lookup_failure_at_mutation_remains_known_precondition(self):
        intent = self.create()
        with patch.object(self.workers, 'assignment_for_execution', side_effect=OSError('sensitive lookup')):
            completed = await self._execute(intent)
        self.assertEqual(completed.status, ActionIntentStatus.FAILED)
        self.assertEqual(completed.procedure_discoveries[-1].outcome, 'failed')
        self.assertEqual(self.ops.calls, [])
        self.assertNotIn('unknown', [r['outcome'] for r in self.service.history(intent.id, self.actor)['receipts']])

    async def test_assignment_evidence_fence_failure_prevents_mutation(self):
        from codex_web.services.execution_workers import WorkerLeaseError
        intent = self.create()
        with patch.object(self.workers, 'record_procedure_discovery', side_effect=WorkerLeaseError('stale fence')):
            completed = await self._execute(intent)
        self.assertEqual(completed.status, ActionIntentStatus.FAILED)
        self.assertIn('procedure_assignment_evidence_unavailable', completed.procedure_discoveries[-1].blockers)
        self.assertEqual(self.ops.calls, [])
        self.assertNotIn('unknown', [r['outcome'] for r in self.service.history(intent.id, self.actor)['receipts']])
