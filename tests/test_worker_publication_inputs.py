"""Worker broker composes publication input before its real procedure START.

The provider HTTP and isolated Git-process transport use private fixtures; Work,
workspace/recovery, worker custody, Skill validation, broker, governed append and
procedure owners remain real. The test never creates a publication input Artifact
or uploads its content, and never advances the publication procedure.
"""
import json
import hashlib
import asyncio
from pathlib import Path
import subprocess
import unittest
from urllib.parse import quote
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.execution_subjects import ExecutionSubject
from codex_web.execution_workspace_backend import GitWorkspaceProvision
from codex_web.models import TaskSourceIdentity
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.skills import SkillService
from tests import test_governed_mr_procedure_runs as governed
from tests import test_initial_publication_procedure as publication
from tests.test_execution_workspaces import _FakeBackend


METHOD = 'workspace-initial-publication-input.v1'


def git(path, *args):
    return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', *args],
        cwd=path, text=True, stderr=subprocess.PIPE).strip()


class WorkerPublicationInputTests(governed.GovernedMRProcedureRunTests):
    def _publish_worker_authority(self, **kwargs):
        # The borrowed readiness fixture grants only its narrower policy read.
        # This collector genuinely reads repository identity, the MR and refs;
        # declare those explicit Project-scoped READ grants before Profile pins.
        from codex_web.authority import AUTHORITY_ROLE_CATALOG_ID, AUTHORITY_ROLE_CATALOG_KIND
        from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
        super()._publish_worker_authority(**kwargs)
        active = self.registry.resolve(definition_id=AUTHORITY_ROLE_CATALOG_ID,kind=AUTHORITY_ROLE_CATALOG_KIND)
        payload = active.payload
        role = next(row for row in payload['roles'] if row['id'] == 'orchestration-worker')
        for suffix,capability in (('repository','repository.read'),('refs','repository.refs.read'),
                ('mr','repository.pull-request.read')):
            role['grants'].append({'id':'publication-input-'+suffix,'capability':capability,
                'level':'read','project_ids':['project-a']})
        draft = self.registry.create_draft(DefinitionDraftCreate(definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,definition_schema_version=active.definition_schema_version,
            payload=payload,actor='fixture'))
        self.registry.approve_publication(draft.record_id,actor='fixture-reviewer',reference='PRIVATE-READ-APPROVAL',
            reason='Private existing-MR publication input scoped read boundary')
        self.registry.publish(draft.record_id,DefinitionPublishRequest(actor='fixture',expected_active_revision=active.revision))

    def respond(self, request):
        hook = getattr(self,'publication_read_hook',None)
        if hook is not None and '/merge_requests/' in request.url.path:
            self.publication_read_hook = None
            hook()
        if '/repository/branches/' in request.url.path and hasattr(self,'remote_head'):
            import httpx
            self.observed_calls.append(request.url.path)
            return httpx.Response(200,json={'name':self.mr['source_branch'],
                'commit':{'id':self.remote_head},'protected':False,'default':False})
        return super().respond(request)

    def verification_resources(self, root):
        # Called by the borrowed setup before canonical Work/workspace creation.
        self.ref = 'group/private!33'
        return super().verification_resources(root)

    async def asyncSetUp(self):
        original_create = SkillService.create
        original_acquire = ExecutionWorkspaceService.acquire
        original_assignment = ExecutionWorkerService.create_assignment

        def create(skills, payload, *, actor, **kwargs):
            if payload.skill_id == 'private-governed-readiness':
                procedure = publication.initial_contract(self.target.id, actor.identity_id, 'gitlab.example')
                procedure['environments'] = ['staging']
                if getattr(self,'publication_schema','six') == 'six':
                    number = publication.field('existing_change_request_number', 'integer')
                    procedure['inputs'].append(number)
                    procedure['steps'][0]['inputs'].append(number)
                payload = payload.model_copy(update={'skill_id':'private-publication-input',
                    'name':'Private existing-MR publication',
                    'instructions':'Collect current governed input, then start its published procedure. No publication in this fixture.',
                    'operational_procedure':procedure})
            return original_create(skills, payload, actor=actor, **kwargs)

        def acquire(service, request, *, actor, **kwargs):
            if request.execution_id == 'verification-execution':
                request = request.model_copy(update={'work_item_ref':None,
                    'subject':ExecutionSubject(kind='thread_bootstrap', ref='publication-fixture-bootstrap')})
            return original_acquire(service, request, actor=actor, **kwargs)

        def assignment(service, request, *, actor, **kwargs):
            if request.execution_id == 'verification-execution':
                workspace = service.workspaces.get(request.execution_workspace_id, actor)
                request = request.model_copy(update={'subject':workspace.subject,'work_item_ref':None})
            return original_assignment(service, request, actor=actor, **kwargs)

        def provision(backend, repository_path, workspace_id, branch_name, base_revision):
            # Replace only the private fixture's fake physical provisioning.
            path = backend.root / workspace_id
            path.mkdir(parents=True, exist_ok=False)
            git(path, 'init', '--initial-branch='+branch_name)
            git(path, 'config', 'user.email', 'private-fixture@example.invalid')
            git(path, 'config', 'user.name', 'Private fixture')
            (path / 'reviewed.txt').write_text('base\n')
            git(path, 'add', 'reviewed.txt'); git(path, 'commit', '-m', 'Private base')
            head = git(path, 'rev-parse', 'HEAD')
            backend.provisioned.append((workspace_id, branch_name, head))
            return GitWorkspaceProvision(path=path, branch_name=branch_name, base_revision=head, head_revision=head)

        with patch.object(SkillService, 'create', new=create), \
                patch.object(ExecutionWorkspaceService, 'acquire', new=acquire), \
                patch.object(ExecutionWorkerService, 'create_assignment', new=assignment), \
                patch.object(_FakeBackend, 'provision_git', new=provision):
            await super().asyncSetUp()

        current = self.workers.store.assignment(self.assignment.id)
        workspace = self.workspaces.get(current.execution_workspace_id, self.admin)
        self.workspaces.authorize_publication_recovery(workspace.id, work_item_ref=self.ref,
            repository_resource_id=self.target.id, assignment_id=current.id,
            assignment_fence=current.fence, source_execution_id=current.execution_id,
            source_subject=current.subject, assignment_deadline_at=current.deadline_at, actor=self.admin)
        workspace = self.workspaces.get(workspace.id, self.admin)
        self.assertEqual(current.subject.kind, 'thread_bootstrap')
        self.assertIsNone(current.work_item_ref)
        self.assertIsNone(workspace.work_item_ref)
        self.assertEqual(workspace.publication_recovery.work_item_ref, self.ref)
        self.assertEqual(workspace.publication_recovery.assignment_id, current.id)
        self.assertEqual(workspace.publication_recovery.assignment_fence, current.fence)
        member = next(row for row in workspace.repository_members if row.resource_id == self.target.id)
        self.checkout = Path(member.workspace_path)
        self.remote_head = git(self.checkout, 'rev-parse', 'HEAD')
        (self.checkout / 'reviewed.txt').write_text('necessary private correction\n')
        git(self.checkout, 'add', 'reviewed.txt'); git(self.checkout, 'commit', '-m', 'Private correction')
        self.local_head = git(self.checkout, 'rev-parse', 'HEAD')
        self.mr.update(source_branch=member.branch_name, sha=self.remote_head,
            source_project_id=42, target_project_id=42)
        self.mr['head_pipeline']['sha'] = self.remote_head
        work = self.items.get(self.ref).model_copy(update={'kind':'merge_request',
            'project_path':'group/private','source_identity':TaskSourceIdentity(
                source_type='gitlab', source_instance='https://gitlab.example/api/v4', external_id=self.ref)})
        self.items.put(self.ref, work)

        def inspect(assignment, *, workspace_path, source_path):
            self.assertEqual((assignment.id, assignment.fence), (current.id, current.fence))
            self.assertEqual(Path(workspace_path), self.checkout)
            return {'head':git(self.checkout, 'rev-parse', 'HEAD'),
                'branch':git(self.checkout, 'symbolic-ref', '--short', 'HEAD'),
                'dirty':bool(git(self.checkout, 'status', '--porcelain'))}
        # Keep the actual runtime's live-assignment checks; control only its
        # isolated Git-process backend, never collector/canonical fact results.
        runtime = self.broker.validator.__self__.local_worker
        runtime.backend = SimpleNamespace(inspect_git_metadata=inspect)
        self.service.local_execution_worker = runtime
        self.collect = {'method':METHOD,'definition':self.pin.model_dump(mode='json'),
            'repository_resource_id':self.target.id,'expected_assignment_id':current.id,
            'expected_fence':current.fence,'expected_head_sha':self.local_head,'request_id':'d'*32}

    async def test_real_worker_collects_governed_publication_input_then_starts_procedure(self):
        before = self.evidence.store.load()
        status, _, result = await self._request('POST','/api/verifications',payload=self.collect)
        self.assertEqual(status, 200, result)
        artifact = result['procedure_input']
        values = json.loads(self.content(artifact['id']))
        self.assertEqual(values, {'execution_workspace_id':self.assignment.execution_workspace_id,
            'branch':self.mr['source_branch'],'head_revision':self.local_head,
            'expected_remote_revision':self.remote_head,'existing_change_request_number':33,
            'pull_request_number':33})
        self.assertEqual(artifact['producer_identity_id'], self.worker_actor.identity_id)
        self.assertIsNotNone(artifact['governance_record_id'])
        self.assertEqual(result['evidence']['result'], 'info')
        self.assertTrue(result['source_information_only'])
        self.assertEqual(len(self.evidence.store.load().verifications), len(before.verifications))
        started = await self.start_snapshot(artifact['id'])
        self.assertEqual(started.procedure_checkpoint.input_snapshot_id, artifact['id'])
        self.assertEqual(started.procedure_checkpoint.definition, self.pin)
        self.assertFalse(any(intent.action_id == publication.PUBLISH for intent in self.intents.store.load().intents))
        self.runner.models.invoke.assert_not_awaited()

    async def test_original_input_response_loss_lookup_and_exact_repeat_do_not_read_again(self):
        # Concurrent deliveries of the SAME request must compose one input.
        responses = await asyncio.gather(*(self._request('POST','/api/verifications',payload=self.collect) for _ in range(2)))
        status,_,first = responses[0]
        self.assertEqual(status,200,first)
        self.assertEqual(responses[1][0],200,responses[1][2])
        self.assertEqual(responses[1][2]['procedure_input']['id'],first['procedure_input']['id'])
        observed = list(self.observed_calls)
        # The first response is treated as lost. Derive the advertised identity
        # using ONLY the original immutable request, never its response fields.
        source = 'sha256:'+hashlib.sha256(json.dumps({'assignment_id':self.collect['expected_assignment_id'],
            'fence':self.collect['expected_fence'],'method':METHOD,'request_id':self.collect['request_id']},
            sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        with patch.object(self.service.local_execution_worker,'inspect_git_metadata',side_effect=AssertionError('no second Git read')):
            status,_,lookup = await self._request('GET','/api/verifications?method='+METHOD+'&source='+quote(source,safe=''))
            self.assertEqual(status,200,lookup)
            self.assertEqual(lookup['outcome'],'unique')
            self.assertEqual(lookup['original']['procedure_input']['id'],first['procedure_input']['id'])
            status,_,repeat = await self._request('POST','/api/verifications',payload=self.collect)
            self.assertEqual(status,200,repeat)
            self.assertEqual(repeat['procedure_input']['id'],first['procedure_input']['id'])
        self.assertEqual(self.observed_calls,observed)

    async def test_descriptor_advertises_actual_fixed_input_schema(self):
        status,_,catalog = await self._request('GET','/api/control-plane-broker/operations')
        self.assertEqual(status,200,catalog)
        operation = next(row for row in catalog['operations'] if row['id'] == 'verification.create')
        descriptor = operation['request_body']['publication_input']
        self.assertEqual(descriptor['method'],METHOD)
        self.assertIn('expected_assignment_id',descriptor['schema']['properties'])
        self.assertIn('expected_fence',descriptor['schema']['properties'])
        self.assertFalse(descriptor['schema']['additionalProperties'])
        lookup = next(row for row in catalog['operations'] if row['id'] == 'verification.read')['publication_input_query']
        self.assertEqual(lookup['required'],['method','source'])
        self.assertEqual(lookup['forbidden'],['work_item_ref'])

    async def test_origin_assertions_and_caller_fact_overrides_refuse_without_output(self):
        before = self.evidence.store.load()
        calls = list(self.observed_calls)
        for update in ({'expected_assignment_id':'assignment-other'}, {'expected_fence':self.collect['expected_fence']+1},
                {'expected_fence':True}, {'producer_identity_id':self.admin.identity_id},
                {'values':{'head_revision':self.local_head}}, {'repository_resource_id':'foreign'}):
            with self.subTest(fields=list(update)):
                status,_,body = await self._request('POST','/api/verifications',payload={**self.collect,**update})
                self.assertIn(status,(403,409),body)
                self.assertEqual(self.evidence.store.load(),before)
        self.assertEqual(self.observed_calls,calls)

    async def test_dirty_head_fork_and_provider_branch_drift_refuse_without_output(self):
        before = self.evidence.store.load()
        path = self.checkout / 'reviewed.txt'
        original = path.read_bytes()
        path.write_text('uncommitted change\n')
        status,_,body = await self._request('POST','/api/verifications',payload=self.collect)
        self.assertEqual(status,403,body)
        path.write_bytes(original)
        status,_,body = await self._request('POST','/api/verifications',payload={**self.collect,'expected_head_sha':self.remote_head})
        self.assertEqual(status,403,body)
        self.mr['source_project_id'] = 999
        status,_,body = await self._request('POST','/api/verifications',payload=self.collect)
        self.assertEqual(status,409,body)
        self.mr['source_project_id'] = 42
        self.mr['sha'] = 'e'*40  # Actual branch read still reports remote_head.
        status,_,body = await self._request('POST','/api/verifications',payload=self.collect)
        self.assertEqual(status,409,body)
        self.assertEqual(self.evidence.store.load(),before)

    async def test_foreign_work_source_instance_is_not_selected_by_matching_path_and_iid(self):
        before = self.evidence.store.load()
        work = self.items.get(self.ref)
        self.items.put(self.ref,work.model_copy(update={'source_identity':work.source_identity.model_copy(
            update={'source_instance':'https://another-gitlab.example/api/v4'})}))
        status,_,body = await self._request('POST','/api/verifications',payload=self.collect)
        self.assertEqual(status,403,body)
        self.assertEqual(self.evidence.store.load(),before)

    async def test_original_resource_context_requester_needs_own_refs_read_authority(self):
        from codex_web.identity import HumanUserCreate
        from codex_web.resource_context import ReadOnlyResourceContext
        from codex_web.authority import AUTHORITY_ROLE_CATALOG_ID, AUTHORITY_ROLE_CATALOG_KIND
        from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
        from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
        human,_ = self.identity.create_human_user(HumanUserCreate(id='publication-context-requester',
            display_name='Publication original requester',roles=['admin']),actor=self.admin)
        active = self.registry.resolve(definition_id=AUTHORITY_ROLE_CATALOG_ID,kind=AUTHORITY_ROLE_CATALOG_KIND)
        payload = active.payload
        payload['roles'].append({'id':'publication-context','name':'Private original context requester',
            'description':'Can collect and read repository/MR identity, deliberately cannot read refs',
            'grants':[{'id':suffix,'capability':capability,'level':level,'project_ids':['project-a']}
                for suffix,capability,level in (('collect','api.verifications.write','execute'),
                    ('repository','repository.read','read'),('mr','repository.pull-request.read','read'))]})
        payload['bindings'].append({'id':'publication-context-binding','role_id':'publication-context',
            'subject_kind':'identity','subject_id':human.id,'organization_id':self.admin.organization_id,
            'workspace_id':self.admin.workspace_id,'project_ids':['project-a']})
        draft = self.registry.create_draft(DefinitionDraftCreate(definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,definition_schema_version=active.definition_schema_version,
            payload=payload,actor='private-fixture'))
        self.registry.approve_publication(draft.record_id,actor='private-reviewer',reference='PRIVATE-CONTEXT',reason='Exact scoped requester negative')
        self.registry.publish(draft.record_id,DefinitionPublishRequest(actor='private-fixture',expected_active_revision=active.revision))
        # Administrative membership makes this private Profile visible. It is
        # separate from canonical Authority Role grants: only the trusted
        # default identity has local-admin wildcard authority in the seed.
        requester = self.identity.actor_for_identity(human.id,scope=self.admin.tenant)
        self.agent_profiles.get(self.assignment.agent_profile.profile_id,actor=requester)
        from codex_web.authority import AuthorityEvaluationRequest
        collect = self.authority.evaluate(AuthorityEvaluationRequest(capability='api.verifications.write',level='execute',
            project_id='project-a',resource_ids=(self.target.id,)),actor=requester)
        refs = self.authority.evaluate(AuthorityEvaluationRequest(capability='repository.refs.read',level='read',
            project_id='project-a',resource_ids=(self.target.id,)),actor=requester)
        self.assertEqual(collect.outcome,'allow')
        self.assertEqual(refs.outcome,'deny')
        context = ReadOnlyResourceContext(schema_version='1.0',read_only_resource_ids=(self.target.id,),
            requester_identity_id=human.id,work_item_ref=self.ref)
        def pin(state):
            state.assignments[0].resource_context = context
            return state
        self.workers.store.update_assignment(self.worker.id,self.assignment.id,pin)
        # Fresh transport pins the canonical context; do not alter an existing
        # broker incarnation's immutable resource-context envelope.
        await self.broker.stop()
        current = self.workers.store.assignment(self.assignment.id)
        self.broker = AssignmentBoundControlPlaneBroker(self.service,assignment=current,worker_id=self.worker.id,
            worker_actor=self.worker_actor,fence=current.fence,
            validator=lambda:self.workers.assignment_for_execution(current.execution_id,actor=self.admin))
        await self.broker.start()
        before = self.evidence.store.load()
        status,_,body = await self._request('POST','/api/verifications',payload=self.collect)
        self.assertEqual(status,403,body)
        self.assertIn('repository.refs.read',str(body))
        self.assertEqual(self.evidence.store.load(),before)

    async def test_current_work_and_definition_changes_during_read_refuse_commit(self):
        before = self.evidence.store.load()
        original = self.items.get(self.ref)
        self.publication_read_hook = lambda: self.items.put(self.ref,original.model_copy(update={'current_owner':'another-agent'}))
        status,_,body = await self._request('POST','/api/verifications',payload=self.collect)
        self.assertEqual(status,403,body)
        self.assertEqual(self.evidence.store.load(),before)
        self.items.put(self.ref,original)
        self.publication_read_hook = lambda: self.registry.quarantine(self.pin.record_id,
            actor='fixture-security',reason='Private exact publication Definition changed during observation')
        status,_,body = await self._request('POST','/api/verifications',payload=self.collect)
        self.assertIn(status,(403,409),body)
        self.assertEqual(self.evidence.store.load(),before)

    async def test_governance_append_failure_leaves_no_partial_input_or_evidence(self):
        before,governed = self.evidence.store.load(),self.evidence.governance.store.load()
        with patch.object(self.evidence.governance,'register_domain_record',side_effect=RuntimeError('private governance append failure')):
            status,_,body = await self._request('POST','/api/verifications',payload=self.collect)
        self.assertEqual(status,500,body)
        self.assertEqual(self.evidence.store.load(),before)
        self.assertEqual(self.evidence.governance.store.load(),governed)
        # Immutable storage may contain unreferenced bytes. Do not assert file
        # rollback or infer permission to retry from an HTTP response alone.

    async def test_lookup_governance_denial_and_changed_original_request_never_rekey(self):
        status,_,first = await self._request('POST','/api/verifications',payload=self.collect)
        self.assertEqual(status,200,first)
        before = self.evidence.store.load()
        status,_,body = await self._request('POST','/api/verifications',
            payload={**self.collect,'expected_head_sha':self.remote_head})
        self.assertEqual(status,403,body)
        with patch.object(self.evidence.governance,'filter_context',
                return_value=SimpleNamespace(denied_record_ids=(first['procedure_input']['governance_record_id'],))):
            status,_,body = await self._request('GET','/api/verifications?method='+METHOD+'&source='+quote(first['source'],safe=''))
        self.assertEqual(status,403,body)
        self.assertNotIn('procedure_input',body)
        self.assertEqual(self.evidence.store.load(),before)

    async def test_six_field_recovery_observes_remote_branch_without_switching_local_checkout(self):
        self.mr['source_branch'] = 'codex/current-existing-mr'
        local_branch = git(self.checkout,'symbolic-ref','--short','HEAD')
        status,_,body = await self._request('POST','/api/verifications',payload=self.collect)
        self.assertEqual(status,200,body)
        values = json.loads(self.content(body['procedure_input']['id']))
        self.assertEqual(values['branch'],self.mr['source_branch'])
        self.assertEqual(values['existing_change_request_number'],33)
        self.assertEqual(git(self.checkout,'symbolic-ref','--short','HEAD'),local_branch)
        self.assertNotEqual(local_branch,values['branch'])


class LegacyWorkerPublicationInputTests(WorkerPublicationInputTests):
    publication_schema = 'five'

    async def test_legacy_five_fields_follow_published_contract_and_refuse_unexpressible_recovery(self):
        status,_,body = await self._request('POST','/api/verifications',payload=self.collect)
        self.assertEqual(status,200,body)
        values = json.loads(self.content(body['procedure_input']['id']))
        self.assertEqual(set(values),{'execution_workspace_id','branch','head_revision',
            'expected_remote_revision','pull_request_number'})
        before = self.evidence.store.load()
        self.mr['source_branch'] = 'codex/different-existing-branch'
        status,_,body = await self._request('POST','/api/verifications',payload={**self.collect,'request_id':'f'*32})
        self.assertEqual(status,403,body)
        self.assertEqual(self.evidence.store.load(),before)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(cls(name) for cls in (WorkerPublicationInputTests,LegacyWorkerPublicationInputTests)
        for name in cls.__dict__ if name.startswith('test_'))
