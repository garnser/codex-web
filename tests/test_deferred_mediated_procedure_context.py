"""Real provider admission defers unused discovery, never current authorization."""
import copy
from contextlib import contextmanager
import time
from types import MethodType
import unittest
from unittest.mock import patch

from codex_web.action_intents import ActionDecisionSnapshot, ActionIntentCreate
from codex_web.agent_providers import AgentProviderUpsert
from codex_web.definitions import DefinitionReference
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.model_gateway import ModelInvocationRecord
from codex_web.procedure_discovery import procedure_action_intent
from codex_web.resources import ResourceUpdate
from codex_web.services.action_providers import ActionRequirementError
from codex_web.services.agent_providers import AgentProviderService
from codex_web.services.agent_runtime import AgentRuntimeRegistry
from codex_web.services.procedure_discovery import (
    ProcedureAdmissionError, ProcedureExecutionCapabilityService,
)
from codex_web.services.procedure_execution import ProcedureExecutionError
from codex_web.services.skills import SkillResolutionFailure
from codex_web.skills import SkillPublish, SkillUpdate
from codex_web.storage.agent_providers import AgentProviderStore
from codex_web.storage.model_gateway import ModelGatewayStore
from tests import test_agent_runtime as runtimes
from tests import test_mr_guarded_readonly_budget as guarded


class DeferredMediatedProcedureContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = None

    async def asyncTearDown(self):
        if self.f is not None:
            await self.f.asyncTearDown()
            self.f.doCleanups()

    async def prepare(self, *, required_capability=False):
        f = self.f = guarded.policy_fixture.MRObservationPolicyTests(
            'test_v2_real_publish_start_readonly_receipt_evidence_after_47_seconds')
        await f.asyncSetUp()
        f.configure(180)
        self.gateway = ModelGatewayStore(f.sqlite)
        # Modest valid accounting history: no prompt/messages/credentials, model
        # inference or timing threshold. Count actual document reads below.
        records = [ModelInvocationRecord(organization_id=f.admin.organization_id,
            workspace_id=f.admin.workspace_id, actor_id=f.admin.identity_id,
            model_class='private-accounting', purpose='Synthetic completed accounting metadata',
            prompt_template_id='private-template', prompt_template_version='1',
            prompt_template_checksum_sha256='0' * 64, rendered_prompt_sha256='0' * 64,
            message_count=0, input_character_count=0, policy_fingerprint_sha256='0' * 64,
            route_reason='Private synthetic history', status='completed') for _ in range(32)]
        self.gateway.update(lambda state: state.model_copy(update={'invocations': records}))
        self.adapter = runtimes._Runtime()
        self.runtime_registry = AgentRuntimeRegistry()
        self.runtime_registry.register(self.adapter, capability_revision=7)
        self.providers = AgentProviderService(AgentProviderStore(f.sqlite), model_gateway=self.gateway)
        self.provider_config = AgentProviderUpsert(id=self.adapter.provider_id,
            display_name='Private capability-only runtime', declared_capabilities=self.adapter.capabilities,
            granted_capabilities=self.adapter.capabilities, health='healthy')
        self.providers.upsert(self.provider_config, actor=f.admin)
        f.discovery.execution_capabilities = ProcedureExecutionCapabilityService(
            f.workers, self.runtime_registry, self.providers)
        f.assignment = f.workers.store.update_assignment(f.worker.id, f.assignment.id,
            lambda state: state.model_copy(update={'assignments': [
                state.assignments[0].model_copy(update={'runtime_binding': ExecutionRuntimeBinding(
                    provider_id=self.adapter.provider_id, runtime_id=self.adapter.runtime_id,
                    capability_revision=7)})]})).assignments[0]
        if required_capability:
            record = f.registry.get_record(f.pin.record_id)
            procedure = copy.deepcopy(record.payload['operational_procedure'])
            procedure['required_provider_capabilities'] = ['agent_execution']
            updated = f.skills.update(record.definition_id, SkillUpdate(operational_procedure=procedure,
                reason='Private required current provider capability'), actor=f.admin)
            published = f.skills.publish(updated['skillId'], updated['recordId'], SkillPublish(), actor=f.admin)
            f.pin = DefinitionReference.model_validate(published['definitionReference'])
        await guarded.publish_guarded_skill(f)
        published = await f.publish_v2()
        started = await f.start_snapshot(published['procedure_input']['id'])
        assignment = f.workers.store.assignment(started.id)
        procedure = f.runner._procedure(assignment, f.admin)
        self.step = step = procedure.steps[0]
        inputs = f.runner._inputs(assignment, f.admin, procedure)
        binding, provider, definition, request = f.runner._request(step, assignment, f.admin,
            procedure, f.runner._step_inputs(step, assignment.procedure_checkpoint, inputs, f.admin, assignment))
        checkpoint = assignment.procedure_checkpoint
        checkpoint.status = 'running'
        checkpoint.current_step_id = step.id
        state = checkpoint.steps[step.id]
        state.started_at = f.runner.clock()
        state.status = 'running'
        state.attempts = 1
        assignment = f.runner._save(assignment, checkpoint)
        self.intent = f.intents.create(ActionIntentCreate(binding_id=binding.id, request=request,
            execution_id=assignment.execution_id, work_item_ref=assignment.work_item_ref,
            timeout_seconds=step.timeout_seconds,
            policy_decision=ActionDecisionSnapshot(outcome='allow', source='canonical:procedure-step')),
            actor=f.admin)
        state.action_intent_id = self.intent.id
        f.runner._save(assignment, checkpoint)
        self.binding, self.provider, self.definition = binding, provider, definition
        self.provider_calls_before = len(f.observed_calls)

    @contextmanager
    def gateway_reads(self):
        original = self.f.sqlite.get
        reads = []
        def get(namespace):
            if namespace == self.gateway.namespace:
                reads.append(namespace)
            return original(namespace)
        with patch.object(self.f.sqlite, 'get', get):
            yield reads

    async def admit(self, *, phase='execute'):
        with procedure_action_intent(self.intent.id):
            return await self.f.intents.procedure_provider_admission(self.binding, self.provider,
                self.definition, self.intent.request, self.f.worker_actor, phase)

    def no_provider_work(self):
        self.assertEqual(len(self.f.observed_calls), self.provider_calls_before)
        self.f.runner.models.invoke.assert_not_called()
        self.assertEqual(self.adapter.calls, [])

    async def test_real_current_mediation_does_not_read_discarded_model_gateway_context(self):
        await self.prepare()
        for phase in ('execute', 'verify'):
            with self.subTest(phase=phase), self.gateway_reads() as reads:
                deadline = await self.admit(phase=phase)
                self.assertIsInstance(deadline, float)
                self.assertGreater(deadline, time.time())
                self.assertEqual(reads, [], 'Truthy current mediation must not construct unused discovery context')
        self.no_provider_work()

    async def test_required_capability_reads_and_next_call_revocation_are_preserved(self):
        await self.prepare(required_capability=True)
        with self.gateway_reads() as reads:
            self.assertGreater(await self.admit(), time.time())
        self.assertGreater(len(reads), 0, 'Required current capability discovery is not discarded work')
        self.providers.upsert(self.provider_config.model_copy(update={'granted_capabilities': ()}), actor=self.f.admin)
        with self.gateway_reads() as reads:
            with self.assertRaises(ProcedureExecutionError) as refused:
                await self.admit()
        self.assertEqual(refused.exception.code, 'procedure_live_provider_capabilities_unavailable')
        self.assertGreater(len(reads), 0)
        self.no_provider_work()

    async def test_current_role_revocation_still_refuses_before_provider(self):
        await self.prepare()
        self.assertGreater(await self.admit(), time.time())
        role = self.f.authority.catalog_record(actor=self.f.admin, project_id='project-a')
        self.f.registry.quarantine(role.record_id, actor=self.f.admin.identity_id, reason='Private current Role revocation')
        with self.assertRaisesRegex(ActionRequirementError, 'procedure_authority_denied'):
            await self.admit()
        self.no_provider_work()

    async def test_current_resource_revocation_still_refuses_before_provider(self):
        await self.prepare()
        self.assertGreater(await self.admit(), time.time())
        self.f.resources.update(self.f.target.id, ResourceUpdate(lifecycle='disabled'), actor=self.f.admin)
        with self.assertRaisesRegex(ActionRequirementError, 'procedure_authority_denied'):
            await self.admit()
        self.no_provider_work()

    async def test_current_lease_expiry_still_refuses_before_provider(self):
        await self.prepare()
        self.assertGreater(await self.admit(), time.time())
        self.f.workers.store.update_assignment(self.f.worker.id, self.f.assignment.id,
            lambda state: state.model_copy(update={'assignments': [state.assignments[0].model_copy(
                update={'lease': state.assignments[0].lease.model_copy(update={'expires_at': time.time() - 1})})]}))
        with self.assertRaises(ProcedureExecutionError) as refused:
            await self.admit()
        self.assertEqual(refused.exception.code, 'procedure_assignment_fence_expired_or_revoked')
        self.no_provider_work()

    async def test_current_procedure_revocation_still_refuses_before_provider(self):
        await self.prepare()
        self.assertGreater(await self.admit(), time.time())
        self.f.registry.quarantine(self.f.pin.record_id, actor=self.f.admin.identity_id,
                                   reason='Private current procedure revocation')
        with self.assertRaises(SkillResolutionFailure) as refused:
            await self.admit()
        self.assertEqual(refused.exception.code, 'procedure_quarantined')
        self.no_provider_work()

    async def test_custom_context_and_capability_owners_keep_eager_context(self):
        await self.prepare()
        f = self.f
        cases = [(f.intents, '_procedure_context'), (f.discovery, 'action_context'),
                 (f.discovery, 'assignment_context'), (f.discovery, 'resource_facts'),
                 (f.discovery.execution_capabilities, 'resolve')]
        for owner, name in cases:
            with self.subTest(owner=type(owner).__name__, method=name):
                original = getattr(owner, name)
                called = []
                def custom(*args, **kwargs):
                    called.append(name)
                    return original(*args, **kwargs)
                with patch.object(owner, name, custom), self.gateway_reads() as reads:
                    self.assertGreater(await self.admit(), time.time())
                self.assertTrue(called)
                self.assertEqual(len(reads), 1)
        original_class_method = type(f.discovery).action_context
        class_calls = []
        def class_context(owner, *args, **kwargs):
            class_calls.append('class-context')
            return original_class_method(owner, *args, **kwargs)
        with patch.object(type(f.discovery), 'action_context', class_context), self.gateway_reads() as reads:
            self.assertGreater(await self.admit(), time.time())
        self.assertEqual(class_calls, ['class-context'])
        self.assertEqual(len(reads), 1)
        before = len(f.intents.get(self.intent.id, f.admin).procedure_discoveries)
        failure = RuntimeError('Private custom context refusal')
        with patch.object(f.intents, '_procedure_context', side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:
                await self.admit()
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(f.intents.get(self.intent.id, f.admin).procedure_discoveries), before)
        # Patch the CLASS while the stock mediator remains untouched. Comparing
        # against a mutable live class attribute would wrongly discard this
        # owner's refusal before reaching it.
        for owner, name in ((f.intents, '_procedure_context'), (f.discovery, 'action_context')):
            with self.subTest(class_refusal=name), self.gateway_reads() as reads:
                failure = RuntimeError('Private original class context refusal')
                with patch.object(type(owner), name, side_effect=failure):
                    with self.assertRaises(RuntimeError) as caught:
                        await self.admit()
                self.assertIs(caught.exception, failure)
                self.assertEqual(reads, [])
                self.assertEqual(len(f.intents.get(self.intent.id, f.admin).procedure_discoveries), before)
        self.no_provider_work()

    async def test_rebound_and_replaced_owner_types_keep_eager_context(self):
        await self.prepare()
        f = self.f
        for owner, name in ((f.intents, '_procedure_context'), (f.discovery, 'assignment_context'),
                            (f.discovery.execution_capabilities, 'resolve')):
            with self.subTest(rebound=name):
                foreign = copy.copy(owner)
                rebound = MethodType(getattr(type(owner), name), foreign)
                with patch.object(owner, name, rebound), self.gateway_reads() as reads:
                    self.assertGreater(await self.admit(), time.time())
                self.assertEqual(len(reads), 1)
        for owner in (f.intents, f.discovery, f.runner, f.discovery.execution_capabilities):
            with self.subTest(replaced=type(owner).__name__):
                original_type = type(owner)
                replacement = type('CustomOwner', (original_type,), {})
                owner.__class__ = replacement
                try:
                    with self.gateway_reads() as reads:
                        self.assertGreater(await self.admit(), time.time())
                    self.assertEqual(len(reads), 1)
                finally:
                    owner.__class__ = original_type
        self.no_provider_work()

    async def test_custom_mediator_and_current_hook_keep_context_before_mediation(self):
        await self.prepare()
        f = self.f
        for name in ('provider_admission', 'provider_admission_current'):
            with self.subTest(method=name):
                original = getattr(f.runner, name)
                at_entry = []
                with self.gateway_reads() as reads:
                    if name == 'provider_admission':
                        async def custom(*args, **kwargs):
                            at_entry.append(len(reads))
                            return await original(*args, **kwargs)
                    else:
                        def custom(*args, **kwargs):
                            at_entry.append(len(reads))
                            return original(*args, **kwargs)
                    with patch.object(f.runner, name, custom):
                        self.assertGreater(await self.admit(), time.time())
                self.assertEqual(at_entry, [1])
        for name in ('provider_admission', 'provider_admission_current'):
            foreign_mediator = copy.copy(f.runner)
            with patch.object(f.runner, name, getattr(foreign_mediator, name)), self.gateway_reads() as reads:
                self.assertGreater(await self.admit(), time.time())
            self.assertEqual(len(reads), 1)
        # A facade hook rebound to another ActionIntent owner is never treated
        # as the canonical same-owner hook, even if its function is identical.
        foreign = copy.copy(f.intents)
        with patch.object(f.intents.execution, 'procedure_admission', foreign.procedure_provider_admission), self.gateway_reads() as reads:
            self.assertGreater(await self.admit(), time.time())
        self.assertEqual(len(reads), 1)
        self.no_provider_work()

    async def test_false_none_and_absent_interpreter_build_one_context_and_record_discovery(self):
        await self.prepare()
        f = self.f
        for outcome in (False, None):
            with self.subTest(outcome=outcome):
                before = len(f.intents.get(self.intent.id, f.admin).procedure_discoveries)
                calls = []
                with self.gateway_reads() as reads:
                    async def custom(*_args):
                        calls.append(len(reads))
                        return outcome
                    with patch.object(f.runner, 'provider_admission', custom):
                        with self.assertRaises(ProcedureAdmissionError):
                            await self.admit()
                self.assertEqual(calls, [1])
                self.assertEqual(len(reads), 1)
                self.assertEqual(len(f.intents.get(self.intent.id, f.admin).procedure_discoveries), before + 1)
        before = len(f.intents.get(self.intent.id, f.admin).procedure_discoveries)
        with patch.object(f.intents, 'procedure_execution', None), self.gateway_reads() as reads:
            await self.admit()
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(f.intents.get(self.intent.id, f.admin).procedure_discoveries), before + 1)
        self.no_provider_work()

    async def test_stock_false_mediation_falls_back_after_missing_checkpoint(self):
        await self.prepare()
        f = self.f
        f.workers.store.update_assignment(f.worker.id, f.assignment.id,
            lambda state: state.model_copy(update={'assignments': [
                state.assignments[0].model_copy(update={'procedure_checkpoint': None})]}))
        before = len(f.intents.get(self.intent.id, f.admin).procedure_discoveries)
        with self.gateway_reads() as reads:
            with self.assertRaises(ProcedureAdmissionError):
                await self.admit()
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(f.intents.get(self.intent.id, f.admin).procedure_discoveries), before + 1)
        self.no_provider_work()

    async def test_rollback_context_rewrite_and_mediator_failure_keep_original_order(self):
        await self.prepare()
        f = self.f
        contexts = []
        original = f.discovery.discover
        def discovery(context, **kwargs):
            contexts.append(context)
            return original(context, **kwargs)
        async def no_mediation(*_args):
            return False
        with patch.object(f.runner, 'provider_admission', no_mediation), patch.object(f.discovery, 'discover', discovery):
            with self.assertRaises(ProcedureAdmissionError):
                await self.admit(phase='rollback')
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].operation, self.intent.action_id + '.rollback')
        self.assertEqual(contexts[0].enforcement, 'canonical_rollback')
        before = len(f.intents.get(self.intent.id, f.admin).procedure_discoveries)
        failure = RuntimeError('Private custom mediator refusal')
        with self.gateway_reads() as reads:
            async def refused(*_args):
                self.assertEqual(len(reads), 1)
                raise failure
            with patch.object(f.runner, 'provider_admission', refused):
                with self.assertRaises(RuntimeError) as caught:
                    await self.admit()
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(f.intents.get(self.intent.id, f.admin).procedure_discoveries), before)
        self.no_provider_work()
