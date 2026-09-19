from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable, Mapping, TypeVar

from codex_web.execution_workers import AssignmentStatus, ExecutionAssignment
from codex_web.identity import AuthenticationActor, PrincipalKind
from codex_web.secrets import SecretReference, SecretStatus
from codex_web.services.identity import TenantIsolationError
from codex_web.services.secrets import (
    SecretBroker,
    SecretBrokerError,
    SecretUseDeniedError,
)


T = TypeVar("T")

ANTHROPIC_AUTH_PURPOSE = "anthropic_api_key"
ANTHROPIC_AUTH_PROVIDERS = frozenset({"anthropic", "claude"})
CLAUDE_WORKER_HOME = "/tmp/claude-worker-home"
DEFAULT_MAX_DELEGATION_SECONDS = 15 * 60


class AnthropicAuthDelegationError(RuntimeError):
    pass


class AnthropicAuthDelegationUnavailableError(AnthropicAuthDelegationError):
    pass


class AnthropicAuthDelegationStaleError(AnthropicAuthDelegationError):
    pass


@dataclass(frozen=True, slots=True)
class AnthropicAuthDelegation:
    """Metadata-only assignment-bound Anthropic authentication grant."""

    assignment_id: str
    worker_id: str
    fence: int
    secret_id: str
    secret_rotation: int
    issued_at: float
    expires_at: float

    def public(self) -> dict[str, str | int | float | bool | None]:
        return {
            "ready": True,
            "assignment_id": self.assignment_id,
            "worker_id": self.worker_id,
            "fence": self.fence,
            "secret_ref": self.secret_id,
            "secret_rotation": self.secret_rotation,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "credential_store": "ephemeral",
            "worker_home": CLAUDE_WORKER_HOME,
            "child_environment_filtered": True,
        }


@dataclass(frozen=True, slots=True)
class AnthropicDelegatedLaunch:
    """Ephemeral launch input visible only inside SecretBroker.use()."""

    delegation: AnthropicAuthDelegation
    command: tuple[str, ...]
    environment: Mapping[str, str]


class AnthropicAuthDelegationService:
    """Resolve one short-lived Anthropic key for one fenced worker assignment.

    The raw credential exists only in the SecretBroker callback used to launch
    Claude. Claude Code's subprocess scrub is mandatory so Bash/hooks/MCP
    children do not inherit provider or cloud credentials.
    """

    def __init__(
        self,
        broker: SecretBroker,
        *,
        max_delegation_seconds: int = DEFAULT_MAX_DELEGATION_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.broker = broker
        self.max_delegation_seconds = max(30, int(max_delegation_seconds))
        self._clock = clock

    @staticmethod
    def _require_worker_actor(actor: AuthenticationActor) -> None:
        if (
            actor.principal_kind != PrincipalKind.SERVICE
            or "execution-worker:run" not in actor.service_scopes
        ):
            raise AnthropicAuthDelegationUnavailableError(
                "Anthropic delegation requires execution-worker:run service authority"
            )
        if "secret:use" not in actor.service_scopes:
            raise AnthropicAuthDelegationUnavailableError(
                "Anthropic delegation requires secret:use service authority"
            )

    @staticmethod
    def _require_assignment_lease(
        assignment: ExecutionAssignment,
        *,
        worker_id: str,
        fence: int,
        now: float,
    ) -> None:
        lease = assignment.lease
        if assignment.status not in {AssignmentStatus.CLAIMED, AssignmentStatus.RUNNING}:
            raise AnthropicAuthDelegationStaleError(
                "Anthropic delegation requires a claimed or running assignment"
            )
        if (
            assignment.assigned_worker_id != worker_id
            or assignment.fence != fence
            or lease is None
            or lease.worker_id != worker_id
            or lease.fence != fence
            or lease.expires_at <= now
        ):
            raise AnthropicAuthDelegationStaleError(
                "Anthropic delegation worker lease/fence is stale or invalid"
            )
        if assignment.deadline_at is not None and assignment.deadline_at <= now:
            raise AnthropicAuthDelegationStaleError(
                "Anthropic delegation assignment deadline has expired"
            )

    @staticmethod
    def _is_anthropic_reference(reference: SecretReference) -> bool:
        provider = (reference.provider or "").strip().casefold()
        purpose = (reference.purpose or "").strip().casefold()
        return (
            provider in ANTHROPIC_AUTH_PROVIDERS
            and purpose == ANTHROPIC_AUTH_PURPOSE
        )

    def _reference(
        self,
        assignment: ExecutionAssignment,
        *,
        actor: AuthenticationActor,
        now: float,
    ) -> SecretReference:
        matches: list[SecretReference] = []
        for secret_id in assignment.secret_refs:
            try:
                reference = self.broker.metadata(
                    secret_id,
                    actor=actor,
                    require_use=True,
                )
            except (SecretBrokerError, SecretUseDeniedError, TenantIsolationError) as exc:
                raise AnthropicAuthDelegationUnavailableError(
                    "assignment secret reference is unavailable to the worker"
                ) from exc
            if self._is_anthropic_reference(reference):
                matches.append(reference)

        if len(matches) != 1:
            raise AnthropicAuthDelegationUnavailableError(
                "assignment requires exactly one usable anthropic_api_key secret reference"
            )
        reference = matches[0]
        if reference.status(now) != SecretStatus.ACTIVE:
            raise AnthropicAuthDelegationUnavailableError(
                f"Anthropic delegation secret is {reference.status(now).value}"
            )
        if reference.expires_at is None:
            raise AnthropicAuthDelegationUnavailableError(
                "Anthropic delegation secret must have an explicit expiry"
            )
        max_expiry = now + self.max_delegation_seconds
        if assignment.deadline_at is not None:
            max_expiry = min(max_expiry, assignment.deadline_at)
        if reference.expires_at > max_expiry:
            raise AnthropicAuthDelegationUnavailableError(
                "Anthropic delegation secret lifetime exceeds the assignment delegation window"
            )
        return reference

    @staticmethod
    def session_id(assignment_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-web:{assignment_id}"))

    @classmethod
    def command(cls, assignment_id: str) -> tuple[str, ...]:
        return (
            "claude",
            "--bare",
            "--print",
            "--session-id",
            cls.session_id(assignment_id),
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "default",
        )

    def issue(
        self,
        assignment: ExecutionAssignment,
        *,
        worker_id: str,
        fence: int,
        actor: AuthenticationActor,
    ) -> AnthropicAuthDelegation:
        self._require_worker_actor(actor)
        now = self._clock()
        self._require_assignment_lease(
            assignment,
            worker_id=worker_id,
            fence=fence,
            now=now,
        )
        reference = self._reference(assignment, actor=actor, now=now)
        return AnthropicAuthDelegation(
            assignment_id=assignment.id,
            worker_id=worker_id,
            fence=fence,
            secret_id=reference.id,
            secret_rotation=reference.rotation,
            issued_at=now,
            expires_at=float(reference.expires_at),
        )

    def validate_current(
        self,
        delegation: AnthropicAuthDelegation,
        assignment: ExecutionAssignment,
        *,
        actor: AuthenticationActor,
    ) -> None:
        self._require_worker_actor(actor)
        now = self._clock()
        self._require_assignment_lease(
            assignment,
            worker_id=delegation.worker_id,
            fence=delegation.fence,
            now=now,
        )
        if assignment.id != delegation.assignment_id:
            raise AnthropicAuthDelegationStaleError(
                "Anthropic delegation assignment changed"
            )
        reference = self._reference(assignment, actor=actor, now=now)
        if (
            reference.id != delegation.secret_id
            or reference.rotation != delegation.secret_rotation
            or float(reference.expires_at or 0) != delegation.expires_at
        ):
            raise AnthropicAuthDelegationStaleError(
                "Anthropic delegation credential rotated or changed"
            )
        if delegation.expires_at <= now:
            raise AnthropicAuthDelegationStaleError(
                "Anthropic delegation credential expired"
            )

    def use(
        self,
        assignment: ExecutionAssignment,
        *,
        worker_id: str,
        fence: int,
        actor: AuthenticationActor,
        consumer: Callable[[AnthropicDelegatedLaunch], T],
    ) -> T:
        delegation = self.issue(
            assignment,
            worker_id=worker_id,
            fence=fence,
            actor=actor,
        )

        def launch(api_key: str) -> T:
            self.validate_current(delegation, assignment, actor=actor)
            return consumer(
                AnthropicDelegatedLaunch(
                    delegation=delegation,
                    command=self.command(assignment.id),
                    environment={
                        "HOME": CLAUDE_WORKER_HOME,
                        "CLAUDE_CONFIG_DIR": f"{CLAUDE_WORKER_HOME}/.claude",
                        "ANTHROPIC_API_KEY": api_key,
                        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
                    },
                )
            )

        return self.broker.use(
            delegation.secret_id,
            actor=actor,
            operation="anthropic-worker-auth-delegation",
            context={
                "assignment_id": assignment.id,
                "worker_id": worker_id,
                "fence": fence,
                "secret_rotation": delegation.secret_rotation,
            },
            consumer=launch,
        )
