"""Pure exact-head disposition contract shared by verification and ActionIntent."""
import unittest

from codex_web.code_host_merge_requirements import CodeHostMergeRequirementsFact
from codex_web.code_host_mr_observation import dispositions


HEAD = "a" * 40


def fact(**changes):
    values = dict(resource_id="repo", binding_id="binding", provider_type="github",
        provider_instance="example", external_id="7", head_revision=HEAD,
        base_revision="b" * 40, target_branch="main", observed_at=1000,
        expires_at=1030, pull_request_state="OPEN", draft=False,
        sources=[dict(source=x, availability="complete", provider_source=x, observed_at=1000)
                 for x in ("reviews", "checks")],
        review_decision="APPROVED", independent_approval_count=1, current_head_approval_count=1,
        observed_checks=[dict(external_id="1", context="test", revision=HEAD,
            kind="check", state="success", provider_state="completed")])
    values.update(changes)
    return CodeHostMergeRequirementsFact.model_validate(values)


class MRObservationDispositionTests(unittest.TestCase):
    def project(self, **changes):
        return dispositions(fact(**changes), expected_head=HEAD, now=1001)

    def test_positive_observations_do_not_turn_incomplete_policy_into_pass(self):
        self.assertEqual(self.project(), {"independent_review": ("review", "pass"),
            "exact_head_ci": ("ci_check", "pass"), "merge_policy_snapshot": ("policy_evaluation", "warn")})

    def test_no_independent_or_current_head_review_is_acceptance(self):
        for changes in ({"independent_approval_count": 0}, {"current_head_approval_count": 0},
                        {"review_decision": "NOT_APPROVED"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.project(**changes)["independent_review"][1], "fail")
        self.assertEqual(self.project(current_head_approval_count=None)["independent_review"][1], "warn")

    def test_changed_stale_future_and_wrong_head_never_publish_pass(self):
        for value, now in ((fact(changed=True), 1001), (fact(), 1030), (fact(), 999),
                           (fact(head_revision="c" * 40), 1001)):
            with self.subTest(now=now, changed=value.changed):
                with self.assertRaises(ValueError):
                    dispositions(value, expected_head=HEAD, now=now)

    def test_foreign_check_head_is_not_exact_head_ci(self):
        original = fact()
        wrong = original.observed_checks[0].model_copy(update={"revision": "c" * 40})
        with self.assertRaisesRegex(ValueError, "check_head_mismatch"):
            self.project(observed_checks=(wrong,))

    def test_missing_unknown_failed_or_ambiguous_checks_are_not_pass(self):
        original = fact().observed_checks[0]
        self.assertEqual(self.project(observed_checks=())["exact_head_ci"][1], "warn")
        self.assertEqual(self.project(observed_checks=(original.model_copy(update={"state": "pending"}),))["exact_head_ci"][1], "warn")
        self.assertEqual(self.project(observed_checks=(original.model_copy(update={"state": "failure"}),))["exact_head_ci"][1], "fail")
        self.assertEqual(self.project(blockers=("required_checks_unmet_or_ambiguous",))["exact_head_ci"][1], "warn")

    def test_unknown_publisher_cannot_satisfy_a_pinned_required_check(self):
        self.assertEqual(self.project(required_checks=[dict(context="test", provider_app_id="app", source_ids=["rule"])])["exact_head_ci"][1], "warn")
        observed = fact().observed_checks[0].model_copy(update={"provider_app_id": "app", "publisher_known": True})
        self.assertEqual(self.project(required_checks=[dict(context="test", provider_app_id="app", source_ids=["rule"])],
            observed_checks=(observed,))["exact_head_ci"][1], "pass")
