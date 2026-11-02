"""Exact-head provider facts and result boundaries, never live GitLab access."""
from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace as NS

from codex_web.code_hosts import CodeHostError
from codex_web.services.code_host_ci_observation import (
    CI_OBSERVATION_ACTION_ID, CIObservationParameters, PrerequisiteResult,
    parameters, project_observation, unavailable_prerequisites,
)
from codex_web.services.gitlab_code_host import GitLabCodeHostProvider
from tests import test_gitlab_action_provider as existing

HEAD = 'a' * 40


def request():
    return NS(parameters=dict(pull_request_number=57, expected_head_sha=HEAD,
        environment='development', change_classification='shared-surface', work_item_ref='work57',
        target_resource_id='target'), resource_ids=('repo',), requested_by='owner', project_id='project')


class NativeCIReaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.binding = NS(base_url='https://gitlab.example/api/v4')
        self.resource = NS(aliases=[NS(provider='gitlab', namespace='repository', value='group/repo')],
                           provenance=None)
        self.pipeline = dict(id=7, project_id=9, sha=HEAD, source='merge_request_event',
                             ref='feature', status='success')
        self.jobs = [dict(id=11, name='test', status='success', commit={'id':HEAD},
                         pipeline={'id':7, 'project_id':9, 'sha':HEAD})]
        self.calls = []
        self.bridges = []
        self.listed = [dict(id=7, sha=HEAD)]
        async def read(base, path, *, token, params=None):
            self.calls.append((path, params))
            if path == 'projects/group%2Frepo':
                return {'id':9}
            if path == 'projects/9/pipelines':
                return copy.deepcopy(self.listed)
            if path.endswith('/jobs'):
                return copy.deepcopy(self.jobs)
            if path.endswith('/bridges'):
                return copy.deepcopy(self.bridges)
            return copy.deepcopy(self.pipeline)
        self.provider = GitLabCodeHostProvider(NS(read_json_bounded=read))

    async def observe(self):
        return await self.provider.validation_runs(self.binding, self.resource, HEAD, credential='test-only')

    async def test_complete_exact_native_jobs_and_no_claim_of_full_selection(self):
        runs = await self.observe()
        self.assertEqual((runs[0].revision, runs[0].checks[0].external_id), (HEAD, '11'))
        self.assertIsNone(runs[0].selection_full)
        self.assertTrue(all(p is None or p.get('sha',HEAD) == HEAD for _,p in self.calls))

    async def test_wrong_job_project_head_or_pipeline_is_refused(self):
        original = copy.deepcopy(self.jobs[0])
        for key, value in [('project_id',10), ('sha','b'*40), ('id',8), ('id',True), ('project_id',9.0)]:
            with self.subTest(key=key):
                self.jobs = [copy.deepcopy(original)]
                self.jobs[0]['pipeline'][key] = value
                with self.assertRaises(CodeHostError):
                    await self.observe()

    async def test_duplicate_or_truncated_inventory_is_not_success(self):
        self.listed *= 2
        with self.assertRaises(CodeHostError):
            await self.observe()
        self.listed = [dict(id=n, sha=HEAD) for n in range(1,101)]
        with self.assertRaises(CodeHostError):
            await self.observe()

    async def test_downstream_bridges_need_separate_complete_facts(self):
        self.bridges = [{'id':99}]
        with self.assertRaisesRegex(CodeHostError, 'downstream'):
            await self.observe()

    async def test_provider_changes_during_observation_fail_closed(self):
        previous = self.provider.client.read_json_bounded
        reads = 0
        async def changing(base,path,**kw):
            nonlocal reads
            value = await previous(base,path,**kw)
            if path == 'projects/9/pipelines/7':
                reads += 1
                if reads > 1:
                    value['status'] = 'running'
            return value
        self.provider.client.read_json_bounded = changing
        with self.assertRaisesRegex(CodeHostError,'changed'):
            await self.observe()


class CIProjectionTests(unittest.TestCase):
    def setUp(self):
        self.request = request()
        self.raw = dict(observation_version='1.0',provider_instance='gitlab.example',binding_id='binding',
            resource_id='repo', current_pipeline_id='7', requested_by='owner', project_id='project', head_sha=HEAD,
            pull_request_number=57,environment='development',change_classification='shared-surface',
            runs=[dict(external_id='7',revision=HEAD,tested_revision=HEAD,workflow_path='.gitlab-ci.yml',
                state='success',checks=[dict(external_id='11',name='test',revision=HEAD,state='success')])],
            prerequisites=unavailable_prerequisites(self.request).model_dump(mode='json'))

    def project(self):
        return project_observation(self.raw,request=self.request,binding_id='binding',provider_instance='gitlab.example')

    def test_green_ci_without_canonical_receipts_is_needs_input_not_authority(self):
        result = self.project()
        self.assertEqual((result['ci_disposition'],result['readiness']),('passed','needs_input'))
        self.assertFalse(result['privileged_operation_authorized'])

    def test_pending_failed_or_inconsistent_ci_never_ready(self):
        self.raw['prerequisites'] = PrerequisiteResult(disposition='verified',verification_ids=tuple('verification-'+c*32 for c in 'abc'),next_owner_identity_id='owner').model_dump(mode='json')
        for pipeline,job,expected in [('running','running','pending'),('failed','failed','failed'),('success','running','unknown')]:
            with self.subTest(pipeline=pipeline,job=job):
                self.raw['runs'][0]['state'] = pipeline
                self.raw['runs'][0]['checks'][0]['state'] = job
                self.assertEqual(self.project()['ci_disposition'],expected)
                self.assertNotEqual(self.project()['readiness'],'ready_for_validation')

    def test_foreign_receipt_head_scope_and_malformed_job_refuse(self):
        original = copy.deepcopy(self.raw)
        for field,value in [('binding_id','foreign'),('head_sha','b'*40),('requested_by','foreign'),('project_id','foreign')]:
            self.raw = {**copy.deepcopy(original),field:value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.project()
        self.raw = copy.deepcopy(original)
        self.raw['runs'][0]['checks'] *= 2
        with self.assertRaises(ValueError):
            self.project()

    def test_parameters_no_implicit_production_or_caller_approval(self):
        for change in ({'environment':'production'},{'install_authorized':True},{'expected_head_sha':'main'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parameters(NS(parameters={**self.request.parameters,**change}))
        self.assertEqual(CIObservationParameters.model_validate(self.request.parameters).environment,'development')


class CIRegisteredActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = existing.GitLabActionProviderTests(methodName='test_merge_request_requires_exact_head_and_green_pipeline')
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)

    async def test_registered_read_only_ci_requires_current_canonical_metadata_authority(self):
        action = next(a for a in self.fixture.provider.actions() if a.action_id == CI_OBSERVATION_ACTION_ID)
        self.assertTrue(action.capabilities.read)
        self.assertEqual(action.required_authority,('repository.read.metadata',))
        self.assertEqual(action.retry_max_attempts,1)
        self.assertFalse(action.process_access)

    async def test_merge_success_pipeline_missing_or_foreign_head_cannot_mutate(self):
        f = self.fixture
        for sha in (None,'b'*40):
            f.client.merge_request_rows = [dict(id=42,iid=33,state='opened',sha=HEAD,
                draft=False,merge_status='can_be_merged',detailed_merge_status='mergeable',
                head_pipeline=dict(id=9,status='success',**({'sha':sha} if sha else {})))]
            req = f._request(existing.CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
                dict(pull_request_number=33,merge_method='merge',expected_head_sha=HEAD))
            with self.subTest(sha=sha), self.assertRaises(existing.ActionRequirementError):
                await f.execution.execute(f.binding.id,req,actor=f.actor)
            self.assertEqual(f.client.merge_request_merges,0)

    async def test_real_registered_execution_and_adapter_keep_missing_receipts_needs_input(self):
        from codex_web.services.procedure_provider_results import CodeHostProcedureResultAdapter
        f = self.fixture
        async def read(base,path,*,token,params=None):
            self.assertEqual(token,'secret-gitlab-token')
            if '/merge_requests/' in path:
                return dict(iid=57,sha=HEAD,project_id=9,head_pipeline=dict(id=7,sha=HEAD))
            if path == 'projects/group%2Fplatform%2Fcodex-web':
                return dict(id=9)
            if path == 'projects/9/pipelines':
                return [dict(id=7,sha=HEAD)]
            if path.endswith('/bridges'):
                return []
            if path.endswith('/jobs'):
                return [dict(id=11,name='test',status='success',commit=dict(id=HEAD),
                    pipeline=dict(id=7,sha=HEAD,project_id=9))]
            return dict(id=7,project_id=9,sha=HEAD,source='merge_request_event',status='success',ref='feature')
        f.client.read_json_bounded = read
        req = f._request(CI_OBSERVATION_ACTION_ID,request().parameters).model_copy(update={'requested_by':f.actor.identity_id})
        result = await f.execution.execute(f.binding.id,req,actor=f.actor)
        output = CodeHostProcedureResultAdapter(provider_instance='gitlab.example').project(req,result)
        self.assertEqual(output['readiness'],'needs_input')
        self.assertEqual(output['ci_disposition'],'passed')
        self.assertTrue((await f.execution.verify(f.binding.id,result,actor=f.actor)).verified)
        foreign = req.model_copy(update={'idempotency_key':'foreign'})
        with self.assertRaises(ValueError):
            CodeHostProcedureResultAdapter(provider_instance='gitlab.example').project(foreign,result)
        self.assertEqual(f.client.merge_request_merges,0)


class CanonicalPrerequisiteTests(unittest.TestCase):
    """Real private SQLite Artifact/Evidence/Verification admission and identity."""
    def setUp(self):
        import hashlib
        import json
        import time
        from tests import test_artifact_evidence as evidence_fixture
        from codex_web.identity import HumanUserCreate, TenantScope, Membership, PrincipalKind
        from codex_web.resources import ResourceCreate, ResourceType, ResourceOperationalContext
        from codex_web.services.identity import IdentityService
        from codex_web.storage.identity_state import IdentityStateStore
        from codex_web.services.resources import ResourceCatalogService
        from codex_web.storage.resource_catalog import ResourceCatalogStore
        from codex_web.services.code_host_ci_observation import CanonicalCIPrerequisiteValidator
        from codex_web.execution_workers import ExecutionAssignment, ExecutionWorker, AssignmentLease, NetworkPolicy, WorkerResourceLimits
        from codex_web.definitions import DefinitionReference
        from codex_web.storage.execution_workers import ExecutionWorkerStore
        self.fixture = evidence_fixture.ArtifactEvidenceTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        state = self.fixture.artifact_store.store
        self.identity = IdentityService(IdentityStateStore(state))
        producer = self.identity.local_trusted_actor()
        human, _ = self.identity.create_human_user(HumanUserCreate(display_name='Independent CI verifier'),actor=producer)
        verifier = self.identity.actor_for_identity(human.id,scope=TenantScope(organization_id='local',workspace_id='default'))
        self.resources = ResourceCatalogService(ResourceCatalogStore(state))
        repository = self.resources.create(ResourceCreate(resource_type=ResourceType.REPOSITORY,name='fixture-repo'),actor=producer)
        target = self.resources.create(ResourceCreate(resource_type=ResourceType.REPOSITORY,name='isolated fixture target',
            operational_context=ResourceOperationalContext(environment='development')),actor=producer)
        profile = DefinitionReference(definition_id='fixture-profile',kind='execution-role-catalog',revision=1,
            record_id='fixture-record',checksum='a'*64,definition_schema_version='1.0')
        self.assignment = ExecutionAssignment(id='fixture-assignment',organization_id='local',workspace_id='default',
            execution_id='fixture-execution',work_item_ref=self.fixture.work_item.ref,
            subject={'kind':'work_item','ref':self.fixture.work_item.ref},
            project_id='home',resource_ids=(repository.id,),execution_contract_version='1.0',
            required_capabilities=('command_execution',),sandbox='workspace-write',approval_policy='never',network=NetworkPolicy(),
            limits=WorkerResourceLimits(),execution_workspace_id='fixture-workspace',execution_profile_id='fixture-profile',
            execution_profile_definition=profile,assigned_worker_id='fixture-worker',created_by=producer.identity_id,
            status='running',fence=1,lease=AssignmentLease(worker_id='fixture-worker',fence=1,
                lease_token='private-fixture-token-never-live',acquired_at=time.time(),expires_at=time.time()+3600))
        self.worker_store = ExecutionWorkerStore(state)
        service = self.identity.create_service_identity('Isolated CI fixture worker')
        self.identity.add_membership(Membership(identity_id=service.id,principal_kind=PrincipalKind.SERVICE,
            organization_id='local',workspace_id='default'),actor=producer)
        worker = ExecutionWorker(id='fixture-worker',organization_id='local',workspace_id='default',
            service_identity_id=service.id,version='fixture',capabilities=('command_execution',),registered_by=producer.identity_id)
        def admit(current):
            current.workers.append(worker)
            current.assignments.append(self.assignment)
            return current
        self.worker_store.update(admit)
        self.request = NS(parameters={**request().parameters, 'work_item_ref':self.fixture.work_item.ref,
            'target_resource_id':target.id},resource_ids=(repository.id,),requested_by=producer.identity_id,
            organization_id='local',workspace_id='default',project_id='home')
        facts = {
            'dependency':dict(manifest_sha256='1'*64,private_core_provenance_sha256='2'*64,python_abi='cp314',platform='linux_x86_64'),
            'runtime':dict(assignment_id=self.assignment.id,execution_id=self.assignment.execution_id,
                execution_workspace_id=self.assignment.execution_workspace_id,worker_id='fixture-worker',assignment_fence='1',
                profile_reference_sha256=hashlib.sha256(json.dumps(profile.model_dump(mode='json'),sort_keys=True,separators=(',',':')).encode()).hexdigest(),
                runtime_contract='1.0',python_abi='cp314',platform='linux_x86_64'),
            'target':dict(target_identity_sha256='3'*64,ci_service_identity_id='fixture-service',purpose='isolated-ci-fixture',coordination_receipt_sha256='4'*64)}
        self.proofs = []
        for kind,values in facts.items():
            body = dict(schema_version='1.0',kind=kind,repository_resource_id=repository.id,head_sha=HEAD,
                target_resource_id=target.id,environment='development',work_item_ref=self.fixture.work_item.ref,
                expires_at=time.time()+3600,facts=values)
            data = json.dumps(body).encode()
            artifact = self.fixture._artifact(project_id='home',artifact_type='report',resource_ids=(repository.id,),
                execution_id='fixture-execution',execution_workspace_id='fixture-workspace',revision=HEAD,
                digest=evidence_fixture.sha256_digest(data))
            self.fixture.service.attach_artifact_content(artifact.id,(data,),actor=producer,media_type='application/json')
            proof = self.fixture.service.create_evidence(evidence_fixture.EvidenceCreate(project_id='home',
                work_item_ref=self.fixture.work_item.ref,execution_id='fixture-execution',execution_workspace_id='fixture-workspace',
                resource_ids=(repository.id,),evidence_type='test_result',result='pass',artifact_ids=(artifact.id,),
                digest=evidence_fixture.sha256_digest(data)),actor=producer)
            verification = self.fixture.service.create_verification(evidence_fixture.VerificationCreate(
                work_item_ref=self.fixture.work_item.ref,execution_id='fixture-execution',artifact_ids=(artifact.id,),
                evidence_ids=(proof.id,),method='ci-dependency-readiness.v1',result='verified',
                findings=('ci_readiness_manifest_sha256:'+hashlib.sha256(data).hexdigest(),)),actor=verifier)
            self.request.parameters[kind+'_verification_id'] = verification.id
            self.proofs.append((artifact,proof,verification))
        self.validator = CanonicalCIPrerequisiteValidator(self.fixture.service,self.identity,self.resources,
            workers=NS(store=self.worker_store))

    def test_independent_exact_receipts_and_assignment_verify_without_host_grant(self):
        result = self.validator(self.request,None)
        self.assertEqual(result.disposition,'verified')
        self.assertFalse(result.authorizes_mutation)
        self.assertEqual(len(result.verification_ids),3)

    def test_missing_receipt_remains_needs_input_and_foreign_head_refuses(self):
        old = dict(self.request.parameters)
        self.request.parameters['runtime_verification_id'] = None
        self.assertEqual(self.validator(self.request,None).disposition,'needs_input')
        self.request.parameters = {**old,'expected_head_sha':'b'*40}
        with self.assertRaises(ValueError):
            self.validator(self.request,None)

    def test_current_assignment_drift_cannot_borrow_old_runtime_proof(self):
        def change(current):
            current.assignments = [item.model_copy(update={'execution_workspace_id':'foreign'}) for item in current.assignments]
            return current
        self.worker_store.update(change)
        with self.assertRaisesRegex(ValueError,'canonical assignment'):
            self.validator(self.request,None)

    def test_production_target_and_revoked_requester_do_not_reuse_receipts(self):
        from codex_web.resources import ResourceUpdate, ResourceOperationalContext
        target = self.request.parameters['target_resource_id']
        self.resources.update(target,ResourceUpdate(operational_context=ResourceOperationalContext(environment='production')),
            actor=self.identity.local_trusted_actor())
        self.assertEqual(self.validator(self.request,None).disposition,'blocked')
        self.request.requested_by = 'missing-identity'
        from codex_web.services.identity import AuthenticationError
        with self.assertRaises(AuthenticationError):
            self.validator(self.request,None)

    def test_replacing_current_artifact_content_does_not_replace_original_review(self):
        import hashlib
        import json
        from codex_web.artifact_evidence import sha256_digest
        artifact, _, _ = self.proofs[0]
        _, stream = self.fixture.service.open_artifact_content(artifact.id,actor=self.identity.local_trusted_actor())
        body = json.loads(b''.join(stream))
        body['facts']['manifest_sha256'] = 'f'*64
        changed = json.dumps(body).encode()
        # Fault-inject an otherwise consistent managed-content replacement under
        # the same Artifact ID. Ordinary attach is immutable; even a restored or
        # migrated Artifact must not inherit the original review for new bytes.
        from codex_web.artifact_content import ArtifactContentScope
        stored = self.fixture.content_service.put(ArtifactContentScope(organization_id='local',workspace_id='default'),
            (changed,),backend_id='local',media_type='application/json')
        def replace(state):
            state.artifacts = [item.model_copy(update={'digest':sha256_digest(changed),
                'content':item.content.model_copy(update={'locator':stored.locator,'size_bytes':stored.size_bytes})})
                if item.id == artifact.id else item for item in state.artifacts]
            return state
        self.fixture.artifact_store.update(replace)
        current = self.fixture.service.get_artifact(artifact.id,actor=self.identity.local_trusted_actor())
        self.assertEqual(current.digest,sha256_digest(changed))
        self.assertEqual(hashlib.sha256(changed).hexdigest(),current.digest.value)
        with self.assertRaisesRegex(ValueError,'content digest changed'):
            self.validator(self.request,None)

    def test_wrong_profile_and_expired_custody_refuse_same_receipts(self):
        original = self.assignment
        changes = [dict(execution_profile_definition=original.execution_profile_definition.model_copy(update={'revision':2})),
                   dict(lease=original.lease.model_copy(update={'expires_at':1.0})),dict(fence=2),dict(status='cancelled')]
        for change in changes:
            with self.subTest(change=change):
                def replace(state):
                    state.assignments = [original.model_copy(update=change)]
                    return state
                self.worker_store.update(replace)
                with self.assertRaisesRegex(ValueError,'canonical assignment'):
                    self.validator(self.request,None)
