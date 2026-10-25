"""Fresh source governance on the real Memory retrieval and audit path."""
import unittest
from unittest.mock import patch

from codex_web.data_governance import DataClassification, GovernedDataLifecycle
from codex_web.organizational_memory import KnowledgeQuery
from codex_web.services.data_governance import DataGovernanceService
from tests import test_ux_briefs as canonical


class UxLeafGovernanceTests(unittest.TestCase):
    setUp = canonical.UxBriefMemoryServiceTests.setUp
    tearDown = canonical.UxBriefMemoryServiceTests.tearDown
    create_memory = canonical.UxBriefMemoryServiceTests.create_memory
    create_brief = canonical.UxBriefMemoryServiceTests.create_brief

    def prepare(self):
        source = self.create_memory('leaf-source')
        brief = self.create_brief(source)
        return source, brief

    def query(self, brief):
        return self.service.search(KnowledgeQuery(knowledge_ids=(brief.id,),
            project_ids=('project-a',)), actor=self.actor)

    def change(self, identity, **changes):
        self.governance.store.update(lambda state: state.model_copy(update={
            'records': [row.model_copy(update=changes) if row.id == identity else row
                        for row in state.records]}))

    def test_real_search_leaf_uses_two_governance_observations_and_keeps_audit(self):
        _source, brief = self.prepare()
        with patch.object(self.governance.store, 'context_state',
                wraps=self.governance.store.context_state) as selected:
            result = self.query(brief)
        self.assertEqual([row.knowledge_id for row in result.items], [brief.id])
        self.assertEqual(selected.call_count, 2)  # Initial candidates, then fresh sources.
        audit = self.service.retrieval_run(result.retrieval_id, actor=self.actor)
        self.assertEqual(audit.selected_knowledge_ids, (brief.id,))

    def after_initial_filter(self, mutation):
        original = self.governance.store.context_state
        first = True
        def run(*args, **kwargs):
            nonlocal first
            result = original(*args, **kwargs)
            if first:
                first = False
                mutation()
            return result
        return patch.object(self.governance.store, 'context_state', side_effect=run)

    def test_tightening_after_initial_filter_is_not_hidden_by_a_snapshot_cache(self):
        source, brief = self.prepare()
        with self.after_initial_filter(lambda: self.change(source.governance_record_id,
                deny_model_context=True)):
            result = self.query(brief)
        self.assertEqual(result.items, ())
        audit = self.service.retrieval_run(result.retrieval_id, actor=self.actor)
        self.assertEqual(audit.denied[0].reason,
                         'governance:brief_source_context_denied')

    def test_new_parent_after_initial_filter_uses_the_original_ancestry_validation(self):
        source, brief = self.prepare()
        parent = self.create_memory('new-parent', deny_model_context=True)
        with self.after_initial_filter(lambda: self.change(source.governance_record_id,
                source_record_ids=(parent.governance_record_id,))):
            result = self.query(brief)
        self.assertEqual(result.items, ())

    def test_missing_and_foreign_source_governance_still_deny(self):
        source, brief = self.prepare()
        for mode in ('foreign', 'missing'):
            with self.subTest(mode=mode):
                if mode == 'foreign':
                    self.change(source.governance_record_id, workspace_id='foreign')
                else:
                    self.governance.store.update(lambda state: state.model_copy(update={
                        'records': [row for row in state.records
                                    if row.id != source.governance_record_id]}))
                self.assertEqual(self.query(brief).items, ())

    def test_leaf_classification_and_lifecycle_use_the_same_filter_rules(self):
        source, brief = self.prepare()
        self.change(source.governance_record_id, classification=DataClassification.SECRET)
        self.assertEqual(self.query(brief).items, ())
        self.change(source.governance_record_id, classification=DataClassification.INTERNAL,
                    lifecycle=GovernedDataLifecycle.REDACTED)
        self.assertEqual(self.query(brief).items, ())

    def test_cycle_keeps_original_visited_set_behavior(self):
        source, brief = self.prepare()
        parent = self.create_memory('cycle-parent')
        self.change(source.governance_record_id, source_record_ids=(parent.governance_record_id,))
        self.change(parent.governance_record_id, source_record_ids=(source.governance_record_id,))
        with patch.object(self.governance, 'filter_leaf_context', None):
            expected = self.query(brief)
        actual = self.query(brief)
        self.assertEqual(actual.model_dump(exclude={'retrieval_id'}),
                         expected.model_dump(exclude={'retrieval_id'}))
        self.assertEqual([row.knowledge_id for row in actual.items], [brief.id])

    def test_legacy_backend_and_custom_governance_keep_their_original_path(self):
        _source, brief = self.prepare()
        with patch.object(self.governance.store.store, 'document_array_items_envelope', None):
            actual = self.query(brief)
        with patch.object(self.governance, 'filter_leaf_context', None):
            expected = self.query(brief)
        self.assertEqual(actual.model_dump(exclude={'retrieval_id'}),
                         expected.model_dump(exclude={'retrieval_id'}))

    def test_subclass_and_instance_filter_overrides_cannot_be_bypassed(self):
        source, brief = self.prepare()
        original = self.governance
        def additional_rule(result, request):
            if source.governance_record_id not in request.record_ids:
                return result
            return result.model_copy(update={'allowed_record_ids': (),
                'denied_record_ids': request.record_ids,
                'decisions': tuple(row.model_copy(update={'allowed': False,
                    'reason': 'custom_source_denial'}) for row in result.decisions)})
        class Custom(DataGovernanceService):
            def filter_context(self, request, *, actor):
                return additional_rule(super().filter_context(request, actor=actor), request)
        self.service.governance = Custom(original.store)
        self.assertEqual(self.query(brief).items, ())
        self.service.governance = original
        canonical_filter = original.filter_context
        with patch.object(original, 'filter_context', side_effect=lambda request, *, actor:
                additional_rule(canonical_filter(request, actor=actor), request)):
            self.assertEqual(self.query(brief).items, ())

    def test_inherited_unmodified_filter_keeps_the_leaf_optimization(self):
        _source, brief = self.prepare()
        class Inherited(DataGovernanceService):
            pass
        self.service.governance = Inherited(self.governance.store)
        with patch.object(self.governance.store, 'context_state',
                wraps=self.governance.store.context_state) as selected:
            result = self.query(brief)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(selected.call_count, 2)

    def test_custom_source_record_reader_cannot_be_bypassed(self):
        source, brief = self.prepare()
        original = self.governance
        reads = []
        class Custom(DataGovernanceService):
            def get_record(self, identity, actor, *, context_only=False):
                reads.append(identity)
                if identity == source.governance_record_id:
                    raise RuntimeError('custom source denial')
                return super().get_record(identity, actor, context_only=context_only)
        self.service.governance = Custom(original.store)
        self.assertEqual(self.query(brief).items, ())
        self.assertIn(source.governance_record_id, reads)
        self.service.governance = original
        with patch.object(original, 'get_record', side_effect=RuntimeError('custom source denial')) as read:
            self.assertEqual(self.query(brief).items, ())
            read.assert_called_once_with(source.governance_record_id, self.actor, context_only=True)

    def test_reader_bound_to_another_service_keeps_its_denial(self):
        from pathlib import Path
        from codex_web.storage.data_governance import DataGovernanceStore
        from codex_web.storage.sqlite_state import SQLiteStateStore
        source, brief = self.prepare()
        other = DataGovernanceService(DataGovernanceStore(
            SQLiteStateStore(Path(self.temp.name) / 'other-governance.sqlite3')))
        # Same canonical function, different receiver and current source state.
        self.assertIs(other.get_record.__func__, self.governance.get_record.__func__)
        with patch.object(other.store, 'context_state', wraps=other.store.context_state) as read:
            with patch.object(self.governance, 'get_record', other.get_record):
                self.assertEqual(self.query(brief).items, ())
            read.assert_called_once_with((source.governance_record_id,),
                organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id)
