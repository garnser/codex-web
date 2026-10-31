"""Real native custody and current admission, with physical metadata read counts."""
import collections
import copy
from contextlib import ExitStack
import importlib
import importlib.util
import logging
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from codex_web import guarded_verification_telemetry as telemetry
from codex_web.action_intents import ActionDecisionSnapshot, ActionIntentCreate
from codex_web.agent_providers import AgentProviderUpsert
from codex_web.definitions import DefinitionLifecycle, DefinitionReference
from codex_web.execution_workers import (
    AssignmentClaimRequest, AssignmentCompleteRequest, AssignmentStartRequest,
    ExecutionRuntimeBinding, ExecutionWorkerRegister, WorkerCapability,
)
from codex_web.execution_workspaces import ExecutionWorkspaceRelease
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.models import Project
from codex_web.resources import ResourceOperationalContext, ResourceUpdate
from codex_web.services.agent_providers import AgentProviderService
from codex_web.services.agent_runtime import AgentRuntimeRegistry
from codex_web.services.codex_worker_configuration import install_codex_worker_configuration
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from codex_web.services.projects import ProjectNotFoundError, ProjectService
from codex_web.services.procedure_execution import ProcedureExecutionError
from codex_web.services.skills import SkillResolutionFailure
from codex_web.services.procedure_discovery import (
    NativeProcedureAdmissionService, ProcedureExecutionCapabilityService,
)
from codex_web.services.turn_execution_binding import TurnExecutionBindingService
from codex_web.skills import SkillCreate, SkillPublish, SkillUpdate
from codex_web.storage.agent_providers import AgentProviderStore
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.thread_history import ThreadHistoryRepository
from codex_web.workspaces import WorkspaceMapper
from tests import test_agent_runtime as runtime_fixture
from tests import test_mr_guarded_readonly_budget as guarded
from tests import test_skill_procedures as contracts
from tests import test_turn_execution_binding as binding_fixture


class _PostInputReads(logging.Handler):
    """Observe existing closed stages; never replace a canonical owner method."""
    def __init__(self, intent):
        super().__init__(logging.INFO)
        self.intent = intent
        self.active = False
        self.inputs_finished = 0
        self.admissions_finished = 0
        self.counts = collections.Counter()

    def emit(self, record):
        row = getattr(record, 'structured', {})
        if (row.get('event') != 'guarded_readonly.verify.stage'
                or row.get('action_intent_id') != self.intent.id
                or row.get('execution_id') != self.intent.execution_id):
            return
        if row.get('stage') == 'inputs' and row.get('outcome') == 'completed':
            self.inputs_finished += 1
            self.active = True
        elif row.get('stage') == 'admission':
            self.admissions_finished += 1
            self.active = False


class PostInputAdmissionMetadataReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = f = guarded.policy_fixture.MRObservationPolicyTests(
            'test_v2_real_publish_start_readonly_receipt_evidence_after_47_seconds')
        await f.asyncSetUp()
        self.addAsyncCleanup(self.close_fixture)
        root = Path(f.temp.name)
        self.project = Project(id='project-a', organization_id=f.admin.organization_id,
            workspace_id=f.admin.workspace_id, name='Private native metadata', path=str(root / 'repo'))
        self.projects = ProjectService(ProjectRepository(root / 'projects.json', store=f.sqlite,
            workspace_mapper=WorkspaceMapper(root=root)))
        self.projects.repository.save([self.project])
        # Use production's canonical metadata-only principal when available.
        # On old6108, use its original application's equivalent callback instead
        # of importing a missing helper or importing the application itself.
        module_name = 'codex_web.services.procedure_scope_actor'
        if importlib.util.find_spec(module_name) is not None:
            scope_actor = importlib.import_module(module_name).ProcedureDefinitionScopeActor(f.identity, self.projects)
        else:
            scope_actor = self.legacy_scope_actor
        f.skills.bind_procedure_contracts(actions=f.intents.execution.registry,
            resources=f.resources, scope_actor=scope_actor)
        f.broker_response_timeout = 180
        f.configure(180)
        await guarded.publish_guarded_skill(f)
        record = f.registry.get_record(f.pin.record_id)
        procedure = copy.deepcopy(record.payload['operational_procedure'])
        procedure['max_duration_seconds'] = 180
        draft = f.skills.update(record.definition_id, SkillUpdate(operational_procedure=procedure,
            reason='Private parent180 with unchanged guarded step120'), actor=f.admin)
        published = f.skills.publish(draft['skillId'], draft['recordId'], SkillPublish(), actor=f.admin)
        f.pin = DefinitionReference.model_validate(published['definitionReference'])
        self.publish_merge_contract()
        old = f.workers.store.assignment(f.assignment.id)
        await f.broker.stop()
        f.workers.complete(f.worker.id, old.id, AssignmentCompleteRequest(
            lease_token=old.lease.lease_token, fence=old.fence, succeeded=False,
            failure_code='private_fixture_replaced'), actor=f.worker_actor)
        f.workspaces.release(old.execution_workspace_id,
            ExecutionWorkspaceRelease(reason='Replace private generic custody'), actor=f.admin)
        profile, _ = f.profiles.resolve('repository-write', organization_id=f.admin.organization_id,
            workspace_id=f.admin.workspace_id, project_id='project-a')
        capabilities = tuple(WorkerCapability(value) for value in profile.required_worker_capabilities)
        if profile.network_enabled:
            capabilities = tuple(dict.fromkeys((*capabilities, WorkerCapability.NETWORK)))
        f.worker = f.workers.register(ExecutionWorkerRegister(
            service_identity_id=f.worker_actor.identity_id, pool='private-native-metadata',
            version=f.worker.version, capabilities=capabilities,
            supported_execution_contract_versions=f.worker.supported_execution_contract_versions,
            supported_sandbox_profiles=f.worker.supported_sandbox_profiles, max_concurrency=1), actor=f.admin)
        self.adapter = runtime_fixture._Runtime()
        self.adapter.provider_id, self.adapter.runtime_id = 'openai', 'codex'
        runtimes = AgentRuntimeRegistry()
        runtimes.register(self.adapter, capability_revision=1)
        providers = AgentProviderService(AgentProviderStore(f.sqlite))
        providers.upsert(AgentProviderUpsert(id='openai', display_name='Private inert runtime',
            declared_capabilities=self.adapter.capabilities, granted_capabilities=self.adapter.capabilities,
            health='healthy'), actor=f.admin)
        f.discovery.execution_capabilities = ProcedureExecutionCapabilityService(f.workers, runtimes, providers)
        project = self.project
        f.resources.bind_project(project=project, resource_id=f.target.id, actor=f.admin)
        install_codex_worker_configuration(f.configuration)
        builder = TurnExecutionBindingService(f.configuration, binding_fixture._Projects(project),
            f.resources, f.workspaces, f.workers, control_actor=f.admin,
            execution_profiles=f.profiles, secrets=f.secret, local_session_probe=lambda: True)
        bound = builder.prepare_bootstrap(bootstrap_id='private-native-metadata',
            execution_id='thread-bootstrap-private-native-metadata', project_id=project.id,
            sandbox='workspace-write', approval_policy='on-request', explicit_repository_id=f.target.id,
            execution_profile_id='repository-write', agent_profile=old.agent_profile,
            runtime_binding=ExecutionRuntimeBinding(provider_id='openai', runtime_id='codex',
                capability_revision=1, authentication_mode='trusted_local_session'))
        claimed = f.workers.claim(f.worker.id, AssignmentClaimRequest(lease_seconds=300),
            actor=f.worker_actor, assignment_id=bound.assignment_id)
        self.assertIsNotNone(claimed)
        f.assignment = f.workers.start(f.worker.id, bound.assignment_id, AssignmentStartRequest(
            fence=claimed.fence, lease_token=claimed.lease.lease_token), actor=f.worker_actor)
        self.assertIsNone(f.assignment.native_credential_custody.admitted_at)
        self.assertEqual(f.assignment.secret_refs, ())
        admission = NativeProcedureAdmissionService(f.discovery, f.workers,
            ThreadHistoryRepository(f.sqlite), f.admin, f.profiles)
        admission.procedure_execution = f.runner
        admission.native_session_attestor = Mock()  # No native process is launched.
        discovery, _ = await admission(project=project, actor=f.admin, thread_id='private-native-metadata',
            execution_id=f.assignment.execution_id, assignment_id=f.assignment.id,
            sandbox=f.assignment.sandbox, source='queued:web', request_checksum='a' * 64)
        f.assignment = f.workers.store.assignment(f.assignment.id)
        self.assertTrue(discovery.admitted)
        custody = f.assignment.native_credential_custody
        self.assertIsNotNone(custody.admitted_at)
        self.assertEqual(len(custody.procedures), 2)
        selected = [f.registry.get_record(ref.record_id).payload['operational_procedure']
                    for ref in custody.procedures]
        self.assertCountEqual([step['action']['action_id'] for proc in selected
                              for step in proc['steps'] if step.get('action')], [
            'code-host.pull-request.observe-readiness.v2',
            'code-host.pull-request.merge', 'code-host.pull-request.verify-merge'])
        self.assertEqual(len(custody.provider_bindings), 1)
        self.assertEqual(len(f.assignment.secret_refs), 1)
        self.assertIsNone(custody.runtime_secret_ref)
        def current():
            assignment = f.workers.store.assignment(f.assignment.id)
            f.runner._live(assignment)
            return assignment
        f.broker = AssignmentBoundControlPlaneBroker(f.service, assignment=f.assignment,
            worker_id=f.worker.id, worker_actor=f.worker_actor, fence=f.assignment.fence, validator=current)
        await f.broker.start()
        published = await f.publish_v2()
        started = await f.start_snapshot(published['procedure_input']['id'])
        assignment = f.workers.store.assignment(started.id)
        procedure = f.runner._procedure(assignment, f.admin)
        step = procedure.steps[0]
        values = f.runner._inputs(assignment, f.admin, procedure)
        binding, _, _, request = f.runner._request(step, assignment, f.admin, procedure,
            f.runner._step_inputs(step, assignment.procedure_checkpoint, values, f.admin, assignment))
        checkpoint = assignment.procedure_checkpoint
        checkpoint.status, checkpoint.current_step_id = 'running', step.id
        state = checkpoint.steps[step.id]
        state.status, state.attempts, state.started_at = 'running', 1, f.runner.clock()
        assignment = f.runner._save(assignment, checkpoint)
        self.intent = f.intents.create(ActionIntentCreate(binding_id=binding.id, request=request,
            execution_id=assignment.execution_id, work_item_ref=assignment.work_item_ref,
            timeout_seconds=step.timeout_seconds,
            policy_decision=ActionDecisionSnapshot(outcome='allow', source='canonical:procedure-step')), actor=f.admin)
        state.action_intent_id = self.intent.id
        f.runner._save(assignment, checkpoint)

    def legacy_scope_actor(self, record):
        # Metadata-only source-equivalent of old6108 application wiring. It
        # authenticates nobody and gives neither service scopes nor Role grants.
        if record.scope_type.value == 'project':
            try:
                project = self.projects.get(record.scope_id)
            except ProjectNotFoundError:
                return None
            workspace_id, organization_id = project.workspace_id, project.organization_id
        elif record.scope_type.value == 'workspace':
            workspace_id, organization_id = record.scope_id, None
        else:
            return None
        workspace = next((row for row in self.f.identity.state().workspaces
            if row.id == workspace_id and (organization_id is None
                or row.organization_id == organization_id)), None)
        if workspace is None:
            return None
        return AuthenticationActor(identity_id='procedure-contract-metadata',
            principal_kind=PrincipalKind.SERVICE, organization_id=workspace.organization_id,
            workspace_id=workspace.id, assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=())

    async def close_fixture(self):
        await self.f.asyncTearDown()
        self.f.doCleanups()

    def publish_merge_contract(self):
        f = self.f
        def value(name, kind):
            return {'name': name, 'type': kind, 'description': 'Synthetic canonical MR value'}
        scalars = [value('pull_request_number', 'integer'), value('expected_head_sha', 'string'),
                   value('merge_method', 'string')]
        proofs = [contracts.value(name) for name in
                  ('independent_review_proof', 'exact_head_ci_proof', 'merge_policy_proof')]
        outputs = [value('pull_request_number', 'integer'), value('head_sha', 'string'),
                   value('merge_commit_sha', 'string')]
        def step(name, kind, action, inputs, declared_outputs):
            return {'id': name, 'kind': kind, 'description': 'Sealed contract only; never executed here',
                'inputs': inputs, 'outputs': declared_outputs, 'preconditions': [],
                'expected_evidence': [{'name': 'verification', 'kind': 'verification', 'description': 'Canonical verification'}],
                'timeout_seconds': 30, 'max_attempts': 1, 'retry_backoff_seconds': 0,
                'permissions': [{'capability': 'repository.pull-request.merge' if kind == 'mutation' else 'repository.read.metadata',
                    'level': 'execute' if kind == 'mutation' else 'read', 'resource_selector': 'repository'}],
                'failure': {'outcome': 'escalate', 'escalation_owner_identity_id': f.admin.identity_id,
                            'attention_reason': 'Preserve original outcome; no replay'},
                'action': {'provider_type': 'gitlab', 'provider_instance': 'gitlab.example',
                    'action_id': action, 'provider_contract_version': '1.2', 'resource_selector': 'repository'}}
        merge = step('merge', 'mutation', 'code-host.pull-request.merge', [*scalars, *proofs], outputs)
        merge.update(impact={'resource_selector': 'repository', 'change': 'configuration',
            'description': 'Never merge in this metadata test', 'max_unavailable_seconds': 0,
            'reversible': False}, verification_step_id='verify-merge')
        verify = step('verify-merge', 'verification', 'code-host.pull-request.verify-merge', outputs,
                      [*outputs, value('merge_verified', 'boolean')])
        verify['preconditions'] = [{'source': 'step', 'name': 'merge', 'predicate': 'succeeded'}]
        procedure = {'schema_version': '1.0', 'owner_identity_id': f.admin.identity_id,
            'task_triggers': ['private-second-selected'], 'operation_triggers': ['code-host.pull-request.merge'],
            'resource_selectors': [{'id': 'repository', 'resource_types': ['repository'], 'resource_ids': [f.target.id]}],
            'environments': ['staging'], 'inputs': [*scalars, *proofs],
            'reviewed_at': 1, 'review_due_at': 4102444800, 'expires_at': 4102444801,
            'required_provider_capabilities': [], 'required_worker_capabilities': [],
            'conflict_group': 'private-second-selected', 'precedence': 1, 'max_duration_seconds': 60,
            'steps': [merge, verify]}
        draft = f.skills.create(SkillCreate(skill_id='private-second-native-selection',
            name='Synthetic sealed merge and verification', instructions='Metadata-only; never merge here.',
            operational_procedure=procedure), actor=f.admin)
        f.skills.publish(draft['skillId'], draft['recordId'], SkillPublish(), actor=f.admin)

    def admit_observed(self, phase="execute"):
        f = self.f
        before = f.workers.store.assignment(f.assignment.id).model_dump(mode='json')
        calls_before = list(f.observed_calls)
        original_get = f.sqlite.get
        observer = self.last_observer = _PostInputReads(self.intent)
        namespaces = {'identity_state', 'definition_registry', 'resource_catalog'}
        def get(namespace):
            if observer.active and namespace in namespaces:
                observer.counts[namespace] += 1
            return original_get(namespace)
        logger = telemetry._logger
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(observer)
        try:
            with patch.object(f.sqlite, 'get', get), telemetry.guarded_verification_timings(self.intent):
                deadline = f.runner.provider_admission_current(self.intent, f.admin, phase)
        finally:
            logger.removeHandler(observer)
            logger.setLevel(previous_level)
            observer.close()
            self.assertFalse(observer.active)
            self.assertEqual(f.workers.store.assignment(f.assignment.id).model_dump(mode='json'), before)
            self.assertEqual(f.observed_calls, calls_before)
            self.assertEqual(self.adapter.calls, [])
            f.runner.models.invoke.assert_not_called()
            self.assertIsNone(telemetry._scope.get())
        self.assertGreater(deadline, f.runner.clock())
        self.assertEqual(observer.inputs_finished, 1)
        self.assertEqual(observer.admissions_finished, 1)
        return observer

    async def test_existing_current_admission_shares_post_input_catalog_reads(self):
        observer = self.admit_observed()
        namespaces = ('definition_registry', 'identity_state', 'resource_catalog')
        actual = {namespace: observer.counts[namespace] for namespace in namespaces}
        self.assertEqual(actual, {namespace: 1 for namespace in namespaces},
                         f'Actual post-input physical catalog loads: {actual}')

    async def test_next_verification_callback_reads_fresh_metadata(self):
        first = self.admit_observed('execute')
        second = self.admit_observed('verify')
        self.assertIsNot(first, second)
        self.assertEqual(second.counts, first.counts)
        self.assertTrue(all(second.counts[name] > 0 for name in
            ('definition_registry', 'identity_state', 'resource_catalog')))

    async def test_later_sealed_skill_revocation_refuses_then_restored_call_is_fresh(self):
        self.admit_observed()
        f = self.f
        later = next(ref for ref in f.assignment.native_credential_custody.procedures
                     if ref.record_id != f.pin.record_id)
        original = f.registry.get_record(later.record_id)
        store = f.skills.definitions.store
        store.update(lambda rows: [row.model_copy(update={'lifecycle': DefinitionLifecycle.QUARANTINED})
            if row.record_id == later.record_id else row for row in rows])
        with self.assertRaises(SkillResolutionFailure) as caught:
            self.admit_observed()
        self.assertEqual(caught.exception.code, 'procedure_quarantined')
        self.assertFalse(self.last_observer.active)
        store.update(lambda rows: [original if row.record_id == later.record_id else row for row in rows])
        restored = self.admit_observed()
        self.assertGreater(restored.counts['definition_registry'], 0)

    async def test_next_call_rechecks_bound_secret_revocation(self):
        self.admit_observed()
        f = self.f
        f.secret.revoke(f.assignment.secret_refs[0], actor=f.admin)
        with self.assertRaises(ProcedureExecutionError) as caught:
            self.admit_observed()
        self.assertEqual(caught.exception.code, 'procedure_credential_reference_revoked')

    async def test_next_call_rechecks_material_resource_facts(self):
        self.admit_observed()
        f = self.f
        current = f.resources.get(f.target.id, f.admin)
        context = current.operational_context or ResourceOperationalContext()
        f.resources.update(f.target.id, ResourceUpdate(operational_context=context.model_copy(
            update={'tags': (*context.tags, 'changed-after-admission')})), actor=f.admin)
        with self.assertRaises(ProcedureExecutionError) as caught:
            self.admit_observed()
        self.assertEqual(caught.exception.code, 'procedure_material_preconditions_changed')

    async def test_current_lease_expiry_still_refuses_before_provider(self):
        self.admit_observed()
        f = self.f
        def expire(state):
            state.assignments[0].lease = state.assignments[0].lease.model_copy(
                update={'expires_at': f.runner.clock() - 1})
            return state
        f.workers.store.update_assignment(f.worker.id, f.assignment.id, expire)
        with self.assertRaises(ProcedureExecutionError) as caught:
            self.admit_observed()
        self.assertEqual(caught.exception.code, 'procedure_assignment_fence_expired_or_revoked')

    async def test_custom_old_signature_owners_keep_whole_legacy_path(self):
        f = self.f
        owners = ((f.identity, 'actor_for_identity', 'identity_state'),
                  (f.resources, 'get', 'resource_catalog'))
        for owner, name, namespace in owners:
            with self.subTest(owner=type(owner).__name__):
                original = getattr(owner, name)
                calls = []
                if name == 'get':
                    def custom(resource_id, actor):
                        calls.append(resource_id)
                        return original(resource_id, actor)
                else:
                    def custom(identity_id, *, scope):
                        calls.append(identity_id)
                        return original(identity_id, scope=scope)
                with patch.object(owner, name, custom):
                    observed = self.admit_observed()
                self.assertTrue(calls)
                self.assertGreater(observed.counts[namespace], 1)
        # Removing custom callbacks restores the ordinary, fresh observation.
        self.admit_observed()

    async def test_custom_class_contract_actions_are_not_skipped(self):
        from codex_web.services.code_host_action_contract import CodeHostActionContract
        original = CodeHostActionContract.actions
        calls = []
        def custom(contract, *args, **kwargs):
            calls.append(contract.provider_type)
            return original(contract, *args, **kwargs)
        with patch.object(CodeHostActionContract, 'actions', custom):
            observed = self.admit_observed()
        self.assertIn('gitlab', calls)
        self.assertGreater(observed.counts['definition_registry'], 1)
        self.admit_observed()

    def assert_storage_hook_uses_legacy(self, owner, name, *, class_level):
        # Observe construction, not canonical observe/get/decode hooks: changing
        # one of those would independently disable the optimization under test.
        modules_and_classes = (
            ('codex_web.storage.identity_state', '_IdentityStateReadObservation'),
            ('codex_web.storage.resource_catalog', '_ResourceCatalogReadObservation'))
        opened = []
        def track(original):
            def initialize(view, *args, **kwargs):
                original(view, *args, **kwargs)
                observer = getattr(self, 'last_observer', None)
                if observer is not None and observer.active:
                    opened.append(view)
            return initialize
        original = getattr(owner, name)
        if name == '_event_records':
            def custom(events):
                return original(events)
            replacement = staticmethod(custom) if class_level else custom
        elif class_level:
            def custom(instance, payload):
                self.assertIs(instance, owner)
                return original(payload)
            replacement = custom
        else:
            def custom(payload):
                return original(payload)
            replacement = custom
        with ExitStack() as stack:
            for module_name, class_name in modules_and_classes:
                cls = getattr(importlib.import_module(module_name), class_name)
                stack.enter_context(patch.object(cls, '__init__', track(cls.__init__)))
            stack.enter_context(patch.object(type(owner) if class_level else owner, name, replacement))
            observed = self.admit_observed()
        # Current record collections are initialized. These compatibility hooks
        # need not execute or migrate data for their replacement to opt out.
        # No post-input shared Identity/Resource view may span such an owner.
        self.assertEqual(opened, [])
        for namespace in ('definition_registry', 'identity_state', 'resource_catalog'):
            self.assertGreater(observed.counts[namespace], 1,
                f'{type(owner).__name__}.{name} must retain whole legacy reads: {observed.counts}')

    async def test_instance_storage_compatibility_hooks_decline_shared_frame(self):
        f = self.f
        for owner, name in ((f.workers.store, '_decode'),
                            (f.workspaces.store, '_decode'),
                            (f.workspaces.store, '_migrate_records'),
                            (f.workspaces.store, '_event_records')):
            with self.subTest(owner=type(owner).__name__, hook=name):
                self.assert_storage_hook_uses_legacy(owner, name, class_level=False)

    async def test_class_storage_compatibility_hooks_decline_shared_frame(self):
        f = self.f
        for owner, name in ((f.workers.store, '_decode'),
                            (f.workspaces.store, '_decode'),
                            (f.workspaces.store, '_migrate_records'),
                            (f.workspaces.store, '_event_records')):
            with self.subTest(owner=type(owner).__name__, hook=name):
                self.assert_storage_hook_uses_legacy(owner, name, class_level=True)

    async def test_eligibility_declines_disguised_definition_and_security_owners_without_callbacks(self):
        f = self.f
        assignment = f.workers.store.assignment(f.assignment.id)
        procedure = f.runner._procedure(assignment, f.admin)
        step = procedure.steps[0]
        definitions = f.skills.definitions
        security_type = importlib.import_module(
            'codex_web.services.skill_security').SkillSecurityService
        touched = []
        def forbidden(*args, **kwargs):
            touched.append('custom callback')
            raise AssertionError('Eligibility must not invoke a custom owner')
        class DisguisedDefinitions(type(definitions)):
            can_observe_reads = forbidden
            get_record = forbidden
            @property
            def store(self):
                return forbidden()
        class DisguisedStore(type(definitions.store)):
            can_observe_reads = forbidden
            @property
            def load(self):
                return forbidden()
        class DisguisedSecurity(security_type):
            can_observe_definition_reads = forbidden
            @property
            def definitions(self):
                return forbidden()
        cases = (
            ('foreign Definition owner', f.skills, 'definitions', object.__new__(DisguisedDefinitions)),
            ('foreign Definition store', definitions, 'store', object.__new__(DisguisedStore)),
            ('foreign Security owner', f.skills, 'security', object.__new__(DisguisedSecurity)),
            ('custom Definition check', definitions, 'can_observe_reads', forbidden),
            ('custom Definition accessor', definitions, 'get_record', forbidden),
            ('custom Definition store check', definitions.store, 'can_observe_reads', forbidden),
        )
        before = assignment.model_dump(mode='json')
        for label, owner, name, replacement in cases:
            with self.subTest(owner=label), patch.object(owner, name, replacement), \
                    patch.object(f.sqlite, 'get', side_effect=forbidden) as reads:
                self.assertFalse(f.runner._can_observe_post_input_metadata(assignment, step, procedure))
                reads.assert_not_called()
            self.assertEqual(touched, [])
        # An exact stock instance with a replaced public check is also custom.
        security = object.__new__(security_type)
        security.can_observe_definition_reads = forbidden
        with patch.object(f.skills, 'security', security), \
                patch.object(f.sqlite, 'get', side_effect=forbidden) as reads:
            self.assertFalse(f.runner._can_observe_post_input_metadata(assignment, step, procedure))
            reads.assert_not_called()
        self.assertEqual(touched, [])
        self.assertEqual(assignment.model_dump(mode='json'), before)
        self.assertIsNone(telemetry._scope.get())
