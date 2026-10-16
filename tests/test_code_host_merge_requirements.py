import unittest

from pydantic import ValidationError

from codex_web.code_host_merge_requirements import (
    CodeHostMergeRequirementsFact, CodeHostMergeReviewRequirements, CodeHostMergeSourceFact,
    MergeFactAvailability, MergeFactSource,
)
from codex_web.compatibility import ContractCompatibilityError


class MergeRequirementsContractTests(unittest.TestCase):
    def payload(self):
        return dict(resource_id='repository-one', binding_id='binding-one', provider_type='github',
            provider_instance='github.example', external_id='7', head_revision='a' * 40,
            base_revision='b' * 40, target_branch='main', observed_at=1000, expires_at=1030,
            pull_request_state='OPEN', draft=False)

    def complete(self):
        return {**self.payload(), 'complete': True, 'requirements_fingerprint': 'c' * 64,
            'sources': tuple(CodeHostMergeSourceFact(source=source,
                availability=MergeFactAvailability.COMPLETE, provider_source=source.value,
                observed_at=1000, record_count=0) for source in MergeFactSource),
            'strict_checks_required': False, 'reviews': CodeHostMergeReviewRequirements(
                approving_count=0, code_owner_required=False, last_push_approval_required=False,
                dismiss_stale_approvals=False), 'independent_approval_count': 0,
            'current_head_approval_count': 0, 'conversation_resolution_required': False,
            'conversation_count': 0, 'unresolved_conversation_count': 0}

    def test_unavailable_is_distinct_from_explicitly_observed_zero(self):
        unknown = CodeHostMergeRequirementsFact(**self.payload())
        self.assertFalse(unknown.complete)
        self.assertIsNone(unknown.reviews.approving_count)
        self.assertIsNone(unknown.independent_approval_count)
        self.assertIsNone(unknown.unresolved_conversation_count)
        known = CodeHostMergeRequirementsFact(**self.complete())
        self.assertTrue(known.complete)
        self.assertEqual(known.reviews.approving_count, 0)
        self.assertIsNone(known.review_decision)  # Inapplicable native review decision is allowed.

    def test_all_failure_availabilities_prevent_complete_claim(self):
        for status in (MergeFactAvailability.DENIED, MergeFactAvailability.UNSUPPORTED,
                       MergeFactAvailability.TRUNCATED, MergeFactAvailability.UNKNOWN, MergeFactAvailability.CHANGED):
            data = self.complete()
            data['sources'] = tuple(source.model_copy(update={'availability': status})
                                    if source.source == MergeFactSource.EFFECTIVE_RULES else source
                                    for source in data['sources'])
            with self.subTest(status=status), self.assertRaises(ValidationError):
                CodeHostMergeRequirementsFact(**data)

    def test_changed_missing_or_uninterpreted_rules_cannot_be_complete(self):
        for changes in ({'changed': True}, {'requirements_fingerprint': None}, {'sources': ()},
                        {'unsupported_rules': ('future-provider-rule',)}, {'independent_approval_count': None},
                        {'conversation_resolution_required': None}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                CodeHostMergeRequirementsFact(**{**self.complete(), **changes})

    def test_duplicate_sources_and_false_absence_cannot_hide_missing_facts(self):
        source = self.complete()['sources'][0]
        with self.assertRaises(ValidationError):
            CodeHostMergeRequirementsFact(**{**self.payload(), 'sources': (source, source)})
        with self.assertRaises(ValidationError):
            CodeHostMergeSourceFact(source=MergeFactSource.EFFECTIVE_RULES,
                availability=MergeFactAvailability.ABSENT, provider_source='rules', observed_at=1000)
        legacy = CodeHostMergeSourceFact(source=MergeFactSource.LEGACY_PROTECTION,
            availability=MergeFactAvailability.ABSENT, provider_source='protection', observed_at=1000)
        self.assertEqual(legacy.availability, MergeFactAvailability.ABSENT)

    def test_strict_counts_booleans_revisions_time_and_versions_reject_ambiguity(self):
        changes = ({'independent_approval_count': True}, {'draft': 'false'}, {'head_revision': 'abc'},
                   {'expires_at': 1000}, {'observed_at': float('nan')},
                   {'conversation_count': 0, 'unresolved_conversation_count': 1})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                CodeHostMergeRequirementsFact(**{**self.payload(), **change})
        with self.assertRaises(ValidationError) as raised:
            CodeHostMergeRequirementsFact(**{**self.payload(), 'schema_version': '2.0'})
        self.assertIsInstance(raised.exception.errors()[0]['ctx']['error'], ContractCompatibilityError)
