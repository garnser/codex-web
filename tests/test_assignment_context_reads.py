"""Exact canonical context reads; private state, no application or provider calls."""
import copy
import unittest
from urllib.parse import urlencode
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from fastapi import HTTPException

from codex_web.decisions import Decision
from codex_web.definitions import DefinitionLifecycle, reference_for
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.execution_workers import ExecutionWorker
from codex_web.goals import GoalRecord, GoalWorkGraphBinding
from codex_web.identity import PrincipalKind
from codex_web.services.assignment_reasoning_broker import (
    AssignmentReasoningBroker, CONTEXT_READ_IDS, OPERATIONS, resolve,
)
from codex_web.services.control_plane_broker import ControlPlaneBrokerService
from codex_web.services.procedure_execution import ProcedureExecutionService, ProcedureExecutionError
from codex_web.services.skills import SkillResolutionFailure
from codex_web.services.goals import GoalNotFoundError
from codex_web.storage.decisions import DecisionNotFoundError
from codex_web.services.projects import ProjectNotFoundError
from codex_web.skills import SkillUpdate
from tests import test_assignment_reasoning_broker as reasoning_fixture
from tests import test_skill_procedures as skill_fixture


class AssignmentContextReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reasoning_fixture.AssignmentReasoningTests.asyncSetUp(self)
        self.skill_fixture = skill_fixture.ProcedureTests()
        self.skill_fixture.setUp()
        self.actor = self.skill_fixture.admin
        self.assignment.organization_id = self.actor.organization_id
        self.assignment.workspace_id = self.actor.workspace_id
        self.assignment.created_by = self.actor.identity_id
        self.live = copy.copy(self.assignment)
        self.broker._requester_actor.return_value = self.actor
        self.broker._state_for_target.return_value = NS(
            organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id,
            project_id='project-a', resource_ids=('repo-a',))
        self.procedures.skills = self.skill_fixture.skills
        def project(project_id, scope):
            if project_id != 'project-a' or scope != self.actor.tenant:
                raise ProjectNotFoundError(project_id)
            return NS(id=project_id, organization_id=scope.organization_id, workspace_id=scope.workspace_id)
        self.skill_fixture.skills.projects=NS(get=project)
        self.goal = GoalRecord(organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id, title='Qualification', description='Exact evidence',
            owner_identity_id=self.actor.identity_id, created_by=self.actor.identity_id,
            updated_by=self.actor.identity_id,
            work_graph_bindings=(GoalWorkGraphBinding(project_id='project-a', root_work_item_refs=('work-a',)),))
        self.decision = Decision(organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id, project_id='project-a', title='Qualification decision',
            question='Is the scoped evidence sufficient?', initiator_identity_id=self.actor.identity_id,
            participants=(), options=(), importance='medium',
            budget={'max_input_tokens':1000, 'max_output_tokens':500, 'max_model_calls':1, 'max_cost_usd':1}, limits={})
        self.fixture.service.goals = NS(get=Mock(side_effect=lambda *a, **k: self.goal))
        self.fixture.service.decisions = NS(get=Mock(side_effect=lambda *a, **k: self.decision))

    async def asyncTearDown(self):
        self.skill_fixture.tearDown()
        await reasoning_fixture.AssignmentReasoningTests.asyncTearDown(self)

    call = reasoning_fixture.AssignmentReasoningTests.call

    def publish(self):
        return self.skill_fixture.publish(self.skill_fixture.draft())

    @staticmethod
    def query(view):
        return {k: [str(view['definitionReference'][k])] for k in ('record_id', 'revision', 'checksum')}

    async def test_exact_goal_and_decision_use_canonical_read_owners_only(self):
        for name, item in (('goal.read', self.goal), ('decision.read', self.decision)):
            result = await self.call(name, target=item.id)
            self.assertEqual(result, {'item':item.model_dump(mode='json')})
        self.fixture.service.goals.get.assert_called_once_with(self.goal.id,
            scope=self.actor.tenant, project_id='project-a')
        self.fixture.service.decisions.get.assert_called_once_with(self.decision.id, actor=self.actor)
        self.procedures.start.assert_not_called()
        self.procedures.advance.assert_not_awaited()
        self.assertEqual(self.fixture.gateway.requests, [])

    async def test_actual_broker_dispatch_preserves_all_three_owner_response_shapes(self):
        service=ControlPlaneBrokerService(identity=None,authority=None,work_items=None,audit=None,operator=NS())
        service._dispatch_scope=Mock(return_value=(self.actor,self.actor,None,None))
        service.assignment_reasoning=self.adapter
        view=self.publish()
        paths=[('/api/goals/'+self.goal.id,'goal.read'),
               ('/api/decisions/'+self.decision.id,'decision.read'),
               ('/api/skills/'+view['skillId']+'?'+urlencode({k:v[0] for k,v in self.query(view).items()}),
                'skill.procedure.read')]
        for path,name in paths:
            status,result,operation,target,_=await service.dispatch(assignment=self.assignment,
                worker_actor=NS(identity_id='worker-service'),method='GET',raw_target=path,body=b'')
            self.assertEqual(status,200)
            self.assertEqual(operation.id,name)
            self.assertIn('item',result)
            if name=='skill.procedure.read':
                self.assertEqual(target,view['skillId'])
                self.assertEqual(result['item']['definitionReference'],view['definitionReference'])

    async def test_cross_tenant_project_and_unbound_goal_are_refused(self):
        for update in ({'organization_id':'foreign'}, {'workspace_id':'foreign'},
                       {'work_graph_bindings':()},
                       {'work_graph_bindings':(GoalWorkGraphBinding(project_id='foreign'),)}):
            with self.subTest(update=update):
                self.goal = self.goal.model_copy(update=update)
                with self.assertRaises(HTTPException):
                    await self.call('goal.read', target=self.goal.id)
                self.goal = self.goal.model_copy(update={
                    'organization_id':self.actor.organization_id,'workspace_id':self.actor.workspace_id,
                    'work_graph_bindings':(GoalWorkGraphBinding(project_id='project-a'),)})
        for field in ('organization_id', 'workspace_id', 'project_id'):
            original = self.decision
            self.decision = original.model_copy(update={field:'foreign'})
            with self.assertRaises(HTTPException):
                await self.call('decision.read', target=self.decision.id)
            self.decision = original

    async def test_goal_root_resource_scope_is_checked(self):
        self.broker._state_for_target.return_value.resource_ids = ('other-repo',)
        with self.assertRaisesRegex(HTTPException, 'assigned resources'):
            await self.call('goal.read', target=self.goal.id)

    async def test_admin_root_read_keeps_per_resource_declaration_and_authority(self):
        self.assignment.execution_profile_id = self.live.execution_profile_id = 'orchestration-admin'
        self.assignment.resource_ids = self.live.resource_ids = ()
        authorize = Mock(side_effect=ValueError('undeclared repository'))
        self.broker.authority=NS()
        self.broker.execution_profiles.authorize_administrative_target = authorize
        with self.assertRaisesRegex(ValueError, 'undeclared repository'):
            await self.call('goal.read', target=self.goal.id)
        authorize.assert_called_once()
        self.procedures.start.assert_not_called()

    async def test_real_admin_definition_accepts_only_declared_exact_reads_with_live_read_authority(self):
        from tests import test_control_plane_broker as broker_fixture
        from codex_web.agent_profiles import AgentProfileExecutionBinding
        from codex_web.authority import AuthorityLevel, AuthorityRoleCatalogDefinition, AuthorityGrant
        from codex_web.control_plane_broker import ControlPlaneBrokerOperation
        from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
        from codex_web.services.control_plane_broker import ControlPlaneBrokerAuthorityDeniedError
        from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
        from codex_web.resources import ResourceCreate, ResourceType
        from codex_web.services.resources import ResourceCatalogService
        from codex_web.storage.resource_catalog import ResourceCatalogStore

        f=broker_fixture.ControlPlaneBrokerTests()
        await f.asyncSetUp(start_broker=False)
        try:
            actor=f.identity.local_trusted_actor()
            resources=ResourceCatalogService(ResourceCatalogStore(f.sqlite))
            repository=resources.create(ResourceCreate(name='Private read fixture',
                resource_type=ResourceType.REPOSITORY),actor=actor)
            f.authority.resources=resources
            profiles=install_execution_profile_definitions(f.registry)
            payload=execution_profile_catalog_seed_payload()
            payload['profiles'].append({'id':'orchestration-admin','name':'Scoped read coordinator',
                'description':'Read-only exact canonical context','workspace_mode':'scratch',
                'repository_access':'none','required_worker_capabilities':['command_execution'],
                'administrative_repository_ids':[repository.id],
                'control_plane_operations':[*sorted(CONTEXT_READ_IDS),'work_item.progress','goal.update']})
            record=f.registry.create_draft(DefinitionDraftCreate(
                definition_id='execution.profiles.default',kind='execution-profile-catalog',
                definition_schema_version='1.1',scope_type='project',scope_id='project-a',
                payload=payload,actor=actor.identity_id))
            f.registry.publish(record.record_id,DefinitionPublishRequest(actor=actor.identity_id))
            _,reference=profiles.resolve('orchestration-admin',organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,project_id='project-a')
            binding=AgentProfileExecutionBinding(profile_id='existing-orchestrator',profile_revision=1,
                profile_record_id='profile-record-one',role_id='orchestrator',
                authority_role_id='local-admin',execution_profile_id='orchestration-admin')
            profiles.agent_profiles=NS(get=lambda *a,**k:NS(lifecycle='active',revision=1,
                record_id=binding.profile_record_id,authority_role_id=binding.authority_role_id,
                execution_profile_id=binding.execution_profile_id,role_id=binding.role_id))
            assignment=f.assignment.model_copy(update={'execution_profile_id':'orchestration-admin',
                'execution_profile_definition':reference,'agent_profile':binding})
            active=f.authority.catalog_record(actor=actor,project_id='project-a')
            catalog=AuthorityRoleCatalogDefinition.model_validate(active.payload)
            read_grants=tuple(AuthorityGrant(id='read-'+str(i),capability=op.capability,
                level=AuthorityLevel.READ,project_ids=('project-a',))
                for i,op in enumerate(op for op in OPERATIONS if op.id in CONTEXT_READ_IDS))
            catalog=catalog.model_copy(update={'roles':tuple(
                role.model_copy(update={'grants':read_grants}) if role.id=='local-admin' else role
                for role in catalog.roles)})
            authority_record=f.registry.create_draft(DefinitionDraftCreate(
                definition_id=active.definition_id,kind=active.kind,
                definition_schema_version=active.definition_schema_version,
                scope_type=active.scope_type,scope_id=active.scope_id,
                payload=catalog.model_dump(mode='json'),actor=actor.identity_id))
            f.registry.approve_publication(authority_record.record_id,actor='test-reviewer',
                reference='READ-ONLY-FIXTURE',reason='Authorize the private read-only fixture')
            f.registry.publish(authority_record.record_id,DefinitionPublishRequest(actor=actor.identity_id))
            f.service.execution_profiles=profiles
            for operation in (op for op in OPERATIONS if op.id in CONTEXT_READ_IDS):
                actual=profiles.authorize_administrative_target(assignment,operation_id=operation.id,
                    repository_id=repository.id,actor=actor,authority=f.authority)
                self.assertEqual(actual.repository_access,'none')
                f.service._authorize(assignment,operation,actor=actor,resource_ids=(repository.id,))
            for op,target in (('procedure.read',repository.id),('goal.read','resource-foreign'),
                              ('goal.update',repository.id)):
                with self.subTest(operation=op,target=target), self.assertRaises(ValueError):
                    profiles.authorize_administrative_target(assignment,operation_id=op,
                        repository_id=target,actor=actor,authority=f.authority)
            progress=ControlPlaneBrokerOperation(id='work_item.progress',method='POST',
                path_template='/api/work-items/{ref}/progress',capability='work_item.progress',
                authority_level=AuthorityLevel.EXECUTE)
            with self.assertRaises(ControlPlaneBrokerAuthorityDeniedError):
                f.service._authorize(assignment,progress,actor=actor,resource_ids=(repository.id,))
            expired=assignment.model_copy(update={'lease':assignment.lease.model_copy(update={'expires_at':0})})
            with self.assertRaisesRegex(ValueError,'live fenced'):
                profiles.authorize_administrative_target(expired,operation_id='goal.read',
                    repository_id=repository.id,actor=actor,authority=f.authority)
        finally:
            f.temp.cleanup()

    async def test_undeclared_changed_fence_and_authority_refuse_before_owner(self):
        self.allowed.remove('goal.read')
        with self.assertRaises(HTTPException):
            await self.call('goal.read', target=self.goal.id)
        self.allowed.append('goal.read')
        self.live.fence += 1
        with self.assertRaises(HTTPException):
            await self.call('goal.read', target=self.goal.id)
        self.live.fence = self.assignment.fence
        self.broker._authorize.side_effect = HTTPException(403, 'authority denied')
        with self.assertRaises(HTTPException):
            await self.call('goal.read', target=self.goal.id)
        self.fixture.service.goals.get.assert_not_called()

    async def test_assignment_retirement_during_read_does_not_return_context(self):
        def read(*a, **k):
            self.live.fence += 1
            return self.goal
        self.fixture.service.goals.get.side_effect = read
        with self.assertRaises(HTTPException):
            await self.call('goal.read', target=self.goal.id)
        self.fixture.service.goals.get.assert_called_once()

    async def test_missing_objects_and_changed_execution_profile_have_no_fallback(self):
        self.live.execution_profile_id='repository-write'
        with self.assertRaises(HTTPException):
            await self.call('goal.read',target=self.goal.id)
        self.live.execution_profile_id=self.assignment.execution_profile_id
        for name, service, error in (('goal.read',self.fixture.service.goals,GoalNotFoundError),
                                    ('decision.read',self.fixture.service.decisions,DecisionNotFoundError)):
            service.get.side_effect=error('not found')
            with self.assertRaises(HTTPException):
                await self.call(name,target='missing')
            service.get.assert_called_once()

    async def test_unexpected_storage_failure_is_not_classified_as_missing_context(self):
        self.fixture.service.goals.get.side_effect=RuntimeError('unexpected storage failure')
        with self.assertRaisesRegex(RuntimeError,'unexpected storage failure'):
            await self.call('goal.read',target=self.goal.id)

    async def test_real_live_owner_refuses_expired_assignment_and_workspace_leases(self):
        a = self.assignment
        a.status='running'; a.required_capabilities=('command_execution',)
        a.execution_contract_version='test/1.0'; a.sandbox='read-only'; a.deadline_at=500
        a.execution_workspace_id='workspace-one'
        a.lease=NS(expires_at=200, fence=a.fence, worker_id=a.assigned_worker_id)
        self.live = copy.copy(a)
        worker=ExecutionWorker(id=a.assigned_worker_id, organization_id=a.organization_id,
            workspace_id=a.workspace_id, service_identity_id='worker-service', version='test',
            capabilities=a.required_capabilities, supported_execution_contract_versions=(a.execution_contract_version,),
            supported_sandbox_profiles=('read-only',), registered_by='test')
        workspace=NS(status='active', execution_id=a.execution_id, project_id=a.project_id,
            resource_ids=a.resource_ids, lease_id='resource-lease', id=a.execution_workspace_id)
        lease=NS(released_at=None, expires_at=200, execution_id=a.execution_id,
            execution_workspace_id=a.execution_workspace_id)
        workers=NS(store=NS(worker=lambda _:worker),workspaces=NS(
            get=lambda *args:workspace,store=NS(lease=lambda _:lease)))
        owner=ProcedureExecutionService(workers=workers, skills=None, discovery=None, intents=None,
            evidence=None, identity=NS(actor_for_identity=lambda *a, **k:NS(principal_kind=PrincipalKind.SERVICE)),
            authority=None, approvals=None, attention=None, control_actor=self.actor, clock=lambda:100)
        self.procedures._live=owner._live
        a.lease.expires_at=99
        with self.assertRaisesRegex(HTTPException, 'lease or fence'):
            await self.call('goal.read',target=self.goal.id)
        a.lease.expires_at=200; lease.expires_at=99
        with self.assertRaisesRegex(HTTPException, 'lease or fence'):
            await self.call('goal.read',target=self.goal.id)
        self.fixture.service.goals.get.assert_not_called()

    async def test_real_effective_procedure_returns_full_exact_owner_view(self):
        view=self.publish()
        result=await self.call('skill.procedure.read',target=view['skillId'],query=self.query(view))
        self.assertEqual(result['item'],self.skill_fixture.skills.get(view['skillId'],
            actor=self.actor,project_id='project-a',revision=view['revision']))
        self.assertEqual(result['item']['definitionReference'], view['definitionReference'])
        self.assertIsNotNone(result['item']['skill']['operational_procedure'])
        self.assertIn('security',result['item'])
        self.procedures.start.assert_not_called()

    async def test_procedure_exact_reference_cannot_retarget_revision_or_checksum(self):
        view=self.publish()
        for key,value in (('record_id','another'),('revision','99'),('checksum','b'*64)):
            query=self.query(view); query[key]=[value]
            with self.assertRaises(HTTPException):
                await self.call('skill.procedure.read',target=view['skillId'],query=query)
        draft=self.skill_fixture.skills.update(view['skillId'],SkillUpdate(
            instructions='New reviewed guidance',reason='Test exact revision retirement'),actor=self.actor)
        new=self.skill_fixture.publish(draft)
        self.assertNotEqual(new['recordId'],view['recordId'])
        with self.assertRaises(HTTPException):
            await self.call('skill.procedure.read',target=view['skillId'],query=self.query(view))

    async def test_procedure_security_expiry_and_wrong_scope_remain_closed(self):
        view=self.publish()
        with patch.object(self.skill_fixture.skills.security,'assert_allowed',side_effect=SkillResolutionFailure('procedure_security_blocked','blocked')):
            with self.assertRaises(HTTPException):
                await self.call('skill.procedure.read',target=view['skillId'],query=self.query(view))
        self.skill_fixture.now=301
        with self.assertRaises(HTTPException):
            await self.call('skill.procedure.read',target=view['skillId'],query=self.query(view))
        self.skill_fixture.now=100
        self.assignment.workspace_id=self.live.workspace_id='foreign'
        with self.assertRaises(HTTPException):
            await self.call('skill.procedure.read',target=view['skillId'],query=self.query(view))

    async def test_draft_missing_and_disabled_procedure_do_not_become_readable(self):
        draft=self.skill_fixture.draft()
        with self.assertRaises(HTTPException):
            await self.call('skill.procedure.read',target=draft['skillId'],query=self.query(draft))
        with self.assertRaises(HTTPException):
            await self.call('skill.procedure.read',target='missing',query=self.query(draft))
        view=self.skill_fixture.publish(draft)
        self.skill_fixture.definitions.quarantine(view['recordId'],actor='admin',reason='Unsafe test record')
        with self.assertRaises(HTTPException):
            await self.call('skill.procedure.read',target=view['skillId'],query=self.query(view))

    async def test_exact_read_routes_body_query_and_no_generic_inventory(self):
        for method,path in (('GET','/api/goals'),('GET','/api/skills'),('POST','/api/goals/a'),
                            ('GET','/api/definitions/records/a'),('GET','/api/goals/%2Fother')):
            with self.subTest(path=path), self.assertRaises(Exception):
                ControlPlaneBrokerService._resolve_operation(method,path)
        for name in CONTEXT_READ_IDS:
            with self.assertRaises(HTTPException):
                await self.call(name,target='exact',payload={'override':True})
        view=self.publish()
        for query in ({}, {'revision':['1']}, {**self.query(view),'extra':['1']},
                      {**self.query(view),'revision':['1','2']}):
            with self.assertRaises(HTTPException):
                await self.call('skill.procedure.read',target=view['skillId'],query=query)
        with self.assertRaises(HTTPException):
            await self.call('goal.read',target=self.goal.id,query={'latest':['true']})

    async def test_catalog_advertises_only_current_declared_exact_reads(self):
        # Use the actual public catalog, not the adapter's operation list alone.
        service=ControlPlaneBrokerService(identity=None,authority=None,work_items=None,audit=None,
            operator=NS(),execution_profiles=self.broker.execution_profiles)
        self.assignment.execution_workspace_id=None
        self.assignment.repository_scope=None; self.assignment.repository_target=None
        reference=NS(model_dump=lambda **k:{'record_id':'exact'})
        self.assignment.execution_profile_definition=reference
        self.broker.execution_profiles.resolve=lambda *a, **k:(NS(control_plane_operations=self.allowed),reference)
        public=service.public_assignment_capability(self.assignment)
        ids={op['id'] for op in public['operations']}
        self.assertTrue(CONTEXT_READ_IDS.issubset(ids))
        self.allowed.remove('goal.read')
        ids={op['id'] for op in service.public_assignment_capability(self.assignment)['operations']}
        self.assertNotIn('goal.read',ids)
        self.assertIn('decision.read',ids)
        seed=execution_profile_catalog_seed_payload()['profiles']
        self.assertTrue(CONTEXT_READ_IDS.issubset(next(p for p in seed if p['id']=='repository-write')['control_plane_operations']))
        self.assertNotIn('orchestration-admin',{p['id'] for p in seed})
