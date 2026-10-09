from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.models import GitLabRoutingSettings, WorkItemState
from codex_web.services.gitlab_artifact_events import GitLabArtifactEventProjector
from codex_web.services.work_item_state import WorkItemStateMachine
from tests.test_task_source_work_item_projection import _Host


class GitLabArtifactResourceAssociationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.host = _Host(Path(temp.name))
        self.host._load_projects = lambda: [SimpleNamespace(
            id='project-a', organization_id='org-a', workspace_id='workspace-a')]
        self.host._load_gitlab_routing_settings = lambda: GitLabRoutingSettings()
        self.host._project_issue_ref = lambda payload: 'group/project!42'
        self.host._gitlab_label_names = lambda payload: []
        self.host._gitlab_owner_agents = lambda payload, settings: []
        self.host._gitlab_url = lambda payload: 'https://gitlab.example/group/project/-/merge_requests/42'
        self.host._gitlab_mr_refs_from_payload = lambda payload: ['group/project!42']
        self.projector = GitLabArtifactEventProjector(self.host, WorkItemStateMachine(self.host))
        self.payload = {
            'object_kind': 'merge_request',
            'project': {'path_with_namespace': 'group/project'},
            'object_attributes': {'iid': 42, 'state': 'opened', 'title': 'MR',
                                  'updated_at': '2026-10-09T04:00:00Z'},
        }

    def assert_associated(self, state):
        self.assertEqual((state.organization_id, state.workspace_id), ('org-a', 'workspace-a'))
        self.assertEqual(state.resource_ids, ['resource-repo'])
        self.assertEqual(self.host.resource_lookup, ('project-a', 'group/project', 'gitlab'))
        saved = self.host.states[state.ref]
        self.assertEqual(saved.resource_ids, ['resource-repo'])
        self.assertEqual(saved.organization_id, 'org-a')

    def test_new_merge_request_uses_project_tenant_and_exact_repository(self):
        self.assert_associated(self.projector.project(self.payload, project_id='project-a'))
        self.assertEqual(self.host.load_calls, 0)
        self.assertEqual(self.host.save_calls, 0)
        self.assertEqual(self.host.put_calls, 1)

    def test_update_repairs_legacy_associations_and_repeat_preserves_them(self):
        self.host.states['group/project!42'] = WorkItemState(
            ref='group/project!42', project_id='project-a', kind='merge_request',
            created_at=1, updated_at=1, last_meaningful_update_at=1)
        for _ in range(2):
            self.assert_associated(self.projector.project(self.payload, project_id='project-a'))

    def test_pipeline_uses_repository_associations_even_when_closed(self):
        self.payload['object_kind'] = 'pipeline'
        state = self.projector.project(self.payload, project_id='project-a')
        self.assert_associated(state)
        self.assertEqual(state.current_stage, 'closed')

    def test_missing_payload_path_resolves_from_merge_request_ref(self):
        self.payload['project'] = {}
        self.assert_associated(self.projector.project(self.payload, project_id='project-a'))

    def test_stale_event_repairs_associations_without_rewinding_lifecycle(self):
        state = self.projector.project(self.payload, project_id='project-a')
        self.host.states[state.ref].resource_ids = []
        self.payload['object_attributes'].update(
            state='closed', title='Stale title', updated_at='2026-10-08T04:00:00Z')
        result = self.projector.project(self.payload, project_id='project-a')
        self.assert_associated(result)
        self.assertEqual(result.current_stage, 'implementation_active')
        self.assertEqual(result.title, 'MR')

    def test_accepted_handoff_retains_lifecycle_while_repairing_associations(self):
        state = self.projector.project(self.payload, project_id='project-a')
        self.host.states[state.ref].resource_ids = []
        self.projector.state_machine._preserve_accepted_handoff_recipient = lambda *args, **kwargs: True
        self.payload['object_attributes']['title'] = 'Ignored title'
        result = self.projector.project(self.payload, project_id='project-a')
        self.assert_associated(result)
        self.assertEqual(result.title, 'MR')

    def test_legacy_bulk_repository_fallback_keeps_associations(self):
        self.host._get_work_item_state_record = None
        self.host._put_work_item_state_record = None
        self.projector = GitLabArtifactEventProjector(self.host, WorkItemStateMachine(self.host))
        self.assert_associated(self.projector.project(self.payload, project_id='project-a'))
        self.assertEqual(self.host.load_calls, 1)
        self.assertEqual(self.host.save_calls, 1)

    def test_unresolved_mapping_does_not_grant_other_project_resources(self):
        self.host._resource_ids_for_project = lambda project_id, **kwargs: []
        self.projector = GitLabArtifactEventProjector(self.host, WorkItemStateMachine(self.host))
        state = self.projector.project(self.payload, project_id='project-a')
        self.assertEqual(state.resource_ids, [])
        self.assertEqual(state.organization_id, 'org-a')
