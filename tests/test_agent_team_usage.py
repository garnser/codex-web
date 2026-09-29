from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.agent_profiles import AgentProfileCreate
from codex_web.agent_teams import AgentTeamCreate, AgentTeamDelegationRecord
from codex_web.api.agent_teams import build_agent_teams_router
from codex_web.automation_definitions import AutomationDefinition
from codex_web.definitions import DefinitionScope
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.models import QueuedTurn
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.agent_teams import AgentTeamService
from codex_web.services.agent_team_usage import AgentTeamUsageService
from codex_web.services.automation_definitions import install_automation_definitions
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.storage.agent_teams import AgentTeamStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository


class AgentTeamUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        store = SQLiteStateStore(Path(self.temp.name) / 'state.db')
        definitions = DefinitionRegistryService(DefinitionRegistryStore(store))
        profiles = AgentProfileService(AgentProfileStore(store), definitions=definitions)
        self.teams = AgentTeamService(AgentTeamStore(store), profiles=profiles, definitions=definitions)
        self.automations = install_automation_definitions(definitions)
        self.queues = TurnQueueRepository(store, Path(self.temp.name) / 'queues.json')
        self.actor = AuthenticationActor(identity_id='admin', principal_kind=PrincipalKind.HUMAN,
            organization_id='org-a', workspace_id='ws-a', roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA)
        profiles.create(AgentProfileCreate(profile_id='leader', name='Leader'), actor=self.actor)
        self.team = self.teams.create(AgentTeamCreate(team_id='delivery', name='Delivery', leader_profile_id='leader'), actor=self.actor)
        self.assignments = []
        self.usage = AgentTeamUsageService(teams=self.teams,
            projects=SimpleNamespace(list=lambda scope: [SimpleNamespace(id='project-a')]),
            automations=self.automations, assignments=lambda actor: self.assignments, queues=self.queues)
        self.teams.usage_loader = self.usage.snapshot
        app = FastAPI()

        @app.middleware('http')
        async def actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_agent_teams_router(self.teams))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def change(self, action='archive', revision=1):
        return self.client.post(f'/api/agent-teams/delivery/{action}', json={
            'reason': 'Review Team lifecycle', 'expected_revision': revision,
        })

    def automate(self, lifecycle='enabled'):
        definition = AutomationDefinition.model_validate({
            'name': 'Team routine', 'lifecycle': lifecycle, 'trigger': {'type': 'manual'},
            'target': {'kind': 'team', 'id': 'delivery'}, 'instructions': 'Do work',
        })
        draft = self.automations.create_draft('routine', definition, actor_id='admin',
            scope_type=DefinitionScope.PROJECT, scope_id='project-a', reason='test')
        self.automations.publish_draft(draft.record_id, actor_id='admin', reason='test')

    def delegate(self):
        self.teams.store.append_delegation(AgentTeamDelegationRecord(
            organization_id='org-a', workspace_id='ws-a', team_id='delivery', team_revision=1,
            work_item_id='work-a', project_id='project-a', event_type='execution_linked',
            mode='deterministic', reason='Canonical execution', dedupe_key='work-a',
            coordinator_execution_id='coordinator', member_execution_ids={'leader': 'member'},
        ))

    def test_enabled_automation_blocks_and_latest_paused_revision_unblocks(self):
        self.assertEqual(self.client.get('/api/agent-teams/delivery/usage').json()['blocking_count'], 0)
        self.automate()
        self.assertEqual(self.change().status_code, 409)
        self.automate('paused')
        impact = self.client.get('/api/agent-teams/delivery/usage').json()
        self.assertEqual(impact['count'], 1)
        self.assertEqual(impact['blocking_count'], 0)
        self.assertEqual(impact['items'][0]['revision'], 2)
        archived = self.change()
        self.assertEqual(archived.status_code, 200)
        self.assertEqual(archived.json()['item']['budgets'], self.team.budgets.model_dump(mode='json'))
        self.assertEqual(archived.json()['item']['leader_profile_id'], 'leader')

    def test_linked_running_and_queued_executions_block_without_exposing_messages(self):
        self.delegate()
        self.assignments = [SimpleNamespace(id='assignment', execution_id='coordinator',
            organization_id='org-a', workspace_id='ws-a', project_id='project-a', status='running')]
        self.queues.put('thread-a', [QueuedTurn(id='queued', thread_id='thread-a', project_id='project-a',
            execution_id='member', message='PRIVATE QUEUED CONTENT', created_at=1)])
        result = self.client.get('/api/agent-teams/delivery/usage')
        self.assertEqual(result.json()['blocking_count'], 2)
        self.assertEqual(result.json()['count'], 3)
        self.assertNotIn('PRIVATE QUEUED', result.text)
        self.assertEqual(self.change('disable').status_code, 409)
        self.assignments[0].status = 'succeeded'
        self.queues.delete('thread-a')
        self.assertEqual(self.change('disable').status_code, 200)
        self.assertEqual(self.client.get('/api/agent-teams/delivery/usage').json()['blocking_count'], 0)

    def test_unlinked_or_foreign_assignments_do_not_become_team_consumers(self):
        self.delegate()
        self.assignments = [SimpleNamespace(id='foreign', execution_id='coordinator',
            organization_id='other', workspace_id='other', project_id='other', status='running'),
            SimpleNamespace(id='unrelated', execution_id='unrelated', organization_id='org-a',
                workspace_id='ws-a', project_id='project-a', status='running')]
        result = self.client.get('/api/agent-teams/delivery/usage')
        self.assertEqual(result.json()['count'], 1)
        self.assertEqual(result.json()['blocking_count'], 0)
        self.assertNotIn('foreign', result.text)
        self.assertNotIn('unrelated', result.text)

    def test_missing_impact_and_stale_revision_block_but_restore_preserves_history(self):
        self.teams.usage_loader = None
        self.assertEqual(self.change().status_code, 409)
        self.teams.usage_loader = self.usage.snapshot
        self.assertEqual(self.change(revision=9).status_code, 409)
        self.assertEqual(self.change().status_code, 200)
        self.teams.usage_loader = None
        restored = self.change('restore', revision=2)
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()['item']['revision'], 3)
        self.assertEqual(len(self.client.get('/api/agent-teams/delivery/revisions').json()['items']), 3)

    def test_team_visibility_and_mutation_authority_are_enforced(self):
        self.actor = self.actor.model_copy(update={'identity_id': 'member', 'roles': (MembershipRole.MEMBER,)})
        self.assertEqual(self.change().status_code, 403)
        self.actor = self.actor.model_copy(update={'organization_id': 'other', 'workspace_id': 'other'})
        self.assertEqual(self.client.get('/api/agent-teams/delivery/usage').status_code, 404)
        self.assertEqual(self.change().status_code, 404)
