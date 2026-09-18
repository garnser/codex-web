from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Mapping, TypeVar

from codex_web.execution_workers import AssignmentStatus, ExecutionAssignment
from codex_web.identity import AuthenticationActor, PrincipalKind
from codex_web.secrets import SecretReference, SecretStatus
from codex_web.services.secrets import (
    SecretBroker,
    SecretBrokerError,
    SecretUseDeniedError,
)


T = TypeVar("T")

CODEX_AUTH_PURPOSE = "codex_access_token"
CODEX_AUTH_PROVIDERS = frozenset({"codex", "openai"})
CODEX_WORKER_HOME = "/tmp/codex-worker-home"
DEFAULT_MAX_DELEGATION_SECONDS = 15 * 60


class CodexAuthDelegationError(RuntimeError):
    pass


class CodexAuthDelegationUnavailableError(CodexAuthDelegationError):
    pass


class CodexAuthDelegationStaleError(CodexAuthDelegationError):
    pass


@dataclass(frozen=True, slots=True)
class CodexAuthDelegation:
    """Metadata-only assignment-bound Codex authentication grant."""

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
            "worker_home": CODEX_WORKER_HOME,
            "child_environment_filtered": True,
        }


@dataclass(frozen=True, slots=True)
class CodexDelegatedLaunch:
    """Ephemeral launch input visible only inside SecretBroker.use()."""

    delegation: CodexAuthDelegation
    command: tuple[str, ...]
    environment: Mapping[str, str]


class CodexAuthDelegationService:
    """Resolve one short-lived Codex access token for one fenced worker assignment.

    The secret value never leaves SecretBroker.use(). The launch callback receives
    it only long enough to start the trusted Codex process. Codex is forced to
    keep credentials in memory and to strip token/key/secret variables from
    repository child commands.
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
            raise CodexAuthDelegationUnavailableError(
                "Codex delegation requires execution-worker:run service authority"
            )
        if "secret:use" not in actor.service_scopes:
            raise CodexAuthDelegationUnavailableError(
                "Codex delegation requires secret:use service authority"
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
            raise CodexAuthDelegationStaleError(
                "Codex delegation requires a claimed or running assignment"
            )
        if (
            assignment.assigned_worker_id != worker_id
            or assignment.fence != fence
            or lease is None
            or lease.worker_id != worker_id
            or lease.fence != fence
            or lease.expires_at <= now
        ):
            raise CodexAuthDelegationStaleError(
                "Codex delegation worker lease/fence is stale or invalid"
            )
        if assignment.deadline_at is not None and assignment.deadline_at <= now:
            raise CodexAuthDelegationStaleError(
                "Codex delegation assignment deadline has expired"
            )

    @staticmethod
    def _is_codex_reference(reference: SecretReference) -> bool:
        provider = (reference.provider or "").strip().casefold()
        purpose = (reference.purpose or "").strip().casefold()
        return (
            provider in CODEX_AUTH_PROVIDERS
            and purpose == CODEX_AUTH_PURPOSE
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
            except (SecretBrokerError, SecretUseDeniedError) as exc:
                raise CodexAuthDelegationUnavailableError(
                    "assignment secret reference is unavailable to the worker"
                ) from exc
            if self._is_codex_reference(reference):
                matches.append(reference)

        if len(matches) != 1:
            raise CodexAuthDelegationUnavailableError(
                "assignment requires exactly one usable codex_access_token secret reference"
            )
        reference = matches[0]
        if reference.status(now) != SecretStatus.ACTIVE:
            raise CodexAuthDelegationUnavailableError(
                f"Codex delegation secret is {reference.status(now).value}"
            )
        if reference.expires_at is None:
            raise CodexAuthDelegationUnavailableError(
                "Codex delegation secret must have an explicit expiry"
            )
        max_expiry = now + self.max_delegation_seconds
        if assignment.deadline_at is not None:
            max_expiry = min(max_expiry, assignment.deadline_at)
        if reference.expires_at > max_expiry:
            raise CodexAuthDelegationUnavailableError(
                "Codex delegation secret lifetime exceeds the assignment delegation window"
            )
        return reference

    @staticmethod
    def command(*, subcommand: tuple[str, ...] = ("app-server",)) -> tuple[str, ...]:
        """Return a Codex invocation that keeps auth memory-only and strips child secrets."""
        return (
            "codex",
            "--config",
            'cli_auth_credentials_store="ephemeral"',
            "--config",
            'shell_environment_policy.inherit="none"',
            "--config",
            "shell_environment_policy.ignore_default_excludes=false",
            "--config",
            'shell_environment_policy.set={PATH="/usr/local/bin:/usr/bin:/bin",HOME="/tmp/codex-worker-home"}',
            "--config",
            'shell_environment_policy.filters.CODEX_ACCESS_TOKEN="exclude"',
            "--config",
            'shell_environment_policy.filters.CODEX_API_KEY="exclude"',
            "--config",
            'shell_environment_policy.filters.OPENAI_API_KEY="exclude"',
            *subcommand,
        )

    def issue(
        self,
        assignment: ExecutionAssignment,
        *,
        worker_id: str,
        fence: int,
        actor: AuthenticationActor,
    ) -> CodexAuthDelegation:
        self._require_worker_actor(actor)
        now = self._clock()
        self._require_assignment_lease(
            assignment,
            worker_id=worker_id,
            fence=fence,
            now=now,
        )
        reference = self._reference(assignment, actor=actor, now=now)
        return CodexAuthDelegation(
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
        delegation: CodexAuthDelegation,
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
            raise CodexAuthDelegationStaleError(
                "Codex delegation assignment changed"
            )
        reference = self._reference(assignment, actor=actor, now=now)
        if (
            reference.id != delegation.secret_id
            or reference.rotation != delegation.secret_rotation
            or float(reference.expires_at or 0) != delegation.expires_at
        ):
            raise CodexAuthDelegationStaleError(
                "Codex delegation credential rotated or changed"
            )
        if delegation.expires_at <= now:
            raise CodexAuthDelegationStaleError(
                "Codex delegation credential expired"
            )

    def status(
        self,
        assignment: ExecutionAssignment,
        *,
        worker_id: str,
        fence: int,
        actor: AuthenticationActor,
    ) -> dict[str, str | int | float | bool | None]:
        try:
            return self.issue(
                assignment,
                worker_id=worker_id,
                fence=fence,
                actor=actor,
            ).public()
        except CodexAuthDelegationError as exc:
            return {
                "ready": False,
                "assignment_id": assignment.id,
                "worker_id": worker_id,
                "fence": fence,
                "reason": str(exc),
                "credential_store": "ephemeral",
                "worker_home": CODEX_WORKER_HOME,
                "child_environment_filtered": True,
            }

    def use(
        self,
        assignment: ExecutionAssignment,
        *,
        worker_id: str,
        fence: int,
        actor: AuthenticationActor,
        consumer: Callable[[CodexDelegatedLaunch], T],
        subcommand: tuple[str, ...] = ("app-server",),
    ) -> T:
        delegation = self.issue(
            assignment,
            worker_id=worker_id,
            fence=fence,
            actor=actor,
        )

        def launch(access_token: str) -> T:
            self.validate_current(delegation, assignment, actor=actor)
            return consumer(
                CodexDelegatedLaunch(
                    delegation=delegation,
                    command=self.command(subcommand=subcommand),
                    environment={
                        "CODEX_HOME": CODEX_WORKER_HOME,
                        "CODEX_ACCESS_TOKEN": access_token,
                    },
                )
            )

        return self.broker.use(
            delegation.secret_id,
            actor=actor,
            operation="codex-worker-auth-delegation",
            context={
                "assignment_id": assignment.id,
                "worker_id": worker_id,
                "fence": fence,
                "secret_rotation": delegation.secret_rotation,
            },
            consumer=launch,
        )
