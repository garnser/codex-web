from __future__ import annotations

import dataclasses
import time
from typing import Any, Awaitable, Callable, Iterable, Mapping, TypeVar

from pydantic import BaseModel

from codex_web.identity import AuthenticationActor, MembershipRole, PrincipalKind, TenantScope
from codex_web.secrets import SecretAuditEvent, SecretCreate, SecretReference, SecretRotate, SecretStatus
from codex_web.services.identity import AuthorizationError, TenantIsolationError
from codex_web.storage.secret_state import SecretStateStore
from codex_web.secret_backends import SecretBackend, SecretBackendError


T = TypeVar("T")


class SecretBrokerError(RuntimeError):
    pass


class SecretNotFoundError(SecretBrokerError):
    pass


class SecretUnavailableError(SecretBrokerError):
    pass


class SecretUseDeniedError(AuthorizationError):
    pass


class SecretRevealDeniedError(AuthorizationError):
    pass


def _contains_secret(value: Any, secret: str) -> bool:
    if isinstance(value, str):
        return secret in value
    if isinstance(value, Mapping):
        return any(_contains_secret(item, secret) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_secret(item, secret) for item in value)
    if dataclasses.is_dataclass(value):
        return _contains_secret(dataclasses.asdict(value), secret)
    if isinstance(value, BaseModel):
        return _contains_secret(value.model_dump(mode="python"), secret)
    return False


def scrub_secret(value: Any, secret_values: Iterable[str]) -> Any:
    """Best-effort boundary scrubber for accidental secret propagation."""

    secrets = tuple(sorted({item for item in secret_values if item}, key=len, reverse=True))
    if isinstance(value, str):
        result = value
        for secret in secrets:
            result = result.replace(secret, "[REDACTED]")
        return result
    if isinstance(value, dict):
        return {key: scrub_secret(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_secret(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_secret(item, secrets) for item in value)
    if isinstance(value, BaseModel):
        scrubbed = scrub_secret(value.model_dump(mode="python"), secrets)
        return value.__class__.model_validate(scrubbed)
    return value


class SecretBroker:
    """Resolve credential material only at a bounded execution boundary."""

    def __init__(
        self,
        store: SecretStateStore,
        backends: Mapping[str, SecretBackend],
    ) -> None:
        self.store = store
        self.backends = dict(backends)

    def _reference(self, secret_id: str) -> SecretReference:
        state = self.store.load()
        reference = next((item for item in state.references if item.id == secret_id), None)
        if reference is None:
            raise SecretNotFoundError("secret reference not found")
        return reference

    def _backend(self, reference: SecretReference) -> SecretBackend:
        backend = self.backends.get(reference.backend)
        if backend is None:
            raise SecretUnavailableError(
                f"secret backend {reference.backend!r} is unavailable"
            )
        return backend

    @staticmethod
    def _scope(actor: AuthenticationActor, reference: SecretReference) -> None:
        if (
            actor.organization_id != reference.organization_id
            or actor.workspace_id != reference.workspace_id
        ):
            raise TenantIsolationError("cross-tenant secret access denied")

    @staticmethod
    def _admin(actor: AuthenticationActor) -> bool:
        if actor.principal_kind == PrincipalKind.SERVICE:
            return "secret:admin" in actor.service_scopes
        return actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN)

    @classmethod
    def _can_use(cls, actor: AuthenticationActor, reference: SecretReference) -> bool:
        if cls._admin(actor):
            return True
        if actor.principal_kind == PrincipalKind.SERVICE and "secret:use" not in actor.service_scopes:
            return False
        return actor.identity_id in {
            reference.owner_identity_id,
            *reference.allowed_identity_ids,
        }

    @classmethod
    def _can_reveal(cls, actor: AuthenticationActor, reference: SecretReference) -> bool:
        if actor.principal_kind == PrincipalKind.SERVICE:
            return (
                "secret:reveal" in actor.service_scopes
                and actor.identity_id in reference.reveal_identity_ids
            )
        return (
            cls._admin(actor)
            and actor.identity_id in {
                reference.owner_identity_id,
                *reference.reveal_identity_ids,
            }
        )

    def _audit(
        self,
        reference: SecretReference,
        actor: AuthenticationActor,
        action: str,
        outcome: str,
        *,
        reason: str | None = None,
        context: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> None:
        event = SecretAuditEvent(
            secret_id=reference.id,
            organization_id=reference.organization_id,
            workspace_id=reference.workspace_id,
            actor_identity_id=actor.identity_id,
            actor_kind=actor.principal_kind.value,
            action=action,
            outcome=outcome,
            reason=reason,
            context=dict(context or {}),
        )

        def apply(state):
            state.audit.append(event)
            state.audit = state.audit[-2000:]
            return state

        self.store.update(apply)

    def metadata(
        self,
        secret_id: str,
        *,
        actor: AuthenticationActor,
        require_use: bool = False,
    ) -> SecretReference:
        reference = self._reference(secret_id)
        self._scope(actor, reference)
        if require_use and not self._can_use(actor, reference):
            raise SecretUseDeniedError("secret use authority denied")
        if not require_use and not (
            self._admin(actor)
            or actor.identity_id == reference.owner_identity_id
            or actor.identity_id in reference.allowed_identity_ids
            or actor.identity_id in reference.reveal_identity_ids
        ):
            raise SecretUseDeniedError("secret metadata authority denied")
        return reference.model_copy(deep=True)

    def list(self, actor: AuthenticationActor) -> list[SecretReference]:
        state = self.store.load()
        return [
            item
            for item in state.references
            if item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
            and (
                self._admin(actor)
                or actor.identity_id == item.owner_identity_id
                or actor.identity_id in item.allowed_identity_ids
                or actor.identity_id in item.reveal_identity_ids
            )
        ]

    def create(
        self,
        payload: SecretCreate,
        *,
        actor: AuthenticationActor,
        scope: TenantScope | None = None,
        backend_type: str = "local",
    ) -> SecretReference:
        scope = scope or actor.tenant
        if scope != actor.tenant:
            raise TenantIsolationError("cross-tenant secret creation denied")
        if not self._admin(actor):
            raise SecretUseDeniedError("secret administration authority required")
        backend = self.backends.get(backend_type)
        if backend is None:
            raise SecretUnavailableError(f"secret backend {backend_type!r} is unavailable")

        reference = SecretReference(
            backend=backend_type,
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            name=payload.name,
            provider=payload.provider,
            purpose=payload.purpose,
            owner_identity_id=actor.identity_id,
            allowed_identity_ids=payload.allowed_identity_ids,
            reveal_identity_ids=payload.reveal_identity_ids,
            expires_at=payload.expires_at,
        )
        backend.put(reference.id, payload.value)
        try:
            def apply(state):
                state.references.append(reference)
                return state
            self.store.update(apply)
        except Exception:
            backend.delete(reference.id)
            raise
        self._audit(reference, actor, "create", "succeeded")
        return reference

    def use(
        self,
        secret_id: str,
        *,
        actor: AuthenticationActor,
        operation: str,
        consumer: Callable[[str], T],
        context: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> T:
        reference = self._reference(secret_id)
        self._scope(actor, reference)
        if reference.status() != SecretStatus.ACTIVE:
            self._audit(reference, actor, "use", "denied", reason=reference.status().value)
            raise SecretUseDeniedError(f"secret is {reference.status().value}")
        if not self._can_use(actor, reference):
            self._audit(reference, actor, "use", "denied", reason="authority")
            raise SecretUseDeniedError("secret use authority denied")
        backend = self._backend(reference)
        try:
            value = backend.get(reference.id)
            result = consumer(value)
        except Exception as exc:
            self._audit(
                reference,
                actor,
                "use",
                "failed",
                reason=type(exc).__name__,
                context={"operation": operation, **dict(context or {})},
            )
            raise
        if _contains_secret(result, value):
            scrubbed = scrub_secret(result, (value,))
            if _contains_secret(scrubbed, value):
                self._audit(reference, actor, "use", "failed", reason="secret-escape")
                raise SecretUnavailableError("provider result attempted to expose secret material")
            result = scrubbed
        self._audit(
            reference,
            actor,
            "use",
            "succeeded",
            context={"operation": operation, **dict(context or {})},
        )
        return result

    async def use_async(
        self,
        secret_id: str,
        *,
        actor: AuthenticationActor,
        operation: str,
        consumer: Callable[[str], Awaitable[T]],
        context: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> T:
        reference = self._reference(secret_id)
        self._scope(actor, reference)
        if reference.status() != SecretStatus.ACTIVE:
            self._audit(reference, actor, "use", "denied", reason=reference.status().value)
            raise SecretUseDeniedError(f"secret is {reference.status().value}")
        if not self._can_use(actor, reference):
            self._audit(reference, actor, "use", "denied", reason="authority")
            raise SecretUseDeniedError("secret use authority denied")
        backend = self._backend(reference)
        value = backend.get(reference.id)
        try:
            result = await consumer(value)
        except Exception as exc:
            self._audit(
                reference,
                actor,
                "use",
                "failed",
                reason=type(exc).__name__,
                context={"operation": operation, **dict(context or {})},
            )
            raise
        if _contains_secret(result, value):
            scrubbed = scrub_secret(result, (value,))
            if _contains_secret(scrubbed, value):
                self._audit(reference, actor, "use", "failed", reason="secret-escape")
                raise SecretUnavailableError("provider result attempted to expose secret material")
            result = scrubbed
        self._audit(
            reference,
            actor,
            "use",
            "succeeded",
            context={"operation": operation, **dict(context or {})},
        )
        return result

    def reveal(self, secret_id: str, *, actor: AuthenticationActor) -> str:
        reference = self._reference(secret_id)
        self._scope(actor, reference)
        if reference.status() != SecretStatus.ACTIVE:
            self._audit(reference, actor, "reveal", "denied", reason=reference.status().value)
            raise SecretRevealDeniedError(f"secret is {reference.status().value}")
        if not self._can_reveal(actor, reference):
            self._audit(reference, actor, "reveal", "denied", reason="authority")
            raise SecretRevealDeniedError("secret reveal authority denied")
        value = self._backend(reference).get(reference.id)
        self._audit(reference, actor, "reveal", "succeeded")
        return value

    def rotate(
        self,
        secret_id: str,
        payload: SecretRotate,
        *,
        actor: AuthenticationActor,
    ) -> SecretReference:
        reference = self._reference(secret_id)
        self._scope(actor, reference)
        if not self._admin(actor):
            raise SecretUseDeniedError("secret administration authority required")
        backend = self._backend(reference)
        backend.put(reference.id, payload.value)
        now = time.time()
        updated = reference.model_copy(
            update={
                "rotated_at": now,
                "rotation": reference.rotation + 1,
                "revoked_at": None,
                "revoke_reason": None,
            }
        )

        def apply(state):
            state.references = [
                updated if item.id == secret_id else item
                for item in state.references
            ]
            return state

        self.store.update(apply)
        self._audit(updated, actor, "rotate", "succeeded")
        return updated

    def revoke(
        self,
        secret_id: str,
        *,
        actor: AuthenticationActor,
        reason: str = "revoked",
    ) -> SecretReference:
        reference = self._reference(secret_id)
        self._scope(actor, reference)
        if not self._admin(actor):
            raise SecretUseDeniedError("secret administration authority required")
        updated = reference.model_copy(
            update={
                "revoked_at": reference.revoked_at or time.time(),
                "revoke_reason": reason,
            }
        )

        def apply(state):
            state.references = [
                updated if item.id == secret_id else item
                for item in state.references
            ]
            return state

        self.store.update(apply)
        self._audit(updated, actor, "revoke", "succeeded", reason=reason)
        return updated

    def audit(self, actor: AuthenticationActor) -> list[SecretAuditEvent]:
        if not self._admin(actor):
            raise SecretUseDeniedError("secret administration authority required")
        return [
            item
            for item in self.store.load().audit
            if item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        ]
