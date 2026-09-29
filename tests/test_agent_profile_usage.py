from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.agent_profiles import AgentProfileCreate
from codex_web.agent_teams import AgentTeamCreate, AgentTeamLifecycle, AgentTeamLifecycleChange
from codex_web.api.agent_profiles import build_agent_profiles_router
from codex_web.automation_definitions import AutomationDefinition
from codex_web.definitions import DefinitionScope
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.models import QueuedTurn
from codex_web.services.agent_profile_usage import AgentProfileUsageService
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.agent_teams import AgentTeamService
from codex_web.services.automation_definitions import install_automation_definitions
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.storage.agent_teams import AgentTeamStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository


class AgentProfileUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        store = SQLiteStateStore(Path(self.temp.name) / 'state.db')
        self.definitions = DefinitionRegistryService(DefinitionRegistryStore(store))
        self.profiles = AgentProfileService(AgentProfileStore(store), definitions=self.definitions)
        self.teams = AgentTeamService(AgentTeamStore(store), profiles=self.profiles, definitions=self.definitions)
        self.automations = install_automation_definitions(self.definitions)
        self.queues = TurnQueueRepository(store, Path(self.temp.name) / 'queues.json')
        self.actor = AuthenticationActor(identity_id='admin', principal_kind=PrincipalKind.HUMAN,
            organization_id='org-a', workspace_id='ws-a', roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA)
        self.profiles.create(AgentProfileCreate(profile_id='coder', name='Coder'), actor=self.actor)
        self.assignments = []
        self.usage = AgentProfileUsageService(
            teams=self.teams, projects=SimpleNamespace(list=lambda scope: [SimpleNamespace(id='project-a')]),
            automations=self.automations, assignments=lambda actor: self.assignments, queues=self.queues,
        )
        self.profiles.usage_loader = self.usage.snapshot
        app = FastAPI()

        @app.middleware('http')
        async def actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_agent_profiles_router(self.profiles))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def automate(self, lifecycle='enabled'):
        definition = AutomationDefinition.model_validate({
            'name': 'Routine', 'lifecycle': lifecycle, 'trigger': {'type': 'manual'},
            'target': {'kind': 'agent_profile', 'id': 'coder'}, 'instructions': 'Do work',
        })
        draft = self.automations.create_draft('routine', definition, actor_id='admin',
            scope_type=DefinitionScope.PROJECT, scope_id='project-a', reason='test')
        return self.automations.publish_draft(draft.record_id, actor_id='admin', reason='test')

    def archive(self, revision=1):
        return self.client.post('/api/agent-profiles/coder/archive', json={
            'reason': 'Retire unused profile', 'expected_revision': revision,
        })

    def test_active_team_is_reported_and_blocks_archive_until_disabled(self):
        self.teams.create(AgentTeamCreate(team_id='delivery', name='Delivery', leader_profile_id='coder'), actor=self.actor)
        impact = self.client.get('/api/agent-profiles/coder/usage').json()
        self.assertTrue(impact['available'])
        self.assertEqual(impact['blocking_count'], 1)
        self.assertEqual(impact['items'][0]['object_type'], 'team')
        self.assertEqual(self.archive().status_code, 409)
        self.assertEqual(self.profiles.get('coder', actor=self.actor).revision, 1)
        self.teams.lifecycle('delivery', AgentTeamLifecycle.DISABLED, AgentTeamLifecycleChange(reason='Stop new work'), actor=self.actor)
        self.assertEqual(self.archive().status_code, 200)
        restored = self.client.post('/api/agent-profiles/coder/restore', json={'reason': 'Restore', 'expected_revision': 2})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()['item']['revision'], 3)
        revisions = self.profiles.revisions('coder', actor=self.actor)
        self.assertEqual({r.revision for r in revisions}, {1, 2, 3})

    def test_effective_automation_rechecked_after_preview_and_paused_revision_unblocks(self):
        self.assertEqual(self.client.get('/api/agent-profiles/coder/usage').json()['blocking_count'], 0)
        self.automate()
        self.assertEqual(self.archive().status_code, 409)
        self.automate('paused')
        impact = self.client.get('/api/agent-profiles/coder/usage').json()
        self.assertEqual(impact['blocking_count'], 0)
        self.assertEqual(impact['count'], 1)
        self.assertEqual(impact['items'][0]['revision'], 2)
        self.assertEqual(impact['project_ids'], ['project-a'])
        self.assertEqual(self.archive().status_code, 200)

    def test_active_assignment_and_queued_turn_block_without_exposing_input(self):
        binding = SimpleNamespace(profile_id='coder', profile_revision=1)
        self.assignments = [SimpleNamespace(id='run-a', organization_id='org-a', workspace_id='ws-a',
            project_id='project-a', agent_profile=binding, status='running')]
        self.queues.put('thread-a', [QueuedTurn(id='queued-a', thread_id='thread-a', project_id='project-a',
            agent_profile_id='coder', message='PRIVATE INPUT DO NOT PROJECT', created_at=1)])
        impact = self.client.get('/api/agent-profiles/coder/usage')
        self.assertEqual(impact.json()['blocking_count'], 2)
        self.assertNotIn('PRIVATE INPUT', impact.text)
        self.assertEqual(self.queues._snapshots, {})
        self.assertEqual(self.archive().status_code, 409)
        self.assignments[0].status = 'succeeded'
        self.queues.delete('thread-a')
        self.assertEqual(self.archive().status_code, 200)

    def test_usage_failure_stale_revision_and_unauthorized_mutation_fail_closed(self):
        self.profiles.usage_loader = None
        self.assertFalse(self.client.get('/api/agent-profiles/coder/usage').json()['available'])
        self.assertEqual(self.archive().status_code, 409)
        self.profiles.usage_loader = self.usage.snapshot
        self.assertEqual(self.archive(revision=9).status_code, 409)
        self.actor = self.actor.model_copy(update={'identity_id': 'member', 'roles': (MembershipRole.MEMBER,)})
        self.assertEqual(self.archive().status_code, 403)
        self.actor = self.actor.model_copy(update={'organization_id': 'org-b', 'workspace_id': 'ws-b'})
        self.assertEqual(self.client.get('/api/agent-profiles/coder/usage').status_code, 404)

    def test_restricted_team_metadata_and_foreign_consumers_are_not_exposed(self):
        self.teams.create(AgentTeamCreate(team_id='private-team', name='Private label', leader_profile_id='coder',
            allowed_identity_ids=('private-owner',)), actor=self.actor)
        self.actor = self.actor.model_copy(update={'identity_id': 'reader', 'roles': (MembershipRole.MEMBER,)})
        self.assignments = [SimpleNamespace(id='foreign-run', organization_id='other', workspace_id='other',
            project_id='foreign', agent_profile=SimpleNamespace(profile_id='coder', profile_revision=1), status='running')]
        result = self.client.get('/api/agent-profiles/coder/usage')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['restricted_count'], 1)
        self.assertEqual(result.json()['blocking_count'], 1)
        self.assertNotIn('private-team', result.text)
        self.assertNotIn('Private label', result.text)
        self.assertNotIn('foreign-run', result.text)

    def test_restore_does_not_depend_on_consumer_projection_availability(self):
        self.assertEqual(self.archive().status_code, 200)
        self.profiles.usage_loader = None
        restored = self.client.post('/api/agent-profiles/coder/restore', json={
            'reason': 'Recover collaborator', 'expected_revision': 2,
        })
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()['item']['lifecycle'], 'active')

    def test_truncated_display_keeps_all_blockers_and_scan_failure_is_closed(self):
        self.assignments = [SimpleNamespace(id=f'run-{i}', organization_id='org-a', workspace_id='ws-a',
            project_id='project-a', agent_profile=SimpleNamespace(profile_id='coder', profile_revision=1),
            status='running' if i == 104 else 'succeeded') for i in range(105)]
        result = self.client.get('/api/agent-profiles/coder/usage').json()
        self.assertEqual(result['count'], 105)
        self.assertEqual(result['blocking_count'], 1)
        self.assertEqual(len(result['items']), 100)
        self.assertTrue(result['truncated'])
        self.assertEqual(self.archive().status_code, 409)
        self.usage.MAX_CONSUMERS = 1
        self.assertFalse(self.client.get('/api/agent-profiles/coder/usage').json()['available'])
        self.assertEqual(self.archive().status_code, 409)
