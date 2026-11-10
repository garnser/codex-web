"""Private canonical stores and positive START capture; no live provider/native calls.

Historical publication rows are controlled typed fixtures, NOT a new publication
qualification. The native adapter/current policy/transaction are tested separately.
"""
from copy import deepcopy
from pathlib import Path
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.agent_profiles import AgentProfileUpdate
from codex_web.artifact_evidence import ArtifactCreate, Artifact, Evidence, sha256_digest
from codex_web.action_intents import (ActionIntent, ActionIntentRetryPolicy, ActionDecisionSnapshot,
    ActionIntentReceipt, ActionIntentVerificationReceipt)
from codex_web.action_providers import ActionResult, ActionVerification, ActionRequest, ActionDefinition
from codex_web.security import ExecutionSecurityPolicy, SecurityTrustDecision
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_role_models import (
    ExecutionRoleCatalogDefinition, MRReviewQualificationPolicy,
    validate_execution_role_catalog, validate_execution_role_catalog_v11,
)
from codex_web.execution_contract_seed import execution_role_catalog_seed_payload
from codex_web.models import QueuedTurn, WorkItemHandoff
from codex_web.mr_assignment_review import (
    METHOD, RECEIPT_CONTRACT, AssignmentReviewRequest, AssignmentMRReviewManifest, AssignmentMRReviewReceipt,
    AssignmentReviewError, ReviewAssignmentProvenance, check_assignment_acceptance,
    digest, qualification, publication_proof, start_provenance,
)
from codex_web.mr_closure_acceptance import MRClosureScope, check_independent_acceptance, MRClosureAcceptanceError
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.queued_native_start import QueuedNativeStartCapture
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from tests import test_worker_mr_verifications as fixtures


class MRReviewPolicyTests(unittest.TestCase):
    def test_legacy_payload_roundtrips_without_optin_or_normalization_delta(self):
        raw = execution_role_catalog_seed_payload()
        self.assertNotIn('mr_review_policy',raw)
        self.assertNotIn('mr_ci_readiness_policy',raw)
        self.assertEqual(ExecutionRoleCatalogDefinition.model_validate(raw).model_dump(mode='json'),raw)
        self.assertEqual(validate_execution_role_catalog(raw), raw)
        self.assertIsNone(ExecutionRoleCatalogDefinition.model_validate(raw).mr_review_policy)

    def test_assignment_basis_requires_new_schema_explicit_disjoint_active_roles(self):
        raw = execution_role_catalog_seed_payload()
        raw['mr_review_policy'] = {'schema_version': '1.0', 'basis': 'assignment_role',
            'implementation_role_ids': ['james'], 'validation_role_ids': ['quinn']}
        with self.assertRaises(ValueError):
            validate_execution_role_catalog(raw)
        self.assertEqual(validate_execution_role_catalog_v11(raw)['mr_review_policy'], raw['mr_review_policy'])
        for policy in ({'implementation_role_ids': ['quinn']}, {'validation_role_ids': []},
                       {'validation_role_ids': ['missing']}):
            changed = deepcopy(raw)
            changed['mr_review_policy'].update(policy)
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate_execution_role_catalog_v11(changed)

    def test_ci_requirement_absence_has_no_native_or_external_fallback(self):
        raw=execution_role_catalog_seed_payload()
        self.assertNotIn('mr_ci_readiness_policy',validate_execution_role_catalog(raw))
        self.assertNotIn('mr_ci_readiness_policy',validate_execution_role_catalog_v11(raw))
        self.assertIsNone(ExecutionRoleCatalogDefinition.model_validate(raw).mr_ci_readiness_policy)

    def test_ci_requirement_is_explicit_versioned_closed_target_not_authority(self):
        for target in ('canonical-native-assignment','external-ci-job'):
            raw=execution_role_catalog_seed_payload()
            raw['mr_ci_readiness_policy']={'schema_version':'1.0','execution_target_kind':target}
            with self.assertRaises(ValueError):
                validate_execution_role_catalog(raw)
            self.assertEqual(validate_execution_role_catalog_v11(raw)['mr_ci_readiness_policy'],raw['mr_ci_readiness_policy'])
        raw['mr_ci_readiness_policy']['execution_target_kind']='invented-host'
        with self.assertRaises(ValueError):
            validate_execution_role_catalog_v11(raw)

    def routing_fixture(self):
        from tests import test_owner_routing as routing_fixtures
        env=routing_fixtures.OwnerRoutingTests()
        env.addCleanup=self.addCleanup
        env.setUp()
        return env

    def publish_routing11(self, env, previous):
        draft=env.registry.create_draft(DefinitionDraftCreate(definition_id='execution-roles.default',
            kind='execution-role-catalog',definition_schema_version='1.1',payload=previous.payload,
            scope_type='project',scope_id='project-a',actor='fixture'))
        env.registry.approve_publication(draft.record_id,actor='fixture-reviewer',reference='PRIVATE-ROUTING11',
            reason='Private compatible metadata-only schema')
        return env.registry.publish(draft.record_id,DefinitionPublishRequest(actor='fixture',
            expected_active_revision=previous.revision))

    def test_new_role_schema_keeps_same_canonical_owner_alias_semantics(self):
        env=self.routing_fixture()
        old=env.publish()
        record=self.publish_routing11(env,old)
        binding=env.snapshot().binding('operator')
        self.assertEqual(binding.canonical_owner,'orchestrator')
        self.assertEqual(binding.definition.definition_schema_version,'1.1')
        self.assertEqual(binding.definition.record_id,record.record_id)
        self.assertEqual(env.snapshot().owner('quinn'),'quinn')

    def test_owner_binding_still_refuses_unsupported_catalog_schema(self):
        from codex_web.owner_routing import OwnerRoutingBinding
        env=self.routing_fixture()
        env.publish()
        binding=env.snapshot().binding('operator')
        bad=binding.model_dump(mode='json')
        bad['definition']['definition_schema_version']='1.2'
        with self.assertRaises(ValueError):
            OwnerRoutingBinding.model_validate(bad)

    def test_prior_owner_binding_reference_is_immutable_after_new_schema_publication(self):
        env=self.routing_fixture()
        previous=env.publish()
        binding=env.snapshot().binding('operator')
        before=binding.model_dump(mode='json')
        newer=self.publish_routing11(env,previous)
        self.assertEqual(binding.model_dump(mode='json'),before)
        self.assertEqual(binding.definition.definition_schema_version,'1.0')
        self.assertNotEqual(env.snapshot().binding('operator').definition.record_id,binding.definition.record_id)
        self.assertEqual(env.snapshot().definition.record_id,newer.record_id)


class AssignmentMRReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Borrow setup only, never inherit unrelated test collection.
        self.e = e = fixtures.WorkerMRVerificationTests()
        e.addCleanup = self.addCleanup
        e.addAsyncCleanup = self.addAsyncCleanup
        await e.asyncSetUp()
        # The borrowed fixture starts with an issue; this fixture owns a real
        # keyed MR Work row and its exact workspace/assignment association.
        e.assignment=e.workers.assignment_for_execution(e.assignment.execution_id,actor=e.admin)
        original_ref=e.ref
        e.ref='group/app!7'
        mr=e.items.get(original_ref).model_copy(update={'ref':e.ref,'kind':'merge_request'})
        e.items.put(e.ref,mr)
        e.states[e.ref]=mr
        # Borrowed transport fixture has a read-only state-machine shim. Install
        # the real canonical Work owner for actual ACK/event/state transitions.
        from codex_web.services.work_item_state import WorkItemStateMachine
        from codex_web.services.work_item_dependencies import WorkItemRuntimeDependencies
        host=SimpleNamespace(DATA_DIR=Path(e.temp.name),WORK_ITEM_EVENTS_FILE=Path(e.temp.name)/'review-work-events.jsonl',
            _load_work_item_states=e.items.load,_save_work_item_states=e.items.save,
            _get_work_item_state_record=e.items.get,_put_work_item_state_record=lambda state:e.items.put(state.ref,state),
            _load_projects=lambda:[SimpleNamespace(id='project-a',organization_id='local',workspace_id='default')],
            _resource_ids_for_project=lambda *args,**kwargs:[e.target.id])
        e.work_items.state_machine=WorkItemStateMachine(store=e.sqlite,
            dependencies=WorkItemRuntimeDependencies.from_host(host))
        e.assignment=e.assignment.model_copy(update={'work_item_ref':e.ref,
            'subject':e.assignment.subject.model_copy(update={'ref':e.ref})})
        e.workers.store.update_assignment(e.worker.id,e.assignment.id,
            lambda state:state.model_copy(update={'assignments':[e.assignment]}))
        workspace_store=e.workspaces.store
        e.sqlite.record_update(workspace_store.workspace_namespace,e.assignment.execution_workspace_id,
            lambda raw:{**raw,'work_item_ref':e.ref,'subject':e.assignment.subject.model_dump(mode='json')},default=None)
        lease=e.workspaces.store.lease(e.workspaces.store.workspace(e.assignment.execution_workspace_id).lease_id)
        e.sqlite.record_update(workspace_store.lease_namespace,lease.id,
            lambda raw:{**raw,'work_item_ref':e.ref,'subject':e.assignment.subject.model_dump(mode='json')},default=None)
        self.now = time.time()
        self.scope = MRClosureScope(organization_id='local', workspace_id='default', project_id='project-a',
            work_item_ref=e.ref, repository_resource_id=e.target.id,
            provider_binding_id=next(b.id for b in e.intents.execution.registry.list_bindings(e.worker_actor)
                if b.provider_type == 'github'), pull_request_number=7,
            head_sha=e.server.head, base_sha=e.server.base)
        self.raw_catalog = execution_role_catalog_seed_payload()
        self.raw_catalog['mr_review_policy'] = dict(schema_version='1.0', basis='assignment_role',
            implementation_role_ids=['james'], validation_role_ids=['quinn'])
        self.catalog = ExecutionRoleCatalogDefinition.model_validate(self.raw_catalog)
        draft=e.registry.create_draft(DefinitionDraftCreate(definition_id='execution-roles.default',
            kind='execution-role-catalog',definition_schema_version='1.1',payload=self.raw_catalog,actor='fixture'))
        e.registry.approve_publication(draft.record_id,actor='private-reviewer',reference='private-policy-review',
            reason='Private semantic review policy only')
        e.registry.publish(draft.record_id,DefinitionPublishRequest(actor='fixture'))
        self.policy_ref = e.roles.reference()
        ref = e.assignment.execution_profile_definition
        def provenance(ident, role):
            return ReviewAssignmentProvenance(assignment_id=ident, execution_id=ident+'-execution',
                execution_workspace_id=ident+'-workspace',
                fence_at_start=1, worker_id=e.worker.id, worker_identity_id=e.worker_actor.identity_id,
                profile_id=role, profile_revision=1, profile_record_id=role+'-record', role_id=role,
                execution_profile_definition=ref, preflight_id=ident+'-preflight',
                native_session_id=ident+'-native', native_turn_id=ident+'-turn',
                thread_id=ident+'-native',work_item_ref=e.ref,
                handoff_requested_at=self.now-20 if role=='quinn' else None,
                repository_resource_id=e.target.id,writable_repository_resource_ids=(e.target.id,),
                started_at=self.now-15 if role=='quinn' else self.now-30)
        self.impl = provenance('implementation', 'james')
        self.qa = provenance('validation', 'quinn')
        self.content = AssignmentMRReviewManifest(**self.scope.model_dump(),schema_version='1.0',
            disposition='accepted').model_dump_json().encode()
        self.artifact = Artifact(organization_id='local', workspace_id='default', project_id='project-a',
            work_item_ref=e.ref, resource_ids=(e.target.id,), producer_identity_id=e.worker_actor.identity_id,
            artifact_type='report', name='Private exact review manifest', revision=self.scope.head_sha,
            execution_id=self.qa.execution_id,execution_workspace_id=self.qa.execution_workspace_id,
            governance_record_id='private-governed', digest=sha256_digest(self.content), produced_at=self.now-5)
        self.handoff = WorkItemHandoff(from_agent='orchestrator',to_agent='quinn',
            requested_at=self.now-20,acknowledged_at=self.now-10,status='accepted')
        self.receipt = AssignmentMRReviewReceipt(**self.scope.model_dump(),manifest_artifact_id=self.artifact.id,
            manifest_sha256=self.artifact.digest.value,disposition='accepted',implementation=self.impl,
            validation=self.qa,implementation_intent_id='original',publication_receipt_id='published',
            publication_verification_id='publication-proof',handoff_requested_at=self.handoff.requested_at,
            handoff_acknowledged_at=self.handoff.acknowledged_at,handoff_from_role='orchestrator',
            handoff_to_role='quinn',policy_definition_ref=self.policy_ref,recorded_at=self.now-1)
        self.proof = Evidence(organization_id='local',workspace_id='default',project_id='project-a',
            work_item_ref=e.ref,resource_ids=(e.target.id,),execution_id=self.qa.execution_id,
            producer_identity_id=e.worker_actor.identity_id,artifact_ids=(self.artifact.id,),
            evidence_type='review',result='pass',governance_record_id='private-governed-proof',
            metadata={'receipt_contract':RECEIPT_CONTRACT,'receipt_sha256':digest(self.receipt.model_dump(mode='json'))})
        # Real owner computes false for same producer/verifier; no override exists.
        e.evidence.store.update(lambda s:s.model_copy(update={'artifacts':[*s.artifacts,self.artifact],
            'evidence':[*s.evidence,self.proof]}))
        from codex_web.artifact_evidence import VerificationCreate
        self.verification = e.evidence.create_verification(VerificationCreate(work_item_ref=e.ref,
            execution_id=self.qa.execution_id,artifact_ids=(self.artifact.id,),evidence_ids=(self.proof.id,),
            method=METHOD,result='verified',findings=('mr_assignment_review_receipt_sha256:'+
                digest(self.receipt.model_dump(mode='json')),)),actor=e.worker_actor)

    def check(self, **changes):
        args=dict(scope=self.scope,artifact=self.artifact,verification=self.verification,evidence=self.proof,
            content=self.content,receipt=self.receipt,current_catalog=self.catalog,
            current_catalog_ref=self.policy_ref,now=time.time())
        args.update(changes)
        return check_assignment_acceptance(**args)

    async def test_same_actual_service_principal_role_review_never_becomes_principal_independent(self):
        self.assertFalse(self.verification.independent)
        self.assertEqual(self.check().disposition,'accepted')
        with self.assertRaisesRegex(AssignmentReviewError,'principal_independence_required'):
            self.check(require_principal_independence=True)
        with self.assertRaises(MRClosureAcceptanceError):
            check_independent_acceptance(self.scope,self.artifact,self.verification,self.content,
                implementation_identity_ids=frozenset({self.e.worker_actor.identity_id}),
                eligible_reviewer_identity_ids=frozenset({self.e.worker_actor.identity_id}),now=time.time())

    async def test_explicit_policy_and_exact_definition_pin_are_required(self):
        for catalog in (self.catalog.model_copy(update={'mr_review_policy':None}),
                self.catalog.model_copy(update={'mr_review_policy':MRReviewQualificationPolicy()})):
            with self.subTest(catalog=catalog),self.assertRaises(AssignmentReviewError):
                self.check(current_catalog=catalog)
        with self.assertRaises(AssignmentReviewError):
            self.check(current_catalog_ref=self.policy_ref.model_copy(update={'revision':99}))

    async def test_same_assignment_successor_same_role_and_ineligible_role_refuse(self):
        for changes in ({'assignment_id':self.impl.assignment_id},{'execution_id':self.impl.execution_id},
                {'role_id':'james'},{'role_id':'release-manager'}):
            bad = self.qa.model_copy(update=changes)
            with self.subTest(changes=changes),self.assertRaises(AssignmentReviewError):
                qualification(self.scope,self.artifact,self.content,self.impl,bad,self.handoff,self.catalog,now=time.time())

    async def test_handoff_is_exact_accepted_actual_recipient_not_fake_delivery(self):
        self.assertEqual(qualification(self.scope,self.artifact,self.content,self.impl,self.qa,
            self.handoff,self.catalog,now=time.time()).disposition,'accepted')
        self.assertIsNone(self.handoff.delivery)
        for changes in ({'status':'pending'},{'to_agent':'james'},{'acknowledged_at':None},
                {'requested_at':time.time()+1},{'from_agent':'made-up-role'}):
            with self.subTest(changes=changes),self.assertRaises(AssignmentReviewError):
                qualification(self.scope,self.artifact,self.content,self.impl,self.qa,
                    self.handoff.model_copy(update=changes),self.catalog,now=time.time())

    async def test_scope_manifest_digest_and_receipt_lineage_cannot_be_borrowed(self):
        for key,value in (('head_sha','c'*40),('base_sha','d'*40),('repository_resource_id','foreign'),
                ('provider_binding_id','foreign'),('work_item_ref','foreign/repo!7'),('organization_id','foreign')):
            with self.subTest(key=key),self.assertRaises(AssignmentReviewError):
                self.check(scope=self.scope.model_copy(update={key:value}))
        with self.assertRaises(AssignmentReviewError):
            self.check(content=self.content+b' ')
        for changes in ({'method':'generic-pass'},{'independent':True},{'findings':()},
                {'verifier_identity_id':'imaginary-worker'},{'evidence_ids':('foreign',)}):
            with self.subTest(changes=changes),self.assertRaises(AssignmentReviewError):
                self.check(verification=self.verification.model_copy(update=changes))

    async def test_positive_start_owner_revalidates_canonical_capture_and_historical_fence(self):
        e=self.e
        preflights=ExecutionPreflightService(ExecutionPreflightStore(e.sqlite),clock=lambda:self.now-30)
        assignment=e.assignment
        original=QueuedTurn(id='qa-original',execution_id='qa-logical',thread_id='qa-native',created_at=self.now-31,
            project_id=assignment.project_id,message='Private original',work_item_ref=e.ref,agent_profile_actor_id=assignment.created_by,
            agent_profile_id=assignment.agent_profile.profile_id,agent_profile_revision=assignment.agent_profile.profile_revision,
            repository_resource_id=e.target.id,writable_repository_resource_ids=(e.target.id,),execution_profile_id='repository-write')
        attempt=preflights.retain_queued(original,organization_id='local',workspace_id='default')
        _,claim=preflights.claim_queued_admission(attempt.id)
        preflights.begin_queued_start(attempt.id,claim_id=claim)
        capture=QueuedNativeStartCapture(preflights,attempt.id,claim,'qa-logical','qa-native',assignment.created_by).bind_assignment(
            assignment,worker_id=e.worker.id,fence=assignment.fence,validator=lambda:assignment)
        c=capture.prepare(generation='private-generation',rpc_request_id='private-rpc',native_session_id='qa-native')
        self.assertTrue(capture.observe(c,generation='private-generation',message={'id':'private-rpc','result':{
            'thread':{'id':'qa-native'},'turn':{'id':'private-turn','threadId':'qa-native','status':'completed'}}}))
        saved=preflights.store.get(attempt.id)
        actual=start_provenance(preflights,saved,assignment,e.worker)
        self.assertEqual(actual.worker_identity_id,e.worker_actor.identity_id)
        self.assertEqual(actual.native_turn_id,'private-turn')
        historical=assignment.model_copy(update={'status':'cancelled','lease':None,'fence':assignment.fence+1})
        self.assertEqual(start_provenance(preflights,saved,historical,e.worker),actual)
        for changed in (saved.model_copy(update={'queued_start_evidence':None}),
                saved.model_copy(update={'queued_turn_snapshot':{**saved.queued_turn_snapshot,'message':'forged'}})):
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                start_provenance(preflights,changed,assignment,e.worker)

    def historical_publication(self, impl=None):
        impl=impl or self.impl
        scope=self.scope
        action='code-host.branch.publish'
        intent=ActionIntent(organization_id=scope.organization_id,workspace_id=scope.workspace_id,
            project_id=scope.project_id,work_item_ref=scope.work_item_ref,execution_id=impl.execution_id,
            binding_id=scope.provider_binding_id,provider_type='github',provider_instance='github.example',
            action_id=action,action_definition=ActionDefinition(action_id=action,title=action),
            request=ActionRequest(action_id=action,organization_id=scope.organization_id,workspace_id=scope.workspace_id,
                project_id=scope.project_id,resource_ids=(scope.repository_resource_id,),parameters={
                    'branch':'codex/retained-original','head_revision':scope.head_sha,'execution_workspace_id':'original-workspace'}),
            authority_decision=ActionDecisionSnapshot(),policy_decision=ActionDecisionSnapshot(),
            security_policy=ExecutionSecurityPolicy(),security_decision=SecurityTrustDecision(outcome='allow',
                risk_class='medium',sandbox='workspace-write',network_enabled=False,
                authority_source='controlled-historical-fixture',policy_source='controlled-historical-fixture'),
            resource_ids=(scope.repository_resource_id,),idempotency_key='original-publication',timeout_seconds=30,
            retry_policy=ActionIntentRetryPolicy(),status='succeeded',attempt=1,correlation_id='original-correlation',
            requested_by=self.e.admin.identity_id,created_at=self.now-40,updated_at=self.now-25,completed_at=self.now-25)
        receipt=ActionIntentReceipt(intent_id=intent.id,attempt=1,provider_type=intent.provider_type,
            provider_instance=intent.provider_instance,action_id=action,idempotency_key=intent.idempotency_key,
            correlation_id=intent.correlation_id,received_at=self.now-25,result=ActionResult(
                provider_binding_id=scope.provider_binding_id,action_id=action,status='succeeded',
                started_at=self.now-26,completed_at=self.now-25,external_id=scope.head_sha,
                idempotency_key=intent.idempotency_key,output={'branch':'codex/retained-original',
                    'head_revision':scope.head_sha,'resource_id':scope.repository_resource_id,
                    'execution_workspace_id':'original-workspace'}))
        verification=ActionIntentVerificationReceipt(intent_id=intent.id,verified=True,verified_at=self.now-24,
            provider_verification=ActionVerification(verified=True))
        intent=intent.model_copy(update={'last_receipt_id':receipt.id,'last_verification_id':verification.id})
        return intent,SimpleNamespace(receipts=[receipt],verifications=[verification])

    async def test_only_exact_successful_publication_with_actual_origin_can_supply_provenance(self):
        intent,history=self.historical_publication()
        self.assertEqual(publication_proof(self.scope,intent,history,self.impl,now=time.time())[0],history.receipts[0])
        for changes in ({'status':'unknown'},{'execution_id':'foreign'},{'binding_id':'foreign'},
                {'last_verification_id':None},{'work_item_ref':'foreign/repo!7'}):
            with self.subTest(changes=changes),self.assertRaises(AssignmentReviewError):
                publication_proof(self.scope,intent.model_copy(update=changes),history,self.impl,now=time.time())
        without_key=history.receipts[0].model_copy(update={'result':history.receipts[0].result.model_copy(
            update={'idempotency_key':None})})
        with self.assertRaises(AssignmentReviewError):
            publication_proof(self.scope,intent,SimpleNamespace(receipts=[without_key],
                verifications=history.verifications),self.impl,now=time.time())
        for key,value in (('head_revision','c'*40),('resource_id','foreign'),
                ('execution_workspace_id','foreign'),('branch','foreign')):
            wrong=history.receipts[0].model_copy(update={'result':history.receipts[0].result.model_copy(update={
                'output':{**history.receipts[0].result.output,key:value}})})
            with self.subTest(key=key),self.assertRaises(AssignmentReviewError):
                publication_proof(self.scope,intent,SimpleNamespace(receipts=[wrong],verifications=history.verifications),
                    self.impl,now=time.time())

    def capture_start(self, preflights, assignment, name, when, *, work_ref=None, nonce=None):
        with patch.object(preflights,'clock',lambda:when):
            pin=assignment.agent_profile
            original=QueuedTurn(id=name,execution_id=name+'-logical',thread_id=name+'-native',created_at=when-1,
                project_id=assignment.project_id,message='Private original immutable input',
                work_item_ref=work_ref or self.e.ref,handoff_requested_at=nonce,
                agent_profile_actor_id=assignment.created_by,agent_profile_id=pin.profile_id,
                agent_profile_revision=pin.profile_revision,repository_resource_id=self.e.target.id,
                writable_repository_resource_ids=(self.e.target.id,),execution_profile_id='repository-write')
            attempt=preflights.retain_queued(original,organization_id='local',workspace_id='default')
            _,claim=preflights.claim_queued_admission(attempt.id)
            preflights.begin_queued_start(attempt.id,claim_id=claim)
            capture=QueuedNativeStartCapture(preflights,attempt.id,claim,original.execution_id,
                original.thread_id,assignment.created_by).bind_assignment(assignment,worker_id=self.e.worker.id,
                    fence=assignment.fence,validator=lambda:assignment)
            c=capture.prepare(generation='private-generation',rpc_request_id=name+'-rpc',native_session_id=original.thread_id)
            self.assertTrue(capture.observe(c,generation='private-generation',message={'id':name+'-rpc','result':{
                'thread':{'id':original.thread_id},'turn':{'id':name+'-turn','threadId':original.thread_id,'status':'completed'}}}))
        return preflights.store.get(attempt.id)

    async def prepare_native_owner(self):
        from codex_web.agent_profiles import AgentProfileExecutionBinding
        from codex_web.services.artifact_content import ArtifactContentService,ArtifactContentRegistry
        from codex_web.services.local_artifact_content import LocalArtifactContentStore
        from codex_web.services.worker_verifications import WorkerVerificationAdapter
        from codex_web.services.worker_mr_assignment_reviews import WorkerMRAssignmentReviewAdapter
        from codex_web.models import WorkItemAckCreate
        e=self.e
        old=e.assignment.model_copy(deep=True,update={'id':'historical-implementation','execution_id':'historical-execution',
            'execution_workspace_id':'original-workspace'})
        profile=e.agent_profiles.update('developer',AgentProfileUpdate(role_id='quinn',reason='Private separate QA Role'),actor=e.admin)
        pin=AgentProfileExecutionBinding(profile_id=profile.profile_id,profile_revision=profile.revision,
            profile_record_id=profile.record_id,role_id=profile.role_id,role_definition_ref=profile.role_definition_ref,
            authority_role_id=profile.authority_role_id,authority_definition_ref=profile.authority_definition_ref,
            execution_profile_id=profile.execution_profile_id)
        e.assignment=e.assignment.model_copy(update={'agent_profile':pin})
        def put(s):
            s.assignments=[e.assignment if x.id==e.assignment.id else x for x in s.assignments]+[old]
            return s
        e.workers.store.update(put)
        preflights=ExecutionPreflightService(ExecutionPreflightStore(e.sqlite))
        impl_attempt=self.capture_start(preflights,old,'implementation',self.now-30)
        qa_attempt=self.capture_start(preflights,e.assignment,'validation',self.now-15,nonce=self.now-20)
        impl=start_provenance(preflights,impl_attempt,old,e.worker)
        intent,history=self.historical_publication(impl)
        def seed(s):
            s.intents.append(intent);s.receipts.extend(history.receipts);s.verifications.extend(history.verifications)
            return s
        e.intents.store.update(seed)
        # Actual canonical ACK owner, not a fabricated accepted delivery or model claim.
        state=e.items.get(e.ref).model_copy(update={'current_owner':'orchestrator','current_stage':'ready_for_validation',
            'artifact_state':'merge_request','next_expected_owner':'quinn','handoff':WorkItemHandoff(
                from_agent='orchestrator',to_agent='quinn',requested_at=self.now-20,status='pending')})
        e.items.put(e.ref,state)
        accepted=e.work_items.state_machine._structured_ack(e.ref,WorkItemAckCreate(actor='quinn',accepted=True,
            expected_handoff_requested_at=self.now-20,artifact_state='merge_request'))
        self.assertEqual(accepted.handoff.status,'accepted')
        content_registry=ArtifactContentRegistry()
        content_registry.register(LocalArtifactContentStore(Path(e.temp.name)/'managed-content',backend_id='local'))
        e.evidence.content=ArtifactContentService(content_registry,default_backend_id='local')
        artifact=e.evidence.create_artifact(ArtifactCreate(project_id='project-a',work_item_ref=e.ref,
            execution_id=e.assignment.execution_id,execution_workspace_id=e.assignment.execution_workspace_id,
            resource_ids=(e.target.id,),artifact_type='report',name='Exact review judgment',
            revision=self.scope.head_sha,digest=sha256_digest(self.content)),actor=e.worker_actor)
        artifact=e.evidence.attach_artifact_content(artifact.id,(self.content,),actor=e.worker_actor)
        self.native_owner=WorkerMRAssignmentReviewAdapter(WorkerVerificationAdapter(e.service),
            execution_roles=e.roles,preflights=preflights)
        self.native_request=dict(method=METHOD,work_item_ref=e.ref,manifest_artifact_id=artifact.id,implementation_intent_id=intent.id,
            implementation_assignment_id=old.id,implementation_preflight_id=impl_attempt.id,
            validation_preflight_id=qa_attempt.id,expected_handoff_requested_at=self.now-20,request_id='f'*32)
        self.native_preflights=preflights
        self.native_old=old
        self.native_artifact=artifact
        return e.service._resolve_operation('POST','/api/verifications')

    async def native_create(self, request=None):
        return await self.native_owner.create(self.e.assignment,self.e.worker_actor,self.native_resolved,
            request or self.native_request,lambda:self.e.workers.assignment_for_execution(
                self.e.assignment.execution_id,actor=self.e.admin))

    async def test_native_owner_real_auth_policy_start_ack_and_atomic_append_same_principal(self):
        self.native_resolved=await self.prepare_native_owner()
        result=await self.native_create()
        self.assertIn('item',result,result)
        self.assertIsNotNone(result['item'],result)
        self.assertFalse(result['item']['independent'])
        self.assertEqual(result['receipt']['implementation']['role_id'],'james')
        self.assertEqual(result['receipt']['validation']['role_id'],'quinn')
        self.assertEqual(result['item']['verifier_identity_id'],self.e.worker_actor.identity_id)
        self.assertEqual(result['receipt']['handoff_from_role'],'orchestrator')
        self.assertFalse(result['principal_independence_claimed'])
        before=len(self.e.evidence.store.load().verifications)
        same=await self.native_create()
        self.assertEqual(same['item']['id'],result['item']['id'])
        self.assertEqual(len(self.e.evidence.store.load().verifications),before)

    async def test_native_owner_missing_original_is_typed_inconclusive_not_invented(self):
        self.native_resolved=await self.prepare_native_owner()
        request={**self.native_request,'implementation_assignment_id':'no-historical-assignment'}
        result=await self.native_create(request)
        self.assertEqual(result['outcome'],'inconclusive',result)
        self.assertIsNone(result['item'])

    async def test_native_owner_revoked_profile_and_changed_nonce_cannot_append(self):
        from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
        self.native_resolved=await self.prepare_native_owner()
        before=len(self.e.evidence.store.load().verifications)
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await self.native_create({**self.native_request,'expected_handoff_requested_at':self.now-21})
        self.e.agent_profiles.update('developer',AgentProfileUpdate(description='Revoked old pin',reason='private'),actor=self.e.admin)
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await self.native_create()
        self.assertEqual(len(self.e.evidence.store.load().verifications),before)

    async def test_native_unknown_publication_preserves_original_without_acceptance_or_retry(self):
        self.native_resolved=await self.prepare_native_owner()
        ident=self.native_request['implementation_intent_id']
        self.e.intents.store.update(lambda s:s.model_copy(update={'intents':[
            i.model_copy(update={'status':'uncertain'}) if i.id==ident else i for i in s.intents]}),intent_ids=(ident,))
        before=self.e.intents.history(ident,self.e.admin)
        result=await self.native_create()
        self.assertEqual(result['outcome'],'inconclusive',result)
        self.assertEqual(self.e.intents.history(ident,self.e.admin),before)
        self.assertIsNone(result['item'])

    async def test_native_foreign_start_or_identity_override_cannot_fabricate_review(self):
        from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
        self.native_resolved=await self.prepare_native_owner()
        result=await self.native_create({**self.native_request,
            'implementation_preflight_id':self.native_request['validation_preflight_id']})
        self.assertEqual(result['outcome'],'inconclusive',result)
        for key,value in (('independent',True),('role_id','quinn'),('verifier_identity_id','invented')):
            with self.subTest(key=key),self.assertRaises(ControlPlaneBrokerDeniedError):
                await self.native_create({**self.native_request,key:value})

    async def test_native_work_nonce_race_refuses_atomic_append(self):
        from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
        self.native_resolved=await self.prepare_native_owner()
        e=self.e
        original=e.sqlite.record_mutate_many
        changed=False
        def racing(domains,operation,*args,**kwargs):
            nonlocal changed
            if e.intents.store.records_namespace in domains and not changed:
                changed=True
                work=e.items.get(e.ref)
                e.items.put(e.ref,work.model_copy(update={'handoff':work.handoff.model_copy(
                    update={'requested_at':work.handoff.requested_at+0.01})}))
            return original(domains,operation,*args,**kwargs)
        before=len(e.evidence.store.load().verifications)
        with patch.object(e.sqlite,'record_mutate_many',side_effect=racing):
            with self.assertRaises(ControlPlaneBrokerDeniedError):
                await self.native_create()
        self.assertTrue(changed)
        self.assertEqual(len(e.evidence.store.load().verifications),before)

    async def test_expired_review_evidence_does_not_qualify_retained_manifest(self):
        with self.assertRaises(AssignmentReviewError):
            self.check(evidence=self.proof.model_copy(update={'retention_expires_at':time.time()-1}))

    async def test_managed_content_observation_rereads_canonical_review_records(self):
        self.native_resolved=await self.prepare_native_owner()
        result=await self.native_create()
        self.assertIsNotNone(result.get('item'),result)
        e=self.e
        open_content=e.evidence.open_artifact_content
        def changing(*args,**kwargs):
            pointer,stream=open_content(*args,**kwargs)
            def chunks():
                yield from stream
                def expire(state):
                    state.evidence=[v.model_copy(update={'retention_expires_at':time.time()-1})
                        if v.id==result['evidence']['id'] else v for v in state.evidence]
                    return state
                e.evidence.store.update(expire)
            return pointer,chunks()
        with patch.object(e.evidence,'open_artifact_content',side_effect=changing):
            with self.assertRaisesRegex(AssignmentReviewError,'records_changed_during_content'):
                self.native_owner.validate_canonical_receipt(self.scope,self.native_artifact.id,
                    result['item']['id'],actor=e.worker_actor)

    async def test_old_or_implementation_artifact_cannot_become_qa_assignment_review(self):
        for changes in ({'execution_id':self.impl.execution_id},
                {'execution_workspace_id':self.impl.execution_workspace_id},
                {'producer_identity_id':self.e.admin.identity_id},
                {'created_at':self.qa.started_at-1},{'produced_at':self.qa.started_at-1}):
            with self.subTest(changes=changes),self.assertRaisesRegex(AssignmentReviewError,'validation_artifact_origin'):
                self.check(artifact=self.artifact.model_copy(update=changes))
        self.native_resolved=await self.prepare_native_owner()
        e=self.e
        baseline=self.native_artifact
        before=len(e.evidence.store.load().verifications)
        for changes in ({'execution_id':self.native_old.execution_id},
                {'execution_workspace_id':self.native_old.execution_workspace_id},{'created_at':self.now-100}):
            e.evidence.store.update(lambda s:s.model_copy(update={'artifacts':[
                baseline.model_copy(update=changes) if a.id==baseline.id else a for a in s.artifacts]}))
            result=await self.native_create()
            self.assertEqual(result['outcome'],'inconclusive',result)
            self.assertIn('validation_artifact_origin',result['code'])
        self.assertEqual(len(e.evidence.store.load().verifications),before)

    async def test_validation_start_must_belong_to_exact_work_nonce_and_handoff_time(self):
        self.native_resolved=await self.prepare_native_owner()
        for index,values in enumerate(({'work_ref':'other/repo!9','nonce':self.now-20},
                {'nonce':self.now-21},{'nonce':self.now-20,'when':self.now-25})):
            values=dict(values)
            when=values.pop('when',self.now-15)
            attempt=self.capture_start(self.native_preflights,self.e.assignment,'unrelated-'+str(index),when,**values)
            result=await self.native_create({**self.native_request,'validation_preflight_id':attempt.id})
            self.assertEqual(result['outcome'],'inconclusive',result)
            self.assertIsNone(result['item'])

    async def prepare_broker_review(self, *, bootstrap=False):
        e=self.e
        if bootstrap:
            from codex_web.execution_workers import ExecutionSubject
            from codex_web.resources import RepositoryExecutionScope
            subject=ExecutionSubject(kind='thread_bootstrap',ref='private-qa-bootstrap')
            e.assignment=e.assignment.model_copy(update={'work_item_ref':None,'subject':subject,
                'repository_scope':RepositoryExecutionScope(organization_id='local',workspace_id='default',
                    project_id='project-a',writable_repository_ids=(e.target.id,),
                    source='single_repository',source_ref=e.target.id)})
            e.workers.store.update_assignment(e.worker.id,e.assignment.id,
                lambda state:state.model_copy(update={'assignments':[e.assignment]}))
            store=e.workspaces.store
            workspace=store.workspace(e.assignment.execution_workspace_id)
            for namespace,ident in ((store.workspace_namespace,workspace.id),(store.lease_namespace,workspace.lease_id)):
                e.sqlite.record_update(namespace,ident,lambda raw:{**raw,'work_item_ref':None,
                    'subject':subject.model_dump(mode='json')},default=None)
            e.workspaces.authorize_publication_recovery(workspace.id,work_item_ref=e.ref,
                repository_resource_id=e.target.id,assignment_id=e.assignment.id,assignment_fence=e.assignment.fence,
                source_execution_id=e.assignment.execution_id,source_subject=subject,
                assignment_deadline_at=e.assignment.deadline_at,actor=e.admin)
        self.native_resolved=await self.prepare_native_owner()
        e.service.execution_roles=e.roles
        e.service.execution_preflights=self.native_preflights
        # Use the canonical operational response bound, not the borrowed tiny
        # parser/overflow fixture override; production limits remain unchanged.
        from codex_web.control_plane_broker import ControlPlaneBrokerLimits
        e.service.limits=e.service.limits.model_copy(update={
            'max_response_bytes':ControlPlaneBrokerLimits().max_response_bytes})
        # A fresh native broker binds the exact current Profile/custody snapshot.
        from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
        await e.broker.stop()
        e.broker=AssignmentBoundControlPlaneBroker(e.service,assignment=e.assignment,
            worker_id=e.worker.id,worker_actor=e.worker_actor,fence=e.assignment.fence,
            validator=lambda:e.workers.assignment_for_execution(e.assignment.execution_id,actor=e.admin))
        await e.broker.start()

    async def test_real_broker_catalog_create_and_exact_read_keep_ci_method(self):
        await self.prepare_broker_review()
        e=self.e
        status,_,catalog=await e._request('GET','/api/control-plane-broker/operations')
        import json
        capability=e.service.public_assignment_capability(e.assignment)
        baseline=deepcopy(capability)
        for operation in baseline.get('operations',[]):
            if operation['id']=='verification.create':
                operation['request_body'].pop('MR_assignment_review',None)
                operation['request_body']['methods'].remove(METHOD)
            if operation['id']=='verification.read':
                operation.pop('MR_assignment_review_query',None)
        measurement={'new_catalog_bytes':len(json.dumps(capability,separators=(',',':')).encode()),
            'baseline357_catalog_bytes':len(json.dumps(baseline,separators=(',',':')).encode()),
            'canonical_bound':e.service.limits.max_response_bytes}
        print('MR review catalog byte measurement: '+json.dumps(measurement,sort_keys=True))
        self.assertLessEqual(measurement['new_catalog_bytes'],measurement['canonical_bound'])
        self.assertEqual(status,200,{'response':catalog,**measurement})
        # Inspect public contracts directly as well as the normal bound catalog.
        create=e.service.public_operation(e.service._resolve_operation('POST','/api/verifications').operation)
        from codex_web.services.worker_ci_verifications import CI_METHOD
        self.assertIn(CI_METHOD,create['request_body']['methods'])
        self.assertIn(METHOD,create['request_body']['methods'])
        self.assertIn('work_item_ref',create['request_body']['MR_assignment_review']['schema']['required'])
        status,_,body=await e._request('POST','/api/verifications',payload=self.native_request)
        self.assertEqual(status,200,body)
        self.assertFalse(body['item']['independent'])
        source=self.native_owner.source(e.assignment,AssignmentReviewRequest.model_validate(self.native_request))
        from urllib.parse import urlencode
        status,_,read=await e._request('GET','/api/verifications?'+urlencode(
            {'method':METHOD,'work_item_ref':e.ref,'source':source}))
        self.assertEqual(status,200,read)
        self.assertEqual(read['original']['item']['id'],body['item']['id'])
        self.assertTrue(read['absence_is_not_permission_to_replay'])

    async def test_real_broker_missing_canonical_role_owner_refuses_without_append(self):
        await self.prepare_broker_review()
        self.e.service.execution_roles=None
        before=len(self.e.evidence.store.load().verifications)
        status,_,body=await self.e._request('POST','/api/verifications',payload=self.native_request)
        self.assertEqual(status,403,body)
        self.assertEqual(len(self.e.evidence.store.load().verifications),before)

    async def test_bootstrap_uses_exact_recovery_work_selector_not_assignment_mutation(self):
        await self.prepare_broker_review(bootstrap=True)
        e=self.e
        before=e.workers.store.assignment(e.assignment.id)
        # Current admitted recovery binds the MR; another selector cannot borrow it.
        status,_,denied=await e._request('POST','/api/verifications',
            payload={**self.native_request,'work_item_ref':'foreign/repo!8'})
        self.assertEqual(status,403,denied)
        status,_,body=await e._request('POST','/api/verifications',payload=self.native_request)
        self.assertEqual(status,200,body)
        self.assertFalse(body['item']['independent'])
        self.assertIsNone(e.workers.store.assignment(e.assignment.id).work_item_ref)
        after=e.workers.store.assignment(e.assignment.id)
        self.assertEqual(after.model_dump(exclude={'procedure_discoveries','updated_at'}),
            before.model_dump(exclude={'procedure_discoveries','updated_at'}))
        self.assertEqual(after.procedure_discoveries[:len(before.procedure_discoveries)],before.procedure_discoveries)
        self.assertGreaterEqual(after.updated_at,before.updated_at)
        self.assertEqual(body['receipt']['work_item_ref'],e.ref)

    async def test_nonempty_assignment_work_cannot_be_overridden_by_selector(self):
        from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
        self.native_resolved=await self.prepare_native_owner()
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await self.native_create({**self.native_request,'work_item_ref':'foreign/repo!8'})
