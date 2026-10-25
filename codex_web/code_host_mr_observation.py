"""Pure fixed-protocol predicates for canonical exact-head MR provider facts.

Mutable thresholds remain current provider/Definition data. Neither an action
receipt nor this projection creates reviewer consent or merge authorization.
"""
from codex_web.code_host_merge_requirements import CodeHostMergeRequirementsFact


def dispositions(fact: CodeHostMergeRequirementsFact, *, expected_head: str, now: float):
    """Return only findings established by this exact, unexpired provider snapshot.

    A complete policy snapshot is not permission to merge. A positive independent
    review observation is not worker-verifier independence or a new QA approval.
    """
    if fact.head_revision != expected_head or fact.changed or now >= fact.expires_at:
        raise ValueError("mr_observation_head_changed_or_expired")
    if now < fact.observed_at:
        raise ValueError("mr_observation_timestamp_in_future")
    sources = {x.source.value: x.availability.value for x in fact.sources}
    complete = lambda name: sources.get(name) == "complete"
    review = "warn"
    if complete("reviews"):
        counts = (fact.current_head_approval_count, fact.independent_approval_count)
        if all(x is not None for x in counts):
            review = ("pass" if all(x > 0 for x in counts)
                      and fact.review_decision in {"APPROVED", "approved"} else "fail")
    checks = "warn"
    observed = fact.observed_checks
    if complete("checks") and observed:
        if any(x.revision != expected_head for x in observed):
            raise ValueError("mr_observation_check_head_mismatch")
        if any(x.state == "failure" for x in observed):
            checks = "fail"
        elif all(x.state == "success" for x in observed):
            # A same-name check from an unknown or different application cannot
            # satisfy a requirement that pins its actual publisher.
            satisfied = all(any(c.context == r.context and
                (r.provider_app_id is None or c.publisher_known and c.provider_app_id == r.provider_app_id)
                for c in observed) for r in fact.required_checks)
            if satisfied and "required_checks_unmet_or_ambiguous" not in fact.blockers:
                checks = "pass"
    return {
        "independent_review": ("review", review),
        "exact_head_ci": ("ci_check", checks),
        "merge_policy_snapshot": ("policy_evaluation", "pass" if fact.complete else "warn"),
    }

