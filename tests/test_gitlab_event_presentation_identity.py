from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace

from codex_web.services.gitlab import GitLabService
from codex_web.services.gitlab_event_presentation import GitLabEventPresentationService
from codex_web.services.work_item_dependencies import gitlab_project_issue_ref


class GitLabEventPresentationIdentityTests(unittest.TestCase):
    def setUp(self):
        self.service = GitLabEventPresentationService(
            delivery=SimpleNamespace(), targets=SimpleNamespace(), telemetry=SimpleNamespace(),
            label_names=lambda payload: [],
            event_url=lambda payload: payload['object_attributes'].get('url'),
        )
        self.payload = {
            'object_kind': 'pipeline',
            'project': {'id': 1, 'path_with_namespace': 'veridataops/platform'},
            'object_attributes': {
                'id': 3431, 'iid': 306, 'ref': 'fix/ci-sibling-contract-acquisition',
                'sha': 'f737' + '0' * 36, 'status': 'failed',
                'url': 'https://gitlab.example/veridataops/platform/-/pipelines/3431',
            },
        }

    def test_pipeline_identifiers_are_not_issue_or_merge_request_references(self):
        rendered = self.service.reference(self.payload)
        self.assertIn('pipeline id=3431, iid=306:', rendered)
        self.assertNotIn('#306', rendered)
        self.assertNotIn('!306', rendered)
        self.assertEqual(GitLabService.reference(None, self.payload), rendered)

    def test_pipeline_prompt_preserves_fact_identity_without_inventing_a_work_item(self):
        original = copy.deepcopy(self.payload)
        prompt = self.service.format_prompt(self.payload, 'quinn')
        self.assertIn('/pipelines/3431', prompt)
        self.assertIn('sha=f73700000000', prompt)
        self.assertIn('not an issue or merge request IID', prompt)
        self.assertNotIn('!306', prompt)
        self.assertNotIn('#306', prompt)
        self.assertNotIn('reconcile the affected work item', prompt)
        self.assertEqual(gitlab_project_issue_ref(self.payload), 'veridataops/platform@pipeline:306')
        self.assertEqual(self.payload, original)

    def test_issue_and_merge_request_iids_keep_their_distinct_task_reference(self):
        for kind, iid, marker in [('issue', 207, '#207'), ('merge_request', 58, '!58')]:
            with self.subTest(kind=kind):
                payload = {**self.payload, 'object_kind': kind,
                           'object_attributes': {'iid': iid, 'title': 'actual work'}}
                reference = self.service.reference(payload)
                self.assertIn(marker, reference)
                self.assertNotIn('!/#', reference)
                self.assertEqual(GitLabService.reference(None, payload), reference)
                self.assertIn('reconcile the affected work item', self.service.format_prompt(payload, 'quinn'))

    def test_explicit_same_project_merge_request_association_is_separate_from_pipeline_iid(self):
        self.payload['merge_request'] = {'iid': 58, 'target_project_id': 1}
        prompt = self.service.format_prompt(self.payload, 'quinn')
        self.assertIn('Associated merge request: veridataops/platform!58', prompt)
        self.assertIn('pipeline id=3431, iid=306', prompt)
        self.assertNotIn('!306', prompt)
        self.assertEqual(gitlab_project_issue_ref(self.payload), 'veridataops/platform@pipeline:306')

    def test_unqualified_associations_do_not_invent_same_project_mr_authority(self):
        for association in (None, {}, {'iid': 58}, {'iid': 58, 'target_project_id': 2},
                            {'iid': True, 'target_project_id': 1}, {'iid': 0, 'target_project_id': 1},
                            {'iid': -1, 'target_project_id': 1}, {'iid': '58/notes', 'target_project_id': 1}):
            with self.subTest(association=association):
                self.payload['merge_request'] = association
                self.assertNotIn('Associated merge request:', self.service.format_prompt(self.payload, 'quinn'))

    def test_missing_project_identity_cannot_qualify_an_associated_mr(self):
        self.payload['merge_request'] = {'iid': 58, 'target_project_id': 1}
        self.payload['project'].pop('id')
        self.assertNotIn('Associated merge request:', self.service.format_prompt(self.payload, 'quinn'))

    def test_pipeline_kind_normalization_does_not_restore_generic_task_mutation(self):
        self.payload['object_kind'] = ' Pipeline '
        prompt = self.service.format_prompt(self.payload, 'quinn')
        self.assertIn('not an issue or merge request IID', prompt)
        self.assertNotIn('!306', prompt)
        self.assertNotIn('reconcile the affected work item', prompt)

    def test_pipeline_notice_uses_pipeline_identifiers_and_status(self):
        notice = self.service.format_notice(self.payload, 'quinn', {'queued': True})
        self.assertIn('pipeline id=3431, iid=306', notice)
        self.assertIn('failed', notice)
        self.assertNotIn('!306', notice)
        self.assertNotIn('#306', notice)

    def test_pipeline_without_iid_retains_global_id_and_never_falls_back_to_task_marker(self):
        self.payload['object_attributes'].pop('iid')
        self.assertIn('pipeline id=3431:', self.service.reference(self.payload))
        self.assertIsNone(gitlab_project_issue_ref(self.payload))


if __name__ == '__main__':
    unittest.main()
