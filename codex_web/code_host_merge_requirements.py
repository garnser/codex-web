"""Versioned read projection for the existing CodeHost provider boundary.

There is no persisted requirements catalog or merge authorization here. Provider
observations expire; the canonical merge ActionIntent and provider enforcement
remain authoritative when a mutation is attempted.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from codex_web.compatibility import ContractSpec


CODE_HOST_MERGE_REQUIREMENTS_CONTRACT = ContractSpec("code-host-merge-requirements", "1.0", ("1.0",))
_Count = Annotated[StrictInt, Field(ge=0, le=100_000)]
_Revision = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
_Reference = Annotated[str, Field(min_length=1, max_length=1024)]


class MergeFactSource(StrEnum):
    PULL_REQUEST = "pull_request"
    LEGACY_PROTECTION = "legacy_protection"
    EFFECTIVE_RULES = "effective_rules"
    CHECKS = "checks"
    REVIEWS = "reviews"
    CONVERSATIONS = "conversations"


class MergeFactAvailability(StrEnum):
    COMPLETE = "complete"
    ABSENT = "absent"
    DENIED = "denied"
    UNSUPPORTED = "unsupported"
    TRUNCATED = "truncated"
    UNKNOWN = "unknown"
    CHANGED = "changed"


class _Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CodeHostMergeSourceFact(_Fact):
    source: MergeFactSource
    availability: MergeFactAvailability
    provider_source: _Reference
    observed_at: float = Field(ge=0, allow_inf_nan=False)
    record_count: _Count | None = None
    reason_code: _Reference | None = None
    rule_ids: tuple[_Reference, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def explicit_absence(self):
        if self.availability == MergeFactAvailability.ABSENT and self.source != MergeFactSource.LEGACY_PROTECTION:
            raise ValueError("only established absence of legacy protection uses absent; empty collections are complete")
        return self


class CodeHostRequiredCheckFact(_Fact):
    context: _Reference
    # None means the observed requirement explicitly allows any publisher.
    provider_app_id: _Reference | None = None
    source_ids: tuple[_Reference, ...] = Field(min_length=1, max_length=100)


class CodeHostMergeCheckFact(_Fact):
    external_id: _Reference
    context: _Reference
    revision: _Revision
    kind: Literal["check", "status", "pipeline"]
    state: Literal["success", "failure", "pending", "unknown"]
    provider_state: _Reference
    provider_conclusion: _Reference | None = None
    provider_app_id: _Reference | None = None
    publisher_known: StrictBool = False
    observed_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    web_url: str | None = Field(default=None, max_length=2048)


class CodeHostMergeReviewRequirements(_Fact):
    approving_count: _Count | None = None
    code_owner_required: StrictBool | None = None
    last_push_approval_required: StrictBool | None = None
    dismiss_stale_approvals: StrictBool | None = None


class CodeHostMergeRequirementsFact(_Fact):
    schema_version: str = CODE_HOST_MERGE_REQUIREMENTS_CONTRACT.current
    resource_id: _Reference
    binding_id: _Reference
    provider_type: _Reference
    provider_instance: _Reference
    external_id: _Reference
    web_url: str | None = Field(default=None, max_length=2048)
    head_revision: _Revision
    base_revision: _Revision
    target_branch: _Reference
    observed_at: float = Field(ge=0, allow_inf_nan=False)
    expires_at: float = Field(ge=0, allow_inf_nan=False)
    requirements_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    sources: tuple[CodeHostMergeSourceFact, ...] = Field(default=(), max_length=6)
    complete: StrictBool = False
    changed: StrictBool = False
    pull_request_state: _Reference
    draft: StrictBool
    mergeable: _Reference | None = None
    merge_state: _Reference | None = None
    review_decision: _Reference | None = None
    required_checks: tuple[CodeHostRequiredCheckFact, ...] = Field(default=(), max_length=500)
    observed_checks: tuple[CodeHostMergeCheckFact, ...] = Field(default=(), max_length=1000)
    strict_checks_required: StrictBool | None = None
    legacy_admin_enforced: StrictBool | None = None
    reviews: CodeHostMergeReviewRequirements = Field(default_factory=CodeHostMergeReviewRequirements)
    independent_approval_count: _Count | None = None
    current_head_approval_count: _Count | None = None
    conversation_resolution_required: StrictBool | None = None
    conversation_count: _Count | None = None
    unresolved_conversation_count: _Count | None = None
    unsupported_rules: tuple[_Reference, ...] = Field(default=(), max_length=100)
    blockers: tuple[_Reference, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def consistent_observation(self):
        CODE_HOST_MERGE_REQUIREMENTS_CONTRACT.require(self.schema_version)
        if self.expires_at <= self.observed_at:
            raise ValueError("requirements observation must have a finite positive freshness window")
        source_names = [item.source for item in self.sources]
        if len(source_names) != len(set(source_names)):
            raise ValueError("requirements sources must be unique")
        if self.unresolved_conversation_count is not None and self.conversation_count is not None:
            if self.unresolved_conversation_count > self.conversation_count:
                raise ValueError("unresolved conversations exceed observed conversations")
        if self.complete:
            if (set(source_names) != set(MergeFactSource) or self.changed or self.unsupported_rules
                    or self.requirements_fingerprint is None
                    or any(item.availability not in {MergeFactAvailability.COMPLETE, MergeFactAvailability.ABSENT}
                           for item in self.sources)):
                raise ValueError("incomplete or changed sources cannot establish complete requirements")
            required_values = (self.strict_checks_required, self.reviews.approving_count,
                self.reviews.code_owner_required, self.reviews.last_push_approval_required,
                self.reviews.dismiss_stale_approvals, self.independent_approval_count,
                self.current_head_approval_count, self.conversation_resolution_required,
                self.conversation_count, self.unresolved_conversation_count)
            if any(value is None for value in required_values):
                raise ValueError("complete requirements need explicit counts and applicability")
        return self
