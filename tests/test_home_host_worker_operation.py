"""Canonical private Home/worker fixtures; backend effects are fake only."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from tests import test_home_operator_host as fixture
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_profiles import HOME_HOST_COMMAND_CONTRACT
from codex_web.execution_workers import AssignmentRenewRequest
from codex_web.local_execution_backend import LocalExecutionResult
from codex_web.services.home_host_action_provider import HostActionOutcomeUnknown
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime


class HomeHostWorkerOperationTests(fixture.HomeOperatorHostTests):
    def publish(self, payload):
        profile=next(p for p in payload['profiles'] if p['id']=='orchestration-only')
        profile['control_plane_operations']=list(dict.fromkeys([*profile['control_plane_operations'],'operator.host.command','operator.project.thread.create']))
        profile['home_operator']={'host_resource_id':getattr(self,'host_resource_id','resource-local-host'),'trusted_worker_ids':[self.f.worker.id],
            'host_network_access':True,'project_targets':[{'project_id':'target','child_profiles':[{'profile_id':'developer','revision':1}]}]}
        profile['home_operator'].update(getattr(self,'host_options',{}))
        record=self.registry.create_draft(DefinitionDraftCreate(definition_id='execution.profiles.default',kind='execution-profile-catalog',
            definition_schema_version='1.3',scope_type='project',scope_id='home',payload=payload,actor='fixture'))
        self.registry.publish(record.record_id,DefinitionPublishRequest(actor='fixture'))
        return record

    async def asyncSetUp(self):
        await super().asyncSetUp()
        def enable(state):
            state.workers[0].supported_execution_contract_versions=(*state.workers[0].supported_execution_contract_versions,HOME_HOST_COMMAND_CONTRACT)
            return state
        self.f.workers.store.update_worker(self.f.worker.id,enable)
        self.native_backend=Mock()
        self.host_backend=SimpleNamespace(worker_id=self.f.worker.id,max_output_bytes=1024,run_action=Mock(side_effect=self.effect))
        self.runtime=LocalExecutionWorkerRuntime(self.f.workers,self.f.workspaces,self.native_backend,
            worker=self.f.worker,worker_actor=self.worker_actor,control_actor=self.actor,trusted_host_backend=self.host_backend)
        self.after_spawn=lambda:None
        self.effects=0

    def origin(self, operation='operator.host.command'):
        return self.service.home_operator_host.authorize_operation(self.assignment,operation_id=operation,
            worker_actor=self.worker_actor,requester_id=self.actor.identity_id)

    def effect(self, operation, *, admission_guard, **kwargs):
        admission_guard();self.effects+=1;self.after_spawn();admission_guard()
        return LocalExecutionResult('true','sha256:'+'a'*64,0,'','',0.01)

    def command(self):
        return self.runtime.execute_trusted_host_operation(self.assignment,('/usr/bin/true',),
            admission_guard=self.origin,timeout_seconds=1,max_output_bytes=1024)

    async def test_nonterminal_host_operation_preserves_parent_and_allows_native_renewal(self):
        self.after_spawn=lambda:self.f.workers.renew(self.f.worker.id,self.assignment.id,
            AssignmentRenewRequest(lease_token=self.assignment.lease.lease_token,fence=self.assignment.fence,lease_seconds=120),actor=self.worker_actor)
        with patch.object(self.f.workers,'complete',side_effect=AssertionError('must not complete Home')):
            result=self.command()
        current=self.current()
        self.assertTrue(result.succeeded);self.assertEqual(self.effects,1)
        self.assertEqual(current.status,'running');self.assertEqual(current.fence,self.assignment.fence)
        self.assertEqual(current.lease.lease_token,self.assignment.lease.lease_token)
        self.native_backend.run.assert_not_called()

    async def test_changed_selection_before_operation_never_launches(self):
        self.select(None,1)
        with self.assertRaises(Exception):self.command()
        self.assertEqual(self.effects,0);self.host_backend.run_action.assert_not_called()

    async def test_fence_revoked_after_effect_is_unknown_and_never_replayed(self):
        self.after_spawn=lambda:self.mutate_assignment(fence=self.assignment.fence+1)
        with self.assertRaises(HostActionOutcomeUnknown):self.command()
        self.assertEqual(self.effects,1);self.assertEqual(self.host_backend.run_action.call_count,1)
        self.assertEqual(self.current().fence,self.assignment.fence+1)

    async def test_worker_without_explicit_contract_never_launches(self):
        def revoke(state):
            state.workers[0].supported_execution_contract_versions=('1.0',)
            return state
        self.f.workers.store.update_worker(self.f.worker.id,revoke)
        with self.assertRaises(Exception):self.command()
        self.assertEqual(self.effects,0);self.host_backend.run_action.assert_not_called()

    async def test_cross_project_origin_uses_its_own_declared_capability(self):
        context=self.origin('operator.project.thread.create')
        self.assertEqual(context.requester.identity_id,self.actor.identity_id)
        self.assertEqual(context.envelope.project_targets[0].project_id,'target')
        with self.assertRaises(Exception):self.origin('operator.project.undeclared')
        self.assertEqual(self.effects,0)

    async def test_missing_backend_never_uses_native_backend_as_fallback(self):
        self.runtime.trusted_host_backend=None
        with self.assertRaises(Exception):self.command()
        self.assertEqual(self.effects,0);self.native_backend.run.assert_not_called()


def load_tests(loader, tests, pattern):
    # Select only this slice; do not rerun all inherited metadata fixtures.
    names=[name for name in HomeHostWorkerOperationTests.__dict__ if name.startswith('test_')]
    return unittest.TestSuite(HomeHostWorkerOperationTests(name) for name in names)
