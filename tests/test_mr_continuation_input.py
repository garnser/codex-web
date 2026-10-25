"""Semantic identity differs from original delivery/freshness provenance."""
import unittest

from codex_web.mr_continuation_input import fact_fingerprint, input_values, semantic_facts
from tests.test_code_host_mr_observation import fact, HEAD


class StableMRInputTests(unittest.TestCase):
    def test_repeat_read_times_urls_provider_check_ids_and_order_are_not_new_input(self):
        original = fact()
        raw = original.model_dump(mode='json')
        raw.update(observed_at=2000, expires_at=2030, web_url='https://example/new-link')
        raw['sources'].reverse()
        for source in raw['sources']:
            source['observed_at'] = 2000
        for check in raw['observed_checks']:
            check.update(observed_at=2000, external_id='different-run-id', web_url='https://example/new-run')
        repeated = type(original).model_validate(raw)
        self.assertEqual(fact_fingerprint(original), fact_fingerprint(repeated))
        self.assertEqual(input_values(original, expected_head=HEAD), input_values(repeated, expected_head=HEAD))
        self.assertEqual(original.observed_at, 1000)  # Original is not rewritten.

    def test_same_head_current_review_check_and_policy_changes_are_material(self):
        original = fact()
        for changes in ({'current_head_approval_count':0}, {'current_head_approval_count':None},
                {'independent_approval_count':None}, {'strict_checks_required':True},
                {'strict_checks_required':False}, {'blockers':('policy_unknown',)},
                {'resource_id':'foreign'}, {'binding_id':'different-binding'},
                {'head_revision':'c'*40}, {'base_revision':'d'*40}):
            changed = original.model_copy(update=changes)
            with self.subTest(changes=changes):
                self.assertNotEqual(fact_fingerprint(original), fact_fingerprint(changed))
        check = original.observed_checks[0].model_copy(update={'state':'pending'})
        self.assertNotEqual(fact_fingerprint(original), fact_fingerprint(original.model_copy(update={'observed_checks':(check,)})))

    def test_unknown_absent_denied_and_false_zero_remain_distinct(self):
        original = fact()
        value = semantic_facts(original)
        self.assertIsNone(value['strict_checks_required'])
        self.assertIsNone(value['requirements_fingerprint'])
        for availability in ('unknown','denied','truncated','complete'):
            source = type(original.sources[0]).model_validate({**original.sources[0].model_dump(mode='json'),'availability':availability})
            projected = semantic_facts(original.model_copy(update={'sources':(source,)}))
            self.assertEqual(projected['sources'][0]['availability'], availability)
        values = [fact_fingerprint(original.model_copy(update={'current_head_approval_count':n})) for n in (None,0,1)]
        self.assertEqual(len(set(values)),3)

    def test_no_head_or_unknown_to_authority_conversion(self):
        original = fact()
        with self.assertRaises(ValueError):input_values(original, expected_head='c'*40)
        with self.assertRaises(ValueError):input_values(original.model_copy(update={'changed':True}), expected_head=HEAD)
        self.assertEqual(set(input_values(original, expected_head=HEAD)),
            {'pull_request_number','expected_head_sha','observed_requirements_fingerprint'})
