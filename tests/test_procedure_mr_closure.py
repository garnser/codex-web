"""Closure owner composition: real original SQLite/ActionIntent/provider owners.

GitLab transport and current-policy/metadata callback edges are fixtures. The
existing independent acceptance predicate is real; no model/native work runs.
"""
import hashlib
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from codex_web.action_intents import ActionIntentCreate, ActionIntentClaimRequest
from codex_web.artifact_evidence import Artifact, ArtifactDigest, Verification
from codex_web.code_host_merge_requirements import CodeHostMergeRequirementsFact
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.models import WorkItemState, TaskSourceIdentity
from codex_web.mr_closure_acceptance import IndependentMRReviewManifest, REVIEW_METHOD, check_independent_acceptance
from codex_web.procedure_control_plane import CanonicalOperationRegistry
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.code_host_ci_observation import CI_OBSERVATION_ACTION_ID, PrerequisiteResult
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.procedure_mr_closure import CONTRACT, MRClosurePrerequisites, MRClosurePrerequisiteOwner
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.skill_procedures import OperationalProcedure
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.services.execution_role_definitions import install_execution_role_definitions
from codex_web.services.mr_policy_acceptance import CurrentPolicyMRAcceptanceOwner
from tests import test_gitlab_action_provider as gitlab_fixtures


HEAD, BASE = 'a' * 40, 'b' * 40


class MRClosureOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = gitlab_fixtures.GitLabActionProviderTests(methodName='test_merge_request_requires_exact_head_and_green_pipeline')
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)
        f = self.f
        self.stage = 'success'
        self.policy_valid = True
        self.read_hook = None
        self.read_calls = self.acceptance_calls = 0
        self.ref = 'group/platform/codex-web!57'
        self.work = WorkItemState(ref=self.ref, organization_id=f.actor.organization_id,
            workspace_id=f.actor.workspace_id, project_id='project', kind='merge_request',
            project_path='group/platform/codex-web', resource_ids=[f.repository.id],
            source_identity=TaskSourceIdentity(source_type='gitlab',source_instance=f.provider.api_base,external_id=self.ref),
            current_owner='orchestrator', last_meaningful_update_at=time.time(), created_at=time.time(),updated_at=time.time())
        self.write_work()
        self.role_registry=DefinitionRegistryService(DefinitionRegistryStore(f.sqlite))
        self.roles=install_execution_role_definitions(self.role_registry)
        self.ci_policy_owner=CurrentPolicyMRAcceptanceOwner(execution_roles=self.roles,assignment_reviews=None)
        self.publish_ci_policy('canonical-native-assignment')
        self.prerequisites = PrerequisiteResult(disposition='verified',
            verification_ids=tuple('verification-' + x * 32 for x in 'abc'),
            next_owner_identity_id=f.actor.identity_id,
            execution_target_kind='canonical-native-assignment', execution_target_requirement_ref=self.roles.reference())
        f.provider.ci_prerequisite_validator = lambda request, binding: self.prerequisites
        async def read(base, path, *, token, params=None):
            if '/merge_requests/' in path:
                return dict(iid=57, sha=HEAD, project_id=9, head_pipeline=dict(id=7, sha=HEAD))
            if path == 'projects/group%2Fplatform%2Fcodex-web':
                return dict(id=9)
            if path == 'projects/9/pipelines':
                return [dict(id=7, sha=HEAD)]
            if path.endswith('/bridges'):
                return []
            if path.endswith('/jobs'):
                return [dict(id=11, name='test', status=self.stage, commit=dict(id=HEAD),
                    pipeline=dict(id=7, sha=HEAD, project_id=9))]
            return dict(id=7, project_id=9, sha=HEAD, source='merge_request_event', status=self.stage, ref='feature')
        f.client.read_json_bounded = read
        authority = install_authority_roles(DefinitionRegistryService(DefinitionRegistryStore(f.sqlite)), f.resources)
        self.intents = ActionIntentService(ActionIntentStore(f.sqlite), f.execution,
            authority=authority, identity=f.identity,
            work_item_host=NS(_load_work_item_states=lambda:{self.ref:self.work}))
        original = f._request(CI_OBSERVATION_ACTION_ID, dict(pull_request_number=57,
            expected_head_sha=HEAD, environment='development', change_classification='shared-surface',
            work_item_ref=self.ref, target_resource_id='target')).model_copy(update={'project_id':'project'})
        created = self.intents.create(ActionIntentCreate(binding_id=f.binding.id, request=original,
            work_item_ref=self.ref), actor=f.actor)
        worker = AuthenticationActor(identity_id='action-worker', principal_kind=PrincipalKind.SERVICE,
            organization_id=f.actor.organization_id, workspace_id=f.actor.workspace_id,
            assurance=AuthenticationAssurance.SERVICE_TOKEN, service_scopes=('action-intent:worker','secret:use'))
        self.worker=worker
        self.intents.claim(ActionIntentClaimRequest(worker_id='worker', lease_seconds=60), actor=worker, intent_id=created.id)
        self.original = await self.intents.execute_claimed(created.id, 'worker', actor=worker)
        self.assertEqual(self.original.status, 'succeeded', self.original.last_error)
        def current_work(ref):
            return WorkItemState.model_validate(f.sqlite.record_get('work_item_states', ref))
        self.assignment = NS(organization_id=f.actor.organization_id, workspace_id=f.actor.workspace_id,
            project_id='project', work_item_ref=None)
        self.context = NS(assignment=self.assignment, requester_actor=f.actor,
            resource_ids=(f.repository.id,), validate_current=lambda: None)
        self.input = MRClosurePrerequisites(pull_request_number=57, expected_head_sha=HEAD, expected_base_sha=BASE,
            ci_intent_id=self.original.id, ci_receipt_id=self.original.last_receipt_id,
            ci_verification_id=self.original.last_verification_id,
            review_artifact_id='artifact-'+'d'*32, review_verification_id='verification-'+'e'*32)
        self.owner = MRClosurePrerequisiteOwner(self.intents,NS(state_machine=NS(_work_item_state=current_work)),
            acceptance=self.accept, read_requirements=self.requirements)
        self.owner.ci_requirement=self.ci_policy_owner.ci_requirement
        self.review_identity = 'independent-human'
        self.meta_changes = {}

    def publish_ci_policy(self, kind):
        record=self.role_registry.get_record(self.roles.reference().record_id)
        payload=dict(record.payload)
        if kind is None:payload.pop('mr_ci_readiness_policy',None)
        else:payload['mr_ci_readiness_policy']={'schema_version':'1.0','execution_target_kind':kind}
        draft=self.role_registry.create_draft(DefinitionDraftCreate(definition_id=record.definition_id,
            kind=record.kind,definition_schema_version='1.1',scope_type=record.scope_type,
            scope_id=record.scope_id,payload=payload,actor='private-author'))
        self.role_registry.approve_publication(draft.record_id,actor='private-reviewer',
            reference='private-policy-review',reason='Canonical current CI requirement fixture')
        return self.role_registry.publish(draft.record_id,DefinitionPublishRequest(
            actor='private-author',expected_active_revision=record.revision))

    async def observed_external_ci(self):
        self.prerequisites=PrerequisiteResult(disposition='needs_input',
            next_owner_identity_id=self.f.actor.identity_id,execution_target_kind='external-ci-job',
            reasons=('external_ci_runtime_attestation_unavailable',))
        request=self.original.request.model_copy(update={'idempotency_key':None,'correlation_id':None})
        original=await self.run_action(request)
        self.assertEqual(original.status,'succeeded',original.last_error)
        return self.input.model_copy(update={'ci_intent_id':original.id,
            'ci_receipt_id':original.last_receipt_id,'ci_verification_id':original.last_verification_id})

    async def run_action(self, request, *, policy_decision=None):
        created=self.intents.create(ActionIntentCreate(binding_id=self.f.binding.id,request=request,
            work_item_ref=self.ref,**({'policy_decision':policy_decision}if policy_decision is not None else {})),actor=self.f.actor)
        self.intents.claim(ActionIntentClaimRequest(worker_id='worker',lease_seconds=60),
            actor=self.worker,intent_id=created.id)
        return await self.intents.execute_claimed(created.id,'worker',actor=self.worker)

    def write_work(self):
        self.f.sqlite.record_apply('work_item_states', upserts={self.work.ref:self.work.model_dump(mode='json')})

    async def accept(self, scope, artifact_id, verification_id, *, actor):
        self.acceptance_calls += 1
        if not self.policy_valid:
            raise ValueError('current Role opt-in revoked')
        content = IndependentMRReviewManifest(**scope.model_dump(), schema_version='1.0', disposition='accepted').model_dump_json().encode()
        digest = hashlib.sha256(content).hexdigest()
        artifact = Artifact(id=artifact_id, organization_id=scope.organization_id,workspace_id=scope.workspace_id,
            project_id=scope.project_id,work_item_ref=scope.work_item_ref,resource_ids=(scope.repository_resource_id,),
            artifact_type='report',name='Original accepted review',producer_identity_id='implementer',
            revision=HEAD, governance_record_id='governed',digest=ArtifactDigest(value=digest),produced_at=1)
        verification = Verification(id=verification_id,organization_id=scope.organization_id,workspace_id=scope.workspace_id,
            work_item_ref=scope.work_item_ref,artifact_ids=(artifact.id,),verifier_identity_id=self.review_identity,
            independent=self.review_identity!='implementer',method=REVIEW_METHOD,result='verified',verified_at=2,
            findings=('mr_review_manifest_sha256:'+digest,))
        return check_independent_acceptance(scope,artifact,verification,content,
            implementation_identity_ids=frozenset({'implementer'}),eligible_reviewer_identity_ids=frozenset({'independent-human'}),now=time.time())

    async def requirements(self, context, scope):
        self.read_calls += 1
        if self.read_hook:
            self.read_hook()
        values=dict(resource_id=scope.repository_resource_id,binding_id=scope.provider_binding_id,
            provider_type='gitlab',provider_instance=self.f.provider.provider_instance,external_id='57',
            head_revision=HEAD,base_revision=BASE,target_branch='main',observed_at=time.time(),expires_at=time.time()+30,
            pull_request_state='opened',draft=False)
        return CodeHostMergeRequirementsFact(**{**values,**self.meta_changes})

    async def test_original_real_provider_and_private_store_gate_supervisor_without_work_subject(self):
        before = self.intents.store.load(intent_id=self.original.id).model_dump()
        result = await self.owner.execute(self.context, self.input)
        self.assertEqual(result.disposition,'succeeded')
        self.assertEqual(result.outputs,{'expected_head_sha':HEAD,'expected_base_sha':BASE})
        self.assertEqual(self.intents.store.load(intent_id=self.original.id).model_dump(),before)
        self.assertEqual(self.f.client.merge_request_merges,0)
        self.assertFalse(result.facts['privileged_operation_authorized'])

    async def test_green_external_ci_absent_optional_policy_reaches_gate_and_normal_merge_verification(self):
        self.publish_ci_policy(None)
        typed=await self.observed_external_ci()
        before=self.intents.store.load(intent_id=typed.ci_intent_id).model_dump()
        result=await self.owner.execute(self.context,typed)
        self.assertEqual(result.disposition,'succeeded')
        self.assertEqual(result.outputs,{'expected_head_sha':HEAD,'expected_base_sha':BASE})
        self.assertEqual(self.intents.store.load(intent_id=typed.ci_intent_id).model_dump(),before)
        self.assertFalse(result.facts['external_ci_runtime_attested'])
        self.f.client.merge_request_rows.append(dict(iid=57,state='opened',sha=HEAD,
            source_branch='feature',target_branch='main',draft=False,merge_status='can_be_merged',
            detailed_merge_status='mergeable',head_pipeline=dict(id=7,status='success',sha=HEAD)))
        from codex_web.action_intents import ActionDecisionSnapshot
        # Same controlled policy convention as ProcedureCodeHostResultTests;
        # this is not a live policy/approval or native QA qualification.
        policy=ActionDecisionSnapshot(outcome='allow',source='policy:private-procedure-result',
            reason='Controlled canonical fixture permits this exact repository merge')
        merge=await self.run_action(self.f._request('code-host.pull-request.merge',
            {'pull_request_number':57,'merge_method':'merge','expected_head_sha':result.outputs['expected_head_sha']}
            ).model_copy(update={'project_id':'project'}),policy_decision=policy)
        self.assertEqual(merge.status,'succeeded',merge.last_error)
        self.assertEqual(merge.security_decision.outcome,'allow')
        self.assertEqual(merge.policy_decision.source,'policy:private-procedure-result')
        self.assertEqual(merge.requested_by,self.f.actor.identity_id)
        self.assertEqual(merge.resource_ids,(self.f.repository.id,))
        self.assertEqual(merge.request.parameters['expected_head_sha'],HEAD)
        self.assertEqual(merge.request.parameters['pull_request_number'],57)
        receipt=self.intents.store.load(intent_id=merge.id).receipts[-1]
        verified=await self.run_action(self.f._request('code-host.pull-request.verify-merge',
            {k:receipt.result.output[k]for k in ('pull_request_number','head_sha','merge_commit_sha')}
            ).model_copy(update={'project_id':'project'}))
        self.assertEqual(verified.status,'succeeded',verified.last_error)
        self.assertEqual(self.f.client.merge_request_merges,1)

    async def test_absent_optional_policy_never_overrides_hard_actual_target_refusal(self):
        self.publish_ci_policy(None)
        self.prerequisites=PrerequisiteResult(disposition='blocked',
            next_owner_identity_id=self.f.actor.identity_id,reasons=('actual_target_not_development',))
        original=await self.run_action(self.original.request.model_copy(
            update={'idempotency_key':None,'correlation_id':None}))
        self.assertEqual(original.status,'succeeded',original.last_error)
        typed=self.input.model_copy(update={'ci_intent_id':original.id,
            'ci_receipt_id':original.last_receipt_id,'ci_verification_id':original.last_verification_id})
        result=await self.owner.execute(self.context,typed)
        self.assertEqual((result.disposition,result.code),('waiting','mr_closure_ci_not_ready'))
        self.assertEqual(result.facts['readiness'],'blocked')
        self.assertFalse(result.outputs)
        self.assertEqual(self.acceptance_calls,0)
        self.assertEqual(self.f.client.merge_request_merges,0)

    async def test_explicit_external_runtime_policy_stays_waiting_without_attestor(self):
        self.publish_ci_policy('external-ci-job')
        result=await self.owner.execute(self.context,await self.observed_external_ci())
        self.assertEqual((result.disposition,result.code),('waiting','mr_closure_ci_not_ready'))
        self.assertFalse(result.outputs)
        self.assertEqual(self.acceptance_calls,0)

    async def test_missing_ci_policy_owner_is_not_absent_published_policy(self):
        self.owner.ci_requirement=None
        result=await self.owner.execute(self.context,self.input)
        self.assertEqual((result.disposition,result.code),('waiting','mr_closure_canonical_owner_unavailable'))
        self.owner.ci_requirement=self.ci_policy_owner.ci_requirement
        self.ci_policy_owner.roles=None
        with self.assertRaisesRegex(ValueError,'current_ci_policy_owner_unavailable'):
            await self.owner.execute(self.context,self.input)

    async def test_verified_receipts_wrong_current_policy_reference_or_target_still_wait(self):
        # Original native verified receipts cannot satisfy a changed/current external policy.
        self.publish_ci_policy('external-ci-job')
        result=await self.owner.execute(self.context,self.input)
        self.assertEqual((result.disposition,result.code),('waiting','mr_closure_ci_not_ready'))
        self.publish_ci_policy('canonical-native-assignment')
        result=await self.owner.execute(self.context,self.input)
        self.assertEqual((result.disposition,result.code),('waiting','mr_closure_ci_not_ready'))

    async def test_new_stricter_ci_policy_during_io_refuses_absent_policy_gate(self):
        self.publish_ci_policy(None)
        typed=await self.observed_external_ci()
        self.read_hook=lambda:self.publish_ci_policy('external-ci-job')
        with self.assertRaisesRegex(ValueError,'current_ci_policy_changed'):
            await self.owner.execute(self.context,typed)

    async def test_other_requester_foreign_subject_or_resource_refuses(self):
        for update in ({'work_item_ref':'foreign'}, {'project_id':'foreign'}):
            old = vars(self.assignment).copy()
            vars(self.assignment).update(update)
            with self.subTest(update=update),self.assertRaises(ValueError):
                await self.owner.execute(self.context,self.input)
            vars(self.assignment).update(old)
        self.context.requester_actor = self.f.actor.model_copy(update={'identity_id':'another-owner'})
        with self.assertRaises(ValueError):
            await self.owner.execute(self.context,self.input)

    async def test_work_source_or_repository_drift_is_not_a_foreign_receipt(self):
        for change in ({'project_path':'group/foreign'}, {'resource_ids':['foreign']},
                       {'source_identity':self.work.source_identity.model_copy(update={'source_instance':'https://foreign.example'})}):
            previous = self.work
            self.work = previous.model_copy(update=change)
            self.write_work()
            with self.subTest(change=change),self.assertRaises(ValueError):
                await self.owner.execute(self.context,self.input)
            self.work = previous
        self.write_work()

    async def test_replaced_receipt_or_last_verification_refuses_original(self):
        for change in ({'ci_receipt_id':'action-receipt-'+'f'*32}, {'ci_verification_id':'action-verification-'+'f'*32}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                await self.owner.execute(self.context,self.input.model_copy(update=change))

    async def test_positive_observation_does_not_override_current_ci_change(self):
        self.stage='running'
        result = await self.owner.execute(self.context,self.input)
        self.assertEqual((result.disposition,result.code),('waiting','mr_closure_ci_original_no_longer_verified'))
        self.assertFalse(result.outputs)

    async def test_original_successful_read_of_pending_failed_or_unsupported_ci_is_not_ready(self):
        for stage in ('running','failed','success'):
            self.stage=stage
            if stage=='success':
                self.prerequisites=PrerequisiteResult(disposition='needs_input',
                    next_owner_identity_id=self.f.actor.identity_id,execution_target_kind='external-ci-job',
                    reasons=('external_ci_runtime_attestation_unavailable',))
            original_request=self.original.request.model_copy(update={'idempotency_key':None,'correlation_id':None})
            created=self.intents.create(ActionIntentCreate(binding_id=self.f.binding.id,request=original_request,
                work_item_ref=self.ref),actor=self.f.actor)
            self.intents.claim(ActionIntentClaimRequest(worker_id='worker',lease_seconds=60),actor=self.worker,intent_id=created.id)
            current=await self.intents.execute_claimed(created.id,'worker',actor=self.worker)
            self.assertEqual(current.status,'succeeded',current.last_error)
            typed=self.input.model_copy(update={'ci_intent_id':current.id,'ci_receipt_id':current.last_receipt_id,
                'ci_verification_id':current.last_verification_id})
            result=await self.owner.execute(self.context,typed)
            with self.subTest(stage=stage):
                self.assertEqual((result.disposition,result.code),('waiting','mr_closure_ci_not_ready'))
                self.assertFalse(result.outputs)
        self.assertEqual(self.acceptance_calls,0)

    async def test_current_mr_head_base_binding_expiry_and_state_are_not_cached(self):
        for change in ({'head_revision':'c'*40},{'base_revision':'c'*40},{'binding_id':'foreign'},
                       {'draft':True},{'pull_request_state':'merged'},{'changed':True},
                       {'observed_at':time.time()-60,'expires_at':time.time()-30}):
            self.meta_changes=change
            result=await self.owner.execute(self.context,self.input)
            with self.subTest(change=change):
                self.assertEqual(result.disposition,'waiting')
                self.assertFalse(result.outputs)

    async def test_current_role_policy_or_strict_independence_refusal_propagates(self):
        self.policy_valid=False
        with self.assertRaisesRegex(ValueError,'Role'):
            await self.owner.execute(self.context,self.input)
        self.policy_valid=True
        self.review_identity='implementer'
        with self.assertRaises(ValueError):
            await self.owner.execute(self.context,self.input)

    async def test_work_changes_during_read_do_not_qualify_snapshot(self):
        def drift():
            self.work=self.work.model_copy(update={'current_owner':'changed'})
            self.write_work()
        self.read_hook=drift
        with self.assertRaisesRegex(ValueError,'records_changed'):
            await self.owner.execute(self.context,self.input)

    async def test_revocation_after_network_read_is_not_hidden_by_cached_acceptance(self):
        self.read_hook=lambda:setattr(self,'policy_valid',False)
        with self.assertRaisesRegex(ValueError,'Role'):
            await self.owner.execute(self.context,self.input)

    async def test_missing_owners_wait_without_provider_mutation(self):
        self.owner.acceptance=None
        result=await self.owner.execute(self.context,self.input)
        self.assertEqual(result.disposition,'waiting')
        self.assertEqual(self.read_calls,0)

    def test_registration_is_read_only_exact_repo_and_no_original_execution_capability(self):
        registry=CanonicalOperationRegistry()
        registry.register(CONTRACT,self.owner)
        self.assertEqual(CONTRACT.effect,'read')
        self.assertFalse(CONTRACT.allow_original_intent)
        self.assertEqual(CONTRACT.max_attempts,1)
        self.assertEqual(CONTRACT.resource_types,('repository',))

    def test_inactive_template_uses_unique_real_engine_bindings_after_gate_and_merge(self):
        from codex_web.services.procedure_execution import ProcedureExecutionService
        raw=json.loads((Path(__file__).resolve().parents[1]/'examples/policies/mr-closure-prerequisites.INACTIVE.json').read_text())
        self.assertFalse(raw['ready'])
        procedure=OperationalProcedure.model_validate(raw['operational_procedure'])
        registry=CanonicalOperationRegistry()
        registry.register(CONTRACT,self.owner)
        registry.validate_step(procedure,procedure.steps[0])
        service=object.__new__(ProcedureExecutionService)
        gate=NS(status='succeeded',outputs={'expected_head_sha':HEAD,'expected_base_sha':BASE})
        merge=NS(status='pending',outputs={})
        verify=NS(status='pending',outputs={})
        checkpoint=NS(steps={'closure-prerequisites':gate,'merge':merge,'verify-merge':verify})
        service._output_values=lambda assignment,state,actor:state.outputs
        inputs={'pull_request_number':57,'merge_method':'merge','expected_head_sha':HEAD}
        values=service._step_inputs(procedure.steps[1],checkpoint,inputs,self.f.actor,self.assignment)
        self.assertEqual(values['expected_head_sha'],HEAD)
        merge.status='succeeded'
        merge.outputs={'pull_request_number':57,'head_sha':HEAD,'merge_commit_sha':'f'*40}
        values=service._step_inputs(procedure.steps[2],checkpoint,inputs,self.f.actor,self.assignment)
        self.assertEqual(values,merge.outputs)
