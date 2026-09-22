from __future__ import annotations

import time
from enum import StrEnum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


FAILURE_TAXONOMY_CONTRACT = ContractSpec(
    "failure-taxonomy",
    "1.0",
    ("1.0",),
)


class FailureCategory(StrEnum):
    PROVIDER_MODEL = "provider_model"
    RUNTIME_WORKER = "runtime_worker"
    CONTROL_POLICY = "control_policy"
    EXTERNAL_ACTION = "external_action"
    UNKNOWN = "unknown"


class FailureRetryability(StrEnum):
    TRANSIENT = "transient"
    AFTER_REMEDIATION = "after_remediation"
    RECONCILE_REQUIRED = "reconcile_required"
    NOT_RETRYABLE = "not_retryable"

    def metric_labels(self) -> dict[str, str]:
        return {
            "category": self.category.value,
            "reason": self.reason_code.value,
            "source": self.source_subsystem,
            "retryability": self.retryability.value,
        }

    @property
    def automatic_retry_allowed(self) -> bool:
        return self == FailureRetryability.TRANSIENT


class FailureOutcome(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class FailureReason(StrEnum):
    # Provider / model
    PROVIDER_AUTH_OR_ACCESS = "provider_auth_or_access"
    PROVIDER_QUOTA_EXHAUSTED = "provider_quota_exhausted"
    PROVIDER_CAPACITY_OR_RATE_LIMIT = "provider_capacity_or_rate_limit"
    PROVIDER_SERVER_ERROR = "provider_server_error"
    PROVIDER_NETWORK = "provider_network"
    MODEL_UNAVAILABLE = "model_unavailable"
    CONTEXT_OVERFLOW = "context_overflow"
    MALFORMED_OR_EMPTY_MODEL_OUTPUT = "malformed_or_empty_model_output"

    # Runtime / worker
    RUNTIME_OFFLINE = "runtime_offline"
    RUNTIME_INCOMPATIBLE = "runtime_incompatible"
    RUNTIME_MISSING_EXECUTABLE = "runtime_missing_executable"
    WORKER_LEASE_LOST = "worker_lease_lost"
    WORKER_REVOKED_OR_QUARANTINED = "worker_revoked_or_quarantined"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    RESOURCE_LIMIT = "resource_limit"
    EXECUTION_TIMEOUT = "execution_timeout"
    EXECUTION_STALLED = "execution_stalled"
    WORKSPACE_PREPARE_FAILED = "workspace_prepare_failed"
    PROCESS_FAILURE = "process_failure"

    # Control plane / policy
    AUTHORITY_DENIED = "authority_denied"
    APPROVAL_REQUIRED_OR_EXPIRED = "approval_required_or_expired"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    CONFIGURATION_MISSING_OR_INVALID = "configuration_missing_or_invalid"
    STALE_REVISION = "stale_revision"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    UNKNOWN_OUTCOME = "unknown_outcome"

    # External action
    ACTION_PROVIDER_AUTH = "action_provider_auth"
    ACTION_RATE_LIMIT = "action_rate_limit"
    ACTION_TIMEOUT_UNKNOWN_OUTCOME = "action_timeout_unknown_outcome"
    ACTION_CONFLICT = "action_conflict"
    VERIFICATION_FAILED = "verification_failed"

    # Safe fallback
    UNCLASSIFIED = "unclassified"


class FailureDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason_code: FailureReason
    category: FailureCategory
    retryability: FailureRetryability
    outcome: FailureOutcome = FailureOutcome.KNOWN
    remediation_key: str
    default_summary: str


_DEFINITIONS: dict[FailureReason, FailureDefinition] = {}


def _define(
    reason: FailureReason,
    category: FailureCategory,
    retryability: FailureRetryability,
    remediation_key: str,
    summary: str,
    *,
    outcome: FailureOutcome = FailureOutcome.KNOWN,
) -> None:
    _DEFINITIONS[reason] = FailureDefinition(
        reason_code=reason,
        category=category,
        retryability=retryability,
        outcome=outcome,
        remediation_key=remediation_key,
        default_summary=summary,
    )


# Provider / model
_define(
    FailureReason.PROVIDER_AUTH_OR_ACCESS,
    FailureCategory.PROVIDER_MODEL,
    FailureRetryability.AFTER_REMEDIATION,
    "provider.credentials",
    "Provider authentication or access was denied.",
)
_define(
    FailureReason.PROVIDER_QUOTA_EXHAUSTED,
    FailureCategory.PROVIDER_MODEL,
    FailureRetryability.AFTER_REMEDIATION,
    "provider.quota",
    "Provider quota or credits are exhausted.",
)
_define(
    FailureReason.PROVIDER_CAPACITY_OR_RATE_LIMIT,
    FailureCategory.PROVIDER_MODEL,
    FailureRetryability.TRANSIENT,
    "provider.capacity",
    "Provider capacity or rate limiting prevented execution.",
)
_define(
    FailureReason.PROVIDER_SERVER_ERROR,
    FailureCategory.PROVIDER_MODEL,
    FailureRetryability.TRANSIENT,
    "provider.status",
    "Provider returned a transient server failure.",
)
_define(
    FailureReason.PROVIDER_NETWORK,
    FailureCategory.PROVIDER_MODEL,
    FailureRetryability.TRANSIENT,
    "provider.network",
    "Provider network connectivity failed.",
)
_define(
    FailureReason.MODEL_UNAVAILABLE,
    FailureCategory.PROVIDER_MODEL,
    FailureRetryability.AFTER_REMEDIATION,
    "model.availability",
    "The selected model is unavailable.",
)
_define(
    FailureReason.CONTEXT_OVERFLOW,
    FailureCategory.PROVIDER_MODEL,
    FailureRetryability.AFTER_REMEDIATION,
    "context.reduce",
    "The execution context exceeded the model limit.",
)
_define(
    FailureReason.MALFORMED_OR_EMPTY_MODEL_OUTPUT,
    FailureCategory.PROVIDER_MODEL,
    FailureRetryability.TRANSIENT,
    "model.output",
    "The model returned malformed or empty output.",
)

# Runtime / worker
_define(
    FailureReason.RUNTIME_OFFLINE,
    FailureCategory.RUNTIME_WORKER,
    FailureRetryability.TRANSIENT,
    "runtime.reconnect",
    "The selected runtime is offline.",
)
_define(
    FailureReason.RUNTIME_INCOMPATIBLE,
    FailureCategory.RUNTIME_WORKER,
    FailureRetryability.AFTER_REMEDIATION,
    "runtime.upgrade",
    "The runtime is incompatible with the execution contract.",
)
_define(
    FailureReason.RUNTIME_MISSING_EXECUTABLE,
    FailureCategory.RUNTIME_WORKER,
    FailureRetryability.AFTER_REMEDIATION,
    "runtime.install",
    "A required runtime executable is missing.",
)
_define(
    FailureReason.WORKER_LEASE_LOST,
    FailureCategory.RUNTIME_WORKER,
    FailureRetryability.TRANSIENT,
    "worker.reassign",
    "The execution worker lease was lost.",
)
_define(
    FailureReason.WORKER_REVOKED_OR_QUARANTINED,
    FailureCategory.RUNTIME_WORKER,
    FailureRetryability.AFTER_REMEDIATION,
    "worker.remediate",
    "The execution worker is revoked or quarantined.",
)
_define(
    FailureReason.SANDBOX_UNAVAILABLE,
    FailureCategory.RUNTIME_WORKER,
    FailureRetryability.AFTER_REMEDIATION,
    "runtime.sandbox",
    "The required sandbox or isolation mode is unavailable.",
)
_define(
    FailureReason.RESOURCE_LIMIT,
    FailureCategory.RUNTIME_WORKER,
    FailureRetryability.AFTER_REMEDIATION,
    "runtime.resources",
    "A runtime resource limit prevented execution.",
)
_define(
    FailureReason.EXECUTION_TIMEOUT,
    FailureCategory.RUNTIME_WORKER,
    FailureRetryability.TRANSIENT,
    "execution.timeout",
    "Execution exceeded its time limit.",
)
_define(
    FailureReason.EXECUTION_STALLED,
    FailureCategory.RUNTIME_WORKER,
    FailureRetryability.TRANSIENT,
    "execution.stalled",
    "Execution stopped making progress.",
)
_define(
    FailureReason.WORKSPACE_PREPARE_FAILED,
    FailureCategory.RUNTIME_WORKER,
    FailureRetryability.AFTER_REMEDIATION,
    "workspace.prepare",
    "The execution workspace could not be prepared.",
)
_define(
    FailureReason.PROCESS_FAILURE,
    FailureCategory.RUNTIME_WORKER,
    FailureRetryability.TRANSIENT,
    "runtime.process",
    "The execution process failed.",
)

# Control plane / policy
_define(
    FailureReason.AUTHORITY_DENIED,
    FailureCategory.CONTROL_POLICY,
    FailureRetryability.AFTER_REMEDIATION,
    "authority.review",
    "Canonical authority denied the operation.",
)
_define(
    FailureReason.APPROVAL_REQUIRED_OR_EXPIRED,
    FailureCategory.CONTROL_POLICY,
    FailureRetryability.AFTER_REMEDIATION,
    "approval.review",
    "A required approval is missing or expired.",
)
_define(
    FailureReason.BUDGET_EXHAUSTED,
    FailureCategory.CONTROL_POLICY,
    FailureRetryability.AFTER_REMEDIATION,
    "budget.review",
    "The execution budget is exhausted.",
)
_define(
    FailureReason.CAPABILITY_UNAVAILABLE,
    FailureCategory.CONTROL_POLICY,
    FailureRetryability.AFTER_REMEDIATION,
    "capability.configure",
    "A required capability is unavailable.",
)
_define(
    FailureReason.DEPENDENCY_BLOCKED,
    FailureCategory.CONTROL_POLICY,
    FailureRetryability.AFTER_REMEDIATION,
    "dependency.remediate",
    "A required dependency is blocked.",
)
_define(
    FailureReason.CONFIGURATION_MISSING_OR_INVALID,
    FailureCategory.CONTROL_POLICY,
    FailureRetryability.AFTER_REMEDIATION,
    "configuration.fix",
    "Required configuration is missing or invalid.",
)
_define(
    FailureReason.STALE_REVISION,
    FailureCategory.CONTROL_POLICY,
    FailureRetryability.TRANSIENT,
    "state.refresh",
    "The operation used a stale canonical revision.",
)
_define(
    FailureReason.CANCELLED,
    FailureCategory.CONTROL_POLICY,
    FailureRetryability.NOT_RETRYABLE,
    "execution.cancelled",
    "The operation was cancelled.",
)
_define(
    FailureReason.SUPERSEDED,
    FailureCategory.CONTROL_POLICY,
    FailureRetryability.NOT_RETRYABLE,
    "execution.superseded",
    "The operation was superseded by newer work.",
)
_define(
    FailureReason.UNKNOWN_OUTCOME,
    FailureCategory.CONTROL_POLICY,
    FailureRetryability.RECONCILE_REQUIRED,
    "execution.reconcile",
    "The operation outcome is unknown and requires reconciliation.",
    outcome=FailureOutcome.UNKNOWN,
)

# External action
_define(
    FailureReason.ACTION_PROVIDER_AUTH,
    FailureCategory.EXTERNAL_ACTION,
    FailureRetryability.AFTER_REMEDIATION,
    "action.credentials",
    "External action provider authentication failed.",
)
_define(
    FailureReason.ACTION_RATE_LIMIT,
    FailureCategory.EXTERNAL_ACTION,
    FailureRetryability.TRANSIENT,
    "action.capacity",
    "External action provider rate limiting prevented execution.",
)
_define(
    FailureReason.ACTION_TIMEOUT_UNKNOWN_OUTCOME,
    FailureCategory.EXTERNAL_ACTION,
    FailureRetryability.RECONCILE_REQUIRED,
    "action.reconcile",
    "External action timed out with an unknown provider outcome.",
    outcome=FailureOutcome.UNKNOWN,
)
_define(
    FailureReason.ACTION_CONFLICT,
    FailureCategory.EXTERNAL_ACTION,
    FailureRetryability.AFTER_REMEDIATION,
    "action.conflict",
    "External action conflicted with provider state.",
)
_define(
    FailureReason.VERIFICATION_FAILED,
    FailureCategory.EXTERNAL_ACTION,
    FailureRetryability.AFTER_REMEDIATION,
    "action.verify",
    "External action verification failed.",
)
_define(
    FailureReason.UNCLASSIFIED,
    FailureCategory.UNKNOWN,
    FailureRetryability.NOT_RETRYABLE,
    "failure.inspect",
    "An unclassified failure occurred.",
)


_SECRET_KEY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "credential",
    "private_key",
    "privatekey",
)


def _safe_detail_key(key: str) -> bool:
    lowered = key.casefold().replace("-", "_")
    return not any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS)


def sanitize_failure_details(
    details: Mapping[str, Any] | None,
    *,
    max_fields: int = 24,
    max_string: int = 256,
) -> dict[str, str | int | float | bool | None]:
    result: dict[str, str | int | float | bool | None] = {}
    if not details:
        return result
    for raw_key, raw_value in details.items():
        if len(result) >= max_fields:
            break
        key = str(raw_key).strip()
        if not key or not _safe_detail_key(key):
            continue
        if raw_value is None or isinstance(raw_value, (bool, int, float)):
            result[key] = raw_value
        elif isinstance(raw_value, str):
            result[key] = raw_value[:max_string]
        else:
            result[key] = str(type(raw_value).__name__)[:max_string]
    return result


class FailureRecord(BaseModel):
    """Versioned, secret-safe canonical failure classification."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: str = FAILURE_TAXONOMY_CONTRACT.current
    category: FailureCategory
    reason_code: FailureReason
    source_subsystem: str = Field(min_length=1)
    retryability: FailureRetryability
    outcome: FailureOutcome = FailureOutcome.KNOWN
    summary: str = Field(min_length=1, max_length=500)
    remediation_key: str = Field(min_length=1, max_length=120)
    correlation_id: str | None = None
    causation_id: str | None = None
    provider_id: str | None = None
    runtime_id: str | None = None
    worker_id: str | None = None
    assignment_id: str | None = None
    execution_id: str | None = None
    action_intent_id: str | None = None
    source_native_code: str | None = None
    source_native_status: str | int | None = None
    evidence_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    attempt: int = Field(default=1, ge=1)
    first_occurred_at: float = Field(default_factory=time.time)
    last_occurred_at: float = Field(default_factory=time.time)
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract(self) -> "FailureRecord":
        FAILURE_TAXONOMY_CONTRACT.require(self.schema_version)
        definition = failure_definition(self.reason_code)
        if self.category != definition.category:
            raise ValueError("failure category does not match reason-code contract")
        if self.retryability != definition.retryability:
            raise ValueError("failure retryability does not match reason-code contract")
        if self.outcome != definition.outcome:
            raise ValueError("failure outcome does not match reason-code contract")
        object.__setattr__(
            self,
            "details",
            sanitize_failure_details(self.details),
        )
        object.__setattr__(
            self,
            "evidence_ids",
            tuple(dict.fromkeys(item for item in self.evidence_ids if item)),
        )
        object.__setattr__(
            self,
            "artifact_ids",
            tuple(dict.fromkeys(item for item in self.artifact_ids if item)),
        )
        return self

    @property
    def automatic_retry_allowed(self) -> bool:
        return self.retryability.automatic_retry_allowed

    @property
    def requires_reconciliation(self) -> bool:
        return self.retryability == FailureRetryability.RECONCILE_REQUIRED


def failure_taxonomy_snapshot() -> dict[str, Any]:
    return {
        "contract": FAILURE_TAXONOMY_CONTRACT.name,
        "version": FAILURE_TAXONOMY_CONTRACT.current,
        "reasons": [
            definition.model_dump(mode="json")
            for definition in sorted(
                _DEFINITIONS.values(),
                key=lambda item: item.reason_code.value,
            )
        ],
    }


def failure_definition(
    reason_code: FailureReason | str,
) -> FailureDefinition:
    try:
        reason = (
            reason_code
            if isinstance(reason_code, FailureReason)
            else FailureReason(str(reason_code))
        )
    except ValueError:
        reason = FailureReason.UNCLASSIFIED
    return _DEFINITIONS[reason]


def create_failure(
    reason_code: FailureReason | str,
    *,
    source_subsystem: str,
    summary: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    provider_id: str | None = None,
    runtime_id: str | None = None,
    worker_id: str | None = None,
    assignment_id: str | None = None,
    execution_id: str | None = None,
    action_intent_id: str | None = None,
    source_native_code: str | None = None,
    source_native_status: str | int | None = None,
    evidence_ids: tuple[str, ...] = (),
    artifact_ids: tuple[str, ...] = (),
    attempt: int = 1,
    occurred_at: float | None = None,
    details: Mapping[str, Any] | None = None,
) -> FailureRecord:
    definition = failure_definition(reason_code)
    timestamp = time.time() if occurred_at is None else float(occurred_at)
    return FailureRecord(
        category=definition.category,
        reason_code=definition.reason_code,
        source_subsystem=str(source_subsystem or "unknown").strip() or "unknown",
        retryability=definition.retryability,
        outcome=definition.outcome,
        summary=(summary or definition.default_summary)[:500],
        remediation_key=definition.remediation_key,
        correlation_id=correlation_id,
        causation_id=causation_id,
        provider_id=provider_id,
        runtime_id=runtime_id,
        worker_id=worker_id,
        assignment_id=assignment_id,
        execution_id=execution_id,
        action_intent_id=action_intent_id,
        source_native_code=(
            str(source_native_code)[:120]
            if source_native_code is not None
            else None
        ),
        source_native_status=source_native_status,
        evidence_ids=evidence_ids,
        artifact_ids=artifact_ids,
        attempt=max(1, int(attempt)),
        first_occurred_at=timestamp,
        last_occurred_at=timestamp,
        details=sanitize_failure_details(details),
    )


def failure_from_exception(
    exc: Exception,
    *,
    source_subsystem: str,
    default_reason: FailureReason = FailureReason.UNCLASSIFIED,
    **context: Any,
) -> FailureRecord:
    reason = getattr(exc, "reason_code", None)
    if reason is None:
        if isinstance(exc, TimeoutError):
            reason = FailureReason.EXECUTION_TIMEOUT
        elif isinstance(exc, FileNotFoundError):
            reason = FailureReason.RUNTIME_MISSING_EXECUTABLE
        else:
            reason = default_reason
    return create_failure(
        reason,
        source_subsystem=source_subsystem,
        source_native_code=type(exc).__name__,
        source_native_status=getattr(
            exc,
            "source_native_status",
            getattr(exc, "status_code", None),
        ),
        **context,
    )


def action_failure_reason(
    native_code: str | int | None,
) -> FailureReason:
    normalized = str(native_code or "").strip().casefold()
    if normalized in {
        "401",
        "403",
        "auth",
        "unauthorized",
        "forbidden",
        "invalid_auth",
        "access_denied",
    }:
        return FailureReason.ACTION_PROVIDER_AUTH
    if normalized in {
        "429",
        "rate_limit",
        "rate_limited",
        "ratelimited",
        "throttled",
    }:
        return FailureReason.ACTION_RATE_LIMIT
    if normalized in {
        "409",
        "conflict",
        "already_exists",
        "revision_conflict",
    }:
        return FailureReason.ACTION_CONFLICT
    return FailureReason.UNCLASSIFIED


def worker_failure_reason(
    native_code: str | int | None,
) -> FailureReason:
    normalized = str(native_code or "").strip().casefold()
    if normalized in {
        "worker_lease_expired",
        "lease_lost",
        "lease_expired",
        "fence_mismatch",
        "stale_fence",
    }:
        return FailureReason.WORKER_LEASE_LOST
    if normalized in {
        "runtime_offline",
        "worker_offline",
        "worker_lost",
    }:
        return FailureReason.RUNTIME_OFFLINE
    if normalized in {
        "worker_revoked",
        "worker_quarantined",
        "revoked",
        "quarantined",
    }:
        return FailureReason.WORKER_REVOKED_OR_QUARANTINED
    if normalized in {
        "sandbox_unavailable",
        "sandbox_failed",
        "isolation_unavailable",
    }:
        return FailureReason.SANDBOX_UNAVAILABLE
    if normalized in {
        "resource_limit",
        "oom",
        "out_of_memory",
        "disk_limit",
        "cpu_limit",
    }:
        return FailureReason.RESOURCE_LIMIT
    if normalized in {
        "timeout",
        "execution_timeout",
        "timed_out",
    }:
        return FailureReason.EXECUTION_TIMEOUT
    if normalized in {
        "stalled",
        "execution_stalled",
        "no_progress",
    }:
        return FailureReason.EXECUTION_STALLED
    if normalized in {
        "workspace_prepare_failed",
        "workspace_failed",
        "checkout_failed",
    }:
        return FailureReason.WORKSPACE_PREPARE_FAILED
    if normalized in {
        "missing_executable",
        "runtime_missing_executable",
        "command_not_found",
    }:
        return FailureReason.RUNTIME_MISSING_EXECUTABLE
    if normalized in {
        "runtime_incompatible",
        "contract_incompatible",
        "unsupported_version",
    }:
        return FailureReason.RUNTIME_INCOMPATIBLE
    if normalized in {
        "capability_unavailable",
        "missing_capability",
    }:
        return FailureReason.CAPABILITY_UNAVAILABLE
    if normalized in {"cancelled", "canceled"}:
        return FailureReason.CANCELLED
    if normalized in {"superseded"}:
        return FailureReason.SUPERSEDED
    if normalized in {
        "process_failure",
        "process_exit",
        "nonzero_exit",
        "non_zero_exit",
    }:
        return FailureReason.PROCESS_FAILURE
    return FailureReason.UNCLASSIFIED


def legacy_failure_reason(
    category: str | None,
    code: str | None,
) -> FailureReason:
    raw_code = str(code or "").strip()
    if raw_code:
        with_value = raw_code.casefold()
        try:
            return FailureReason(with_value)
        except ValueError:
            pass
        action = action_failure_reason(with_value)
        if action != FailureReason.UNCLASSIFIED:
            return action
        worker = worker_failure_reason(with_value)
        if worker != FailureReason.UNCLASSIFIED:
            return worker
        if with_value in {
            "rate_limited",
            "ratelimited",
            "quota",
            "quota_exhausted",
        }:
            return (
                FailureReason.PROVIDER_QUOTA_EXHAUSTED
                if "quota" in with_value
                else FailureReason.PROVIDER_CAPACITY_OR_RATE_LIMIT
            )
        if with_value in {"auth", "unauthorized", "forbidden"}:
            return FailureReason.PROVIDER_AUTH_OR_ACCESS
        if with_value in {"context_overflow", "context_length"}:
            return FailureReason.CONTEXT_OVERFLOW
    normalized_category = str(category or "").strip().casefold()
    if normalized_category in {"authority", "policy"}:
        return FailureReason.AUTHORITY_DENIED
    if normalized_category in {"approval"}:
        return FailureReason.APPROVAL_REQUIRED_OR_EXPIRED
    if normalized_category in {"provider", "model"}:
        return FailureReason.MODEL_UNAVAILABLE
    if normalized_category in {"runtime", "worker"}:
        return FailureReason.PROCESS_FAILURE
    if normalized_category in {"action", "external_action"}:
        return FailureReason.UNKNOWN_OUTCOME
    return FailureReason.UNCLASSIFIED


def aggregate_failure(
    previous: FailureRecord | None,
    current: FailureRecord,
) -> FailureRecord:
    if previous is None:
        return current
    if (
        previous.reason_code != current.reason_code
        or previous.source_subsystem != current.source_subsystem
        or previous.execution_id != current.execution_id
    ):
        return current
    return current.model_copy(
        update={
            "attempt": max(previous.attempt + 1, current.attempt),
            "first_occurred_at": min(
                previous.first_occurred_at,
                current.first_occurred_at,
            ),
            "last_occurred_at": max(
                previous.last_occurred_at,
                current.last_occurred_at,
            ),
        }
    )
