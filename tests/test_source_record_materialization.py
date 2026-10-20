"""One provider record, through real HTTP/credentials and guarded SQLite state."""
from dataclasses import replace
from pathlib import Path
import json
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.api.work_items import build_work_items_router
from codex_web.integrations.github_client import GitHubClient
from codex_web.identity import MembershipRole
from codex_web.services.github_task_source import GitHubTaskSource
from codex_web.services.task_sources import TaskSourceCapabilities, TaskSourceCapability
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.models import TaskSourceConfiguration, WorkItemState
from codex_web.secrets import SecretCreate
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_task_source_project_resolution as fixtures


class SourceRecordMaterializationTests(unittest.TestCase):
    def setUp(self):
        f = fixtures.ProjectSourceResolutionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.fixture = f
        root = Path(f.temp.name)
        self.repo = ModelMapRepository(SQLiteStateStore(root / 'point.sqlite3'),
            namespace='work_item_states', legacy_path=root / 'legacy.json', model=WorkItemState)
        deps = replace(f.service.work_items, get_state=self.repo.get,
            get_state_by_source_identity=lambda identity: self.repo.get(identity.external_id),
            load_states=self.repo.load, save_state=lambda value: self.repo.put(value.ref, value),
            update_state=self.repo.update)
        f.service.work_items = deps
        f.service.state_machine.dependencies = deps
        f.service.task_source_projector.dependencies = deps
        self.calls = []
        self.status = 200
        self.related_status = 200
        self.response_number = 59
        self.race = None
        self.transport_failure = False
        self.pull_request_response = False
        f.service.builtin_task_source_runtime.github_client = GitHubClient(
            transport=httpx.MockTransport(self.respond))
        app = FastAPI()
        @app.middleware('http')
        async def actor(request, call_next):
            request.state.identity_actor = f.actor
            request.state.tenant_scope = f.actor.tenant
            return await call_next(request)
        app.include_router(build_work_items_router(f.service))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def respond(self, request):
        self.calls.append((request.method, request.url.path))
        self.assertEqual(request.method, 'GET')
        if self.transport_failure:
            raise httpx.ReadTimeout('bounded fixture transport failure', request=request)
        if self.race:
            race, self.race = self.race, None
            race()
        if self.status != 200:
            return httpx.Response(self.status, json={'message': 'not available'})
        if request.url.path.endswith('/related_merge_requests'):
            return httpx.Response(self.related_status, json=[], headers={'X-Total': '0', 'X-Next-Page': ''})
        self.assertTrue(request.url.path.endswith('/issues/59'), request.url)
        return httpx.Response(200, json={'number': self.response_number,
            'iid': self.response_number, 'title': 'Original closed source record',
            'state': 'closed', 'body': 'Actual provider record; no replacement issue',
            'labels': [], 'assignees': [], 'updated_at': '2026-10-20T00:00:00Z',
            'references': {'full': f'acme/app#{self.response_number}'},
            'html_url': 'https://github.test/acme/app/issues/59',
            **({'pull_request': {'url': 'https://github.test/acme/app/pulls/59'}}
                if self.pull_request_response else {})})

    def fetch(self, reference='acme/app#59', **kwargs):
        return self.client.post('/api/work-items/sync-source-record-v1/project-a',
            json={'reference': reference, 'reason': 'original recovery', **kwargs})

    def assert_no_projection(self):
        self.assertEqual(self.repo.load(), {})
        self.assertEqual(self.fixture.host.dispatches, [])
        self.assertFalse(self.fixture.host.WORK_ITEM_EVENTS_FILE.exists())

    def test_closed_github_record_materializes_with_real_source_scope_and_no_dispatch(self):
        result = self.fetch()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['work_item_refs'], ['acme/app#59'])
        state = self.repo.get('acme/app#59')
        self.assertEqual((state.project_id, state.organization_id, state.workspace_id),
            ('project-a', 'local', 'default'))
        self.assertEqual(state.current_stage, 'closed')
        self.assertEqual(state.source_identity.revision, '2026-10-20T00:00:00Z')
        self.assertEqual(state.source_identity.source_instance, self.fixture.config.source_instance)
        self.assertEqual(self.calls, [('GET', '/repos/acme/app/issues/59')])
        self.assertTrue(self.fixture.host.autonomy_paused and self.fixture.host.work_paused)
        self.assertEqual(self.fixture.host.dispatches, [])
        events = [json.loads(line) for line in self.fixture.host.WORK_ITEM_EVENTS_FILE.read_text().splitlines()]
        event = next(e for e in events if e['event_type'] == 'operator_source_sync')
        self.assertEqual(event['actor'], self.fixture.actor.identity_id)

    def gitlab(self):
        f = self.fixture
        secret = f.secrets.create(SecretCreate(name='GitLab fixture', provider='gitlab',
            value='fixture-gitlab-token', allowed_identity_ids=['service-task-source-runtime']), actor=f.actor)
        f.project.authoritative_task_source = TaskSourceConfiguration(source_type='gitlab',
            source_instance='https://gitlab.test/api/v4', scope='acme', credential_secret_id=secret.id)
        f.service.gitlab = GitLabClient(
            transport=httpx.MockTransport(self.respond))

    def test_closed_gitlab_record_uses_group_boundary_and_exact_issue_read(self):
        self.gitlab()
        result = self.fetch()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.repo.get('acme/app#59').current_stage, 'closed')
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all('/issues/59' in path for _, path in self.calls))
        self.assertEqual(self.fixture.host.dispatches, [])

    def test_outside_scope_and_path_ambiguities_never_reach_provider(self):
        for reference in ('other/app#59', 'acme/app-else#59', 'Acme/app#59', '../app#59', 'acme/%2e%2e#59'):
            with self.subTest(reference=reference):
                response = self.fetch(reference)
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.calls, [])
        self.assert_no_projection()

    def test_gitlab_namespace_prefix_is_not_a_group_boundary(self):
        self.gitlab()
        for reference in ('acme-else/app#59', 'acme/../other#59', 'acme/app#59!1', 'acme/app#0'):
            with self.subTest(reference=reference):
                response = self.fetch(reference)
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.calls, [])
        self.assert_no_projection()

    def test_gitlab_merge_request_is_refused_before_provider_read(self):
        self.gitlab()
        result = self.fetch('acme/app!59')
        self.assertEqual(result.status_code, 400, result.text)
        self.assertEqual(result.json()['detail']['code'], 'invalid_source_reference')
        self.assertEqual(self.calls, [])
        self.assert_no_projection()

    def test_github_pull_request_response_is_not_materialized_as_an_issue(self):
        self.pull_request_response = True
        result = self.fetch()
        self.assertEqual(result.status_code, 400, result.text)
        self.assertEqual(result.json()['detail']['code'], 'invalid_source_reference')
        self.assertEqual(self.calls, [('GET', '/repos/acme/app/issues/59')])
        self.assert_no_projection()

    def test_provider_denials_are_not_empty_discovery_or_placeholder_records(self):
        for source in ('github', 'gitlab'):
            if source == 'gitlab': self.gitlab()
            for status in (403, 404, 429, 503):
                with self.subTest(source=source, status=status):
                    self.status = status
                    result = self.fetch()
                    self.assertEqual(result.status_code, 409, result.text)
                    self.assertEqual(result.json()['detail']['provider_status'], status)
                    self.assert_no_projection()

    def test_wrong_provider_identity_is_refused_before_materialization(self):
        self.response_number = 60
        result = self.fetch()
        self.assertEqual(result.status_code, 409, result.text)
        self.assert_no_projection()

    def test_gitlab_wrong_provider_identity_is_not_a_different_import(self):
        self.gitlab()
        self.response_number = 60
        result = self.fetch()
        self.assertEqual(result.status_code, 409, result.text)
        self.assert_no_projection()

    def test_denied_related_artifact_read_does_not_import_an_incomplete_original(self):
        self.gitlab()
        self.related_status = 403
        result = self.fetch()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()['detail']['provider_status'], 403)
        self.assert_no_projection()

    def test_legacy_composition_without_atomic_update_refuses_before_provider_read(self):
        f = self.fixture
        f.service.work_items = replace(f.service.work_items, update_state=None)
        app = FastAPI()
        @app.middleware('http')
        async def actor(request, call_next):
            request.state.identity_actor = f.actor
            request.state.tenant_scope = f.actor.tenant
            return await call_next(request)
        app.include_router(build_work_items_router(f.service))
        with TestClient(app) as client:
            result = client.post('/api/work-items/sync-source-record-v1/project-a', json={'reference': 'acme/app#59'})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()['detail']['code'], 'source_record_atomic_materialization_unavailable')
        self.assertEqual(self.calls, [])
        self.assert_no_projection()

    def test_transport_uncertainty_never_becomes_a_record_or_discovery_fallback(self):
        self.transport_failure = True
        result = self.fetch()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIsNone(result.json()['detail']['provider_status'])
        self.assertEqual(len(self.calls), 1)
        self.assert_no_projection()

    def test_unsupported_scoped_read_is_not_substituted_by_bulk_discovery(self):
        capabilities = TaskSourceCapabilities(frozenset({TaskSourceCapability.DISCOVERY, TaskSourceCapability.READ}))
        with patch.object(GitHubTaskSource, 'capabilities', capabilities):
            result = self.fetch()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()['detail']['capability'], 'scoped_read')
        self.assertEqual(self.calls, [])
        self.assert_no_projection()

    def test_nonadministrator_cannot_materialize_even_an_in_scope_record(self):
        self.fixture.actor = self.fixture.actor.model_copy(update={'roles': (MembershipRole.MEMBER,)})
        result = self.fetch()
        self.assertEqual(result.status_code, 403, result.text)
        self.assertEqual(self.calls, [])
        self.assert_no_projection()

    def existing(self, *, foreign=False):
        return WorkItemState(ref='acme/app#59', project_id='foreign' if foreign else 'project-a',
            organization_id='foreign' if foreign else 'local', workspace_id='default',
            current_owner='james', implementation_owner='james', validation_owner='quinn',
            next_owner='orchestrator', blocker='Retained unknown provider outcome',
            next_action='Reconcile the original; never replay', notes=['original retained input'],
            created_at=1, updated_at=1, last_meaningful_update_at=1)

    def test_existing_ownership_history_and_foreign_scope_are_never_overwritten(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                original = self.existing(foreign=foreign)
                self.repo.put(original.ref, original)
                result = self.fetch()
                self.assertEqual(result.status_code, 409, result.text)
                self.assertEqual(result.json()['detail']['code'], 'source_record_already_materialized')
                self.assertEqual(self.repo.get(original.ref), original)
                self.assertFalse(self.fixture.host.WORK_ITEM_EVENTS_FILE.exists())
        self.assertEqual(self.fixture.host.dispatches, [])

    def test_concurrent_foreign_winner_is_rechecked_inside_canonical_writer(self):
        original = self.existing(foreign=True)
        projector = self.fixture.service.task_source_projector
        deps = projector.dependencies
        def race(ref, updater):
            self.repo.put(original.ref, original)
            return self.repo.update(ref, updater)
        projector.dependencies = replace(deps, update_state=race)
        result = self.fetch()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.repo.get(original.ref), original)
        self.assertFalse(self.fixture.host.WORK_ITEM_EVENTS_FILE.exists())
        self.assertEqual(self.fixture.host.dispatches, [])

    def test_unauthorized_project_and_unknown_request_fields_do_not_read_source(self):
        response = self.client.post('/api/work-items/sync-source-record-v1/foreign', json={'reference': 'acme/app#59'})
        self.assertEqual(response.status_code, 404, response.text)
        response = self.fetch(actor='impersonated')
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.calls, [])
        self.assert_no_projection()
