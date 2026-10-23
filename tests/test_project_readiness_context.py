from __future__ import annotations

import unittest
from unittest.mock import Mock

from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
from codex_web.storage.definition_registry import DefinitionRegistryStore
from tests import test_project_readiness as fixture


class ContextualProjectReadinessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ProjectReadinessTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.registry = DefinitionRegistryService(DefinitionRegistryStore(self.fixture.sqlite))
        self.profiles = install_execution_profile_definitions(self.registry)
        self.record = self.registry.create_draft(DefinitionDraftCreate(
            definition_id='execution.profiles.default', kind='execution-profile-catalog',
            scope_type='project', scope_id='project-a', actor='fixture',
            definition_schema_version='1.0', payload=execution_profile_catalog_seed_payload()))
        self.registry.publish(self.record.record_id, DefinitionPublishRequest(actor='fixture'))
        self.service = self.fixture.service(resources=fixture._Resources(repository_ids=()))
        self.service.execution_profiles = self.profiles

    def evaluate(self, profile='orchestration-only', **kwargs):
        return self.service.evaluate('project-a', actor=fixture._actor(),
                                     execution_profile_id=profile, **kwargs)

    def test_canonical_scratch_context_does_not_require_a_repository(self):
        generic = self.service.evaluate('project-a', actor=fixture._actor())
        self.assertFalse(generic.execution_ready)
        result = self.evaluate()
        self.assertTrue(result.execution_ready)
        self.assertEqual(result.execution_context.profile_id, 'orchestration-only')
        self.assertEqual(result.execution_context.definition.record_id, self.record.record_id)
        self.assertEqual(result.execution_context.sandbox, 'workspace-write')
        checks = {item.id: item for item in result.checks}
        self.assertFalse(checks['repository:resources'].required)
        self.assertFalse(checks['repository:execution-target'].required)
        self.assertEqual(checks['execution:worker'].details['required_capabilities'], ['command_execution'])

    def test_repository_profile_still_requires_both_repository_prerequisites(self):
        result = self.evaluate('repository-write')
        self.assertFalse(result.execution_ready)
        self.assertEqual({x.code for x in result.blockers},
                         {'repository_resource_missing', 'repository_target_missing'})

    def test_unknown_profile_is_blocked_without_claiming_a_definition(self):
        result = self.evaluate('unpublished-profile')
        self.assertFalse(result.execution_ready)
        self.assertIn('execution_profile_unavailable', [x.code for x in result.blockers])
        self.assertIsNone(result.execution_context.definition)

    def test_canonical_scope_is_checked_before_profile_resolution(self):
        self.profiles.resolve = Mock(side_effect=AssertionError('wrong-scope resolution'))
        with self.assertRaises(LookupError):
            self.service.evaluate('foreign-project', actor=fixture._actor(),
                                  execution_profile_id='orchestration-only')
        self.profiles.resolve.assert_not_called()

    def test_context_never_overwrites_generic_readiness_history(self):
        generic = self.service.evaluate('project-a', actor=fixture._actor(), record=True)
        before = self.fixture.readiness_store.get('project-a', organization_id='local', workspace_id='default')
        self.assertTrue(self.evaluate(record=True).execution_ready)
        after = self.fixture.readiness_store.get('project-a', organization_id='local', workspace_id='default')
        self.assertEqual(before, after)
        self.assertEqual(after.last_correlation_id, generic.correlation_id)

    def test_worker_denial_remains_required_for_scratch(self):
        self.service.workers = fixture._Workers(ready=False)
        result = self.evaluate()
        self.assertFalse(result.execution_ready)
        self.assertIn('worker_capability_missing', [x.code for x in result.blockers])

    def test_authentication_denial_remains_required_for_scratch(self):
        self.service.runtime_binding = ExecutionRuntimeBinding(provider_id='openai', runtime_id='codex',
            capability_revision=1, authentication_mode='trusted_local_session')
        self.service.local_session_probe = lambda: False
        result = self.evaluate()
        self.assertFalse(result.execution_ready)
        self.assertIn('local_session_unavailable', [x.code for x in result.blockers])

    def test_superseded_catalog_cannot_keep_removed_profile_ready(self):
        payload = execution_profile_catalog_seed_payload()
        payload['profiles'].append(dict(next(x for x in payload['profiles']
            if x['id'] == 'orchestration-only'), id='extra-scratch'))
        extra = self.registry.create_draft(DefinitionDraftCreate(
            definition_id='execution.profiles.default', kind='execution-profile-catalog',
            scope_type='project', scope_id='project-a', actor='fixture',
            definition_schema_version='1.0', payload=payload))
        self.registry.publish(extra.record_id, DefinitionPublishRequest(actor='fixture'))
        old = self.evaluate('extra-scratch').execution_context.definition
        changed = self.registry.create_draft(DefinitionDraftCreate(
            definition_id='execution.profiles.default', kind='execution-profile-catalog',
            scope_type='project', scope_id='project-a', actor='fixture',
            definition_schema_version='1.0', payload=execution_profile_catalog_seed_payload()))
        self.registry.publish(changed.record_id, DefinitionPublishRequest(actor='fixture'))
        result = self.evaluate('extra-scratch')
        self.assertFalse(result.execution_ready)
        self.assertIsNone(result.execution_context.definition)
        self.assertEqual(old.record_id, extra.record_id)

    def test_unrelated_storage_failure_propagates(self):
        self.profiles.resolve = Mock(side_effect=RuntimeError('metadata unavailable'))
        with self.assertRaisesRegex(RuntimeError, 'metadata unavailable'):
            self.evaluate()

    def test_api_exposes_canonical_context_and_preserves_generic_history(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from codex_web.api.project_readiness import build_project_readiness_router
        app = FastAPI()

        @app.middleware('http')
        async def actor(request, call_next):
            request.state.identity_actor = fixture._actor()
            return await call_next(request)

        app.include_router(build_project_readiness_router(self.service))
        with TestClient(app) as client:
            generic = client.get('/api/projects/project-a/readiness').json()
            prior = self.fixture.readiness_store.get('project-a', organization_id='local', workspace_id='default')
            contextual = client.get('/api/projects/project-a/readiness', params={
                'execution_profile_id':'orchestration-only', 'sandbox':'read-only'})
            self.assertEqual(contextual.status_code, 200)
            result = contextual.json()
            self.assertTrue(result['execution_ready'])
            self.assertEqual(result['execution_context']['definition']['record_id'], self.record.record_id)
            self.assertEqual(result['execution_context']['sandbox'], 'read-only')
            self.assertFalse(generic['execution_ready'])
            self.assertEqual(self.fixture.readiness_store.get('project-a', organization_id='local', workspace_id='default'), prior)
            self.assertEqual(client.get('/api/projects/project-a/readiness?sandbox=read-only').status_code, 422)
            self.assertEqual(client.get('/api/projects/project-a/readiness?execution_profile_id=orchestration-only&sandbox=unsupported').status_code, 422)

    def test_requested_sandbox_is_probed_without_changing_project(self):
        seen = []
        self.service.environment_probe = lambda project, actor: seen.append(project.sandbox) or {
            'available': False, 'code': 'sandbox_profile_unsupported', 'reason': 'not enforced'}
        result = self.evaluate(sandbox='read-only')
        self.assertEqual(seen, ['read-only'])
        self.assertEqual(self.fixture.project.sandbox, 'workspace-write')
        self.assertEqual(result.execution_context.sandbox, 'read-only')
        self.assertFalse(result.execution_ready)
        self.assertIn('sandbox_profile_unsupported', [x.code for x in result.blockers])


if __name__ == '__main__':
    unittest.main()
