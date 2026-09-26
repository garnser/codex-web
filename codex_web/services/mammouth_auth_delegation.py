from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

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

MAMMOUTH_AUTH_PURPOSE = "mammouth_api_key"
MAMMOUTH_AUTH_PROVIDERS = frozenset({"mammouth", "mammouth-ai"})
MAMMOUTH_WORKER_HOME = "/tmp/mammouth-worker-home"
DEFAULT_MAX_DELEGATION_SECONDS = 15 * 60


class MammouthAuthDelegationError(RuntimeError):
    pass


class MammouthAuthDelegationUnavailableError(MammouthAuthDelegationError):
    pass


class MammouthAuthDelegationStaleError(MammouthAuthDelegationError):
    pass


@dataclass(frozen=True, slots=True)
class MammouthAuthDelegation:
    """Metadata-only Mammouth credential grant bound to one worker fence."""

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
            "worker_home": MAMMOUTH_WORKER_HOME,
            "environment_allowlisted": True,
        }


@dataclass(frozen=True, slots=True)
class MammouthDelegatedLaunch:
    """Ephemeral launch input visible only inside ``SecretBroker.use``."""

    delegation: MammouthAuthDelegation
    command: tuple[str, ...]
    environment: Mapping[str, str]


class MammouthAuthDelegationService:
    """Inject one short-lived Mammouth key into one fenced worker assignment.

    The raw API key is resolved only inside the SecretBroker callback. It is
    never returned by issue/readiness APIs or copied into persisted runtime
    state. The eventual worker adapter must launch this input inside the
    canonical sandbox rather than on the control-plane host.
    """

    def __init__(
        self,
        broker: SecretBroker,
        *,
        executable: str = "mammouth",
        max_delegation_seconds: int = DEFAULT_MAX_DELEGATION_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.broker = broker
        self.executable = executable
        self.max_delegation_seconds = max(30, int(max_delegation_seconds))
        self._clock = clock

    @staticmethod
    def _require_worker_actor(actor: AuthenticationActor) -> None:
        if (
            actor.principal_kind != PrincipalKind.SERVICE
            or "execution-worker:run" not in actor.service_scopes
        ):
            raise MammouthAuthDelegationUnavailableError(
                "Mammouth delegation requires execution-worker:run service authority"
            )
        if "secret:use" not in actor.service_scopes:
            raise MammouthAuthDelegationUnavailableError(
                "Mammouth delegation requires secret:use service authority"
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
            raise MammouthAuthDelegationStaleError(
                "Mammouth delegation requires a claimed or running assignment"
            )
        if (
            assignment.assigned_worker_id != worker_id
            or assignment.fence != fence
            or lease is None
            or lease.worker_id != worker_id
            or lease.fence != fence
            or lease.expires_at <= now
        ):
            raise MammouthAuthDelegationStaleError(
                "Mammouth delegation worker lease/fence is stale or invalid"
            )
        if assignment.deadline_at is not None and assignment.deadline_at <= now:
            raise MammouthAuthDelegationStaleError(
                "Mammouth delegation assignment deadline has expired"
            )

    @staticmethod
    def _is_mammouth_reference(reference: SecretReference) -> bool:
        provider = (reference.provider or "").strip().casefold()
        purpose = (reference.purpose or "").strip().casefold()
        return (
            provider in MAMMOUTH_AUTH_PROVIDERS
            and purpose == MAMMOUTH_AUTH_PURPOSE
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
                raise MammouthAuthDelegationUnavailableError(
                    "assignment secret reference is unavailable to the worker"
                ) from exc
            if self._is_mammouth_reference(reference):
                matches.append(reference)

        if len(matches) != 1:
            raise MammouthAuthDelegationUnavailableError(
                "assignment requires exactly one usable mammouth_api_key secret reference"
            )
        reference = matches[0]
        if reference.status(now) != SecretStatus.ACTIVE:
            raise MammouthAuthDelegationUnavailableError(
                f"Mammouth delegation secret is {reference.status(now).value}"
            )
        if reference.expires_at is None:
            raise MammouthAuthDelegationUnavailableError(
                "Mammouth delegation secret must have an explicit expiry"
            )
        max_expiry = now + self.max_delegation_seconds
        if assignment.deadline_at is not None:
            max_expiry = min(max_expiry, assignment.deadline_at)
        if reference.expires_at > max_expiry:
            raise MammouthAuthDelegationUnavailableError(
                "Mammouth delegation secret lifetime exceeds the assignment delegation window"
            )
        return reference

    def issue(
        self,
        assignment: ExecutionAssignment,
        *,
        worker_id: str,
        fence: int,
        actor: AuthenticationActor,
    ) -> MammouthAuthDelegation:
        self._require_worker_actor(actor)
        now = self._clock()
        self._require_assignment_lease(
            assignment,
            worker_id=worker_id,
            fence=fence,
            now=now,
        )
        reference = self._reference(assignment, actor=actor, now=now)
        return MammouthAuthDelegation(
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
        delegation: MammouthAuthDelegation,
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
            raise MammouthAuthDelegationStaleError(
                "Mammouth delegation assignment changed"
            )
        reference = self._reference(assignment, actor=actor, now=now)
        if (
            reference.id != delegation.secret_id
            or reference.rotation != delegation.secret_rotation
            or float(reference.expires_at or 0) != delegation.expires_at
        ):
            raise MammouthAuthDelegationStaleError(
                "Mammouth delegation credential rotated or changed"
            )
        if delegation.expires_at <= now:
            raise MammouthAuthDelegationStaleError(
                "Mammouth delegation credential expired"
            )

    def use(
        self,
        assignment: ExecutionAssignment,
        *,
        worker_id: str,
        fence: int,
        actor: AuthenticationActor,
        consumer: Callable[[MammouthDelegatedLaunch], T],
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
                MammouthDelegatedLaunch(
                    delegation=delegation,
                    command=(self.executable,),
                    environment={
                        "HOME": MAMMOUTH_WORKER_HOME,
                        "MAMMOUTH_API_KEY": api_key,
                    },
                )
            )

        return self.broker.use(
            delegation.secret_id,
            actor=actor,
            operation="mammouth-worker-auth-delegation",
            context={
                "assignment_id": assignment.id,
                "worker_id": worker_id,
                "fence": fence,
                "secret_rotation": delegation.secret_rotation,
            },
            consumer=launch,
        )
