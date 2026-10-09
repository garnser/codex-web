from unittest import TestCase
from unittest.mock import Mock

from codex_web.models import WorkItemState
from codex_web.services.workflow_claims import WorkflowClaimPolicy


class WorkflowClaimScalingTests(TestCase):
    def setUp(self):
        self.item = WorkItemState(ref='group/app#1', current_owner='developer', created_at=1, updated_at=1, last_meaningful_update_at=1)
        self.load = Mock(return_value={self.item.ref: self.item})
        self.selected = Mock(return_value={self.item.ref: self.item})
        self.policy = WorkflowClaimPolicy(load_states=self.load, get_states=self.selected,
                                          ensure_defaults=lambda item: item,
                                          coerce_owner=lambda owner: owner,
                                          owner_names=('developer', 'tester'))

    def test_messages_without_references_do_not_access_storage(self):
        self.assertEqual(self.policy.findings('Working on the implementation now.'), ([], []))
        self.load.assert_not_called()
        self.selected.assert_not_called()

    def test_qualified_reference_checks_current_canonical_owner_with_selected_read(self):
        findings, mentioned = self.policy.findings('group/app#1 current_owner=tester')
        self.assertEqual(mentioned, [self.item])
        self.assertEqual(len(findings), 1)
        self.load.assert_not_called()
        self.selected.assert_called_once_with(('group/app#1',))
        self.selected.return_value = {self.item.ref: self.item.model_copy(update={'current_owner': 'tester'})}
        self.assertEqual(self.policy.findings('group/app#1 current_owner=tester')[0], [])

    def test_bare_reference_preserves_cross_repository_ambiguity(self):
        self.load.return_value['other/app#1'] = self.item.model_copy(update={'ref': 'other/app#1'})
        self.assertEqual(self.policy.findings('#1 current_owner=tester'), ([], []))
        self.selected.assert_not_called()
        self.load.assert_called_once()

    def test_mixed_references_keep_full_catalog_ambiguity_checks(self):
        other = self.item.model_copy(update={'ref': 'other/app#2'})
        self.load.return_value[other.ref] = other
        self.assertEqual({s.ref for s in self.policy.mentioned_states('group/app#1 and #2')},
                         {self.item.ref, other.ref})
        self.selected.assert_not_called()
