"""Normalize observed GitHub policy data without granting merge authority."""
from dataclasses import dataclass

from codex_web.code_host_merge_requirements import CodeHostMergeReviewRequirements, CodeHostRequiredCheckFact
from codex_web.integrations.github_client import GitHubReadIncompleteError


def boolean(value):
    if type(value) is not bool:
        raise GitHubReadIncompleteError("policy_boolean")
    return value


def count(value):
    if type(value) is not int or not 0 <= value <= 100_000:
        raise GitHubReadIncompleteError("policy_count")
    return value


def native_id(value):
    if type(value) is not int or not 0 < value < 2 ** 63:
        raise GitHubReadIncompleteError("provider_id")
    return value


def text(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 1024:
        raise GitHubReadIncompleteError("policy_text")
    return value


def array(value):
    if not isinstance(value, list) or len(value) > 500:
        raise GitHubReadIncompleteError("policy_array")
    return value


def object_(value):
    if not isinstance(value, dict):
        raise GitHubReadIncompleteError("policy_object")
    return value


def app(value):
    if value is None or (type(value) is int and value == -1):
        return None
    if type(value) is not int or value <= 0:
        raise GitHubReadIncompleteError("policy_app")
    return str(value)


@dataclass(frozen=True)
class GitHubMergePolicy:
    checks: tuple[CodeHostRequiredCheckFact, ...]
    strict: bool | None
    reviews: CodeHostMergeReviewRequirements
    conversations: bool | None
    unsupported: tuple[str, ...]


def rule_source(rule):
    return text(f"ruleset:{text(rule.get('ruleset_source_type'))}:{text(rule.get('ruleset_source'))}:{native_id(rule.get('ruleset_id'))}")


def normalize_policy(legacy, rules, *, legacy_known):
    checks = {}
    minimum, owners, last_push, dismiss, conversations, strict = 0, False, False, False, False, False
    unsupported = set()

    def check(context, application, source):
        identity = (text(context), app(application))
        checks.setdefault(identity, set()).add(source)

    if legacy is not None:
        object_(legacy)
        status = legacy.get("required_status_checks")
        if status is not None:
            object_(status)
            strict = boolean(status.get("strict"))
            contexts = array(status.get("contexts"))
            native_checks = status.get("checks")
            if native_checks is None:
                for context in contexts:
                    check(context, None, "legacy_protection")
            else:
                covered = set()
                for item in array(native_checks):
                    object_(item)
                    if "app_id" not in item:
                        raise GitHubReadIncompleteError("policy_app_missing")
                    check(item.get("context"), item["app_id"], "legacy_protection")
                    covered.add(item.get("context"))
                for context in contexts:
                    if text(context) not in covered:
                        check(context, None, "legacy_protection")
        reviews = legacy.get("required_pull_request_reviews")
        if reviews is not None:
            object_(reviews)
            minimum = count(reviews.get("required_approving_review_count"))
            owners = boolean(reviews.get("require_code_owner_reviews"))
            last_push = boolean(reviews.get("require_last_push_approval"))
            dismiss = boolean(reviews.get("dismiss_stale_reviews"))
        resolution = legacy.get("required_conversation_resolution")
        if resolution is not None:
            conversations = boolean(object_(resolution).get("enabled"))
        for field in ("required_signatures", "required_linear_history", "lock_branch"):
            value = legacy.get(field)
            if value is not None and boolean(object_(value).get("enabled")):
                unsupported.add(f"legacy.{field}")
        if legacy.get("restrictions"):
            unsupported.add("legacy.push_restrictions")

    for rule in array(rules):
        object_(rule)
        kind = text(rule.get("type"))
        source = rule_source(rule)
        parameters = rule.get("parameters")
        if kind == "required_status_checks":
            parameters = object_(parameters)
            rule_strict = boolean(parameters.get("strict_required_status_checks_policy"))
            strict = strict or rule_strict
            for item in array(parameters.get("required_status_checks")):
                object_(item)
                # Omission explicitly means any integration in this endpoint's
                # documented schema; malformed present values remain unknown.
                check(item.get("context"), item.get("integration_id"), source)
            unknown = set(parameters) - {"required_status_checks", "strict_required_status_checks_policy", "do_not_enforce_on_create"}
            unsupported.update(f"{kind}.{key}" for key in unknown)
        elif kind == "pull_request":
            parameters = object_(parameters)
            minimum = max(minimum, count(parameters.get("required_approving_review_count")))
            owners = boolean(parameters.get("require_code_owner_review")) or owners
            last_push = boolean(parameters.get("require_last_push_approval")) or last_push
            dismiss = boolean(parameters.get("dismiss_stale_reviews_on_push")) or dismiss
            conversations = boolean(parameters.get("required_review_thread_resolution")) or conversations
            known = {"required_approving_review_count", "require_code_owner_review", "require_last_push_approval",
                     "dismiss_stale_reviews_on_push", "required_review_thread_resolution", "allowed_merge_methods",
                     "required_reviewers", "dismissal_restriction"}
            unsupported.update(f"{kind}.{key}" for key in set(parameters) - known)
            if parameters.get("required_reviewers"):
                unsupported.add("pull_request.required_reviewers")
            methods = parameters.get("allowed_merge_methods")
            if methods is not None and {text(method) for method in array(methods)} != {"merge", "squash", "rebase"}:
                unsupported.add("pull_request.allowed_merge_methods")
        elif kind not in {"creation", "deletion", "non_fast_forward"}:
            # These three ref operations do not constrain a provider PR merge
            # into its existing base. Every other unhandled rule stays visible.
            unsupported.add(kind)
    if len(checks) > 500 or len(unsupported) > 100:
        raise GitHubReadIncompleteError("policy_limit")
    required = tuple(CodeHostRequiredCheckFact(context=context, provider_app_id=application,
        source_ids=tuple(sorted(sources))) for (context, application), sources in
        sorted(checks.items(), key=lambda item: (item[0][0], item[0][1] or "")))
    if not legacy_known:
        return GitHubMergePolicy(required, None, CodeHostMergeReviewRequirements(), None, tuple(sorted(unsupported)))
    return GitHubMergePolicy(required, strict, CodeHostMergeReviewRequirements(approving_count=minimum,
        code_owner_required=owners, last_push_approval_required=last_push, dismiss_stale_approvals=dismiss),
        conversations, tuple(sorted(unsupported)))
