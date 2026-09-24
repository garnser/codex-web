from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
import threading
from dataclasses import dataclass
from typing import Iterable

from fastapi import HTTPException

from codex_web.identity import (
    ASSURANCE_RANK,
    DEFAULT_HUMAN_IDENTITY_ID,
    DEFAULT_ORGANIZATION_ID,
    DEFAULT_WORKSPACE_ID,
    AuthenticationActor,
    AuthenticationAssurance,
    ExternalAuthenticationResult,
    ExternalIdentityLink,
    HumanIdentity,
    HumanUserCreate,
    IdentityState,
    Membership,
    MembershipRole,
    MembershipUpdate,
    Organization,
    PrincipalKind,
    ServiceIdentity,
    ServiceTokenCredentials,
    ServiceTokenRecord,
    SessionCredentials,
    SessionRecord,
    TenantScope,
    Workspace,
)
from codex_web.storage.identity_state import IdentityStateStore


class IdentityError(RuntimeError):
    pass


class AuthenticationError(IdentityError):
    pass


class AuthorizationError(IdentityError):
    pass


class TenantIsolationError(AuthorizationError):
    pass


class TokenReplayError(AuthenticationError):
    pass


class AuthenticationRateLimiter:
    """Small deterministic brute-force guard for authentication adapters.

    Provider-specific login/OIDC handlers can share this boundary. Successful
    authentication clears failures; failure state is deliberately not authority.
    """

    def __init__(self, *, max_failures: int = 8, window_seconds: int = 300) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _active(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        return [value for value in self._failures.get(key, []) if value >= cutoff]

    def check(self, key: str) -> None:
        now = time.time()
        with self._lock:
            active = self._active(key, now)
            self._failures[key] = active
            if len(active) >= self.max_failures:
                raise AuthenticationError("authentication temporarily rate limited")

    def failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            active = self._active(key, now)
            active.append(now)
            self._failures[key] = active

    def success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def _token() -> str:
    return secrets.token_urlsafe(32)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _matches(value: str, digest: str) -> bool:
    return hmac.compare_digest(_hash(value), digest)


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    actor: AuthenticationActor
    csrf_token_hash: str


class IdentityService:
    """Canonical human/service identity and tenant-bound authentication service.

    Authentication proves an identity. Membership grants codex-web authority.
    External IdP claims and service credentials never become authorization by
    themselves.
    """

    def __init__(self, store: IdentityStateStore) -> None:
        self.store = store

    def bootstrap_local(self) -> IdentityState:
        """Create deterministic single-user defaults for existing installations."""

        def apply(state: IdentityState) -> IdentityState:
            if not any(item.id == DEFAULT_ORGANIZATION_ID for item in state.organizations):
                state.organizations.append(
                    Organization(id=DEFAULT_ORGANIZATION_ID, name="Local")
                )
            if not any(item.id == DEFAULT_WORKSPACE_ID for item in state.workspaces):
                state.workspaces.append(
                    Workspace(
                        id=DEFAULT_WORKSPACE_ID,
                        organization_id=DEFAULT_ORGANIZATION_ID,
                        name="Default",
                    )
                )
            if not any(item.id == DEFAULT_HUMAN_IDENTITY_ID for item in state.humans):
                state.humans.append(
                    HumanIdentity(
                        id=DEFAULT_HUMAN_IDENTITY_ID,
                        display_name="Local Administrator",
                    )
                )
            if not any(
                item.identity_id == DEFAULT_HUMAN_IDENTITY_ID
                and item.organization_id == DEFAULT_ORGANIZATION_ID
                and item.workspace_id == DEFAULT_WORKSPACE_ID
                and item.revoked_at is None
                for item in state.memberships
            ):
                state.memberships.append(
                    Membership(
                        identity_id=DEFAULT_HUMAN_IDENTITY_ID,
                        principal_kind=PrincipalKind.HUMAN,
                        organization_id=DEFAULT_ORGANIZATION_ID,
                        workspace_id=DEFAULT_WORKSPACE_ID,
                        roles=[MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.APPROVER],
                    )
                )
            return state

        return self.store.update(apply)

    def state(self) -> IdentityState:
        return self.store.load()

    @staticmethod
    def _active_memberships(
        state: IdentityState,
        identity_id: str,
        scope: TenantScope,
        *,
        principal_kind: PrincipalKind | None = None,
    ) -> list[Membership]:
        return [
            item
            for item in state.memberships
            if item.identity_id == identity_id
            and (principal_kind is None or item.principal_kind == principal_kind)
            and item.organization_id == scope.organization_id
            and item.workspace_id in {None, scope.workspace_id}
            and item.revoked_at is None
        ]

    @classmethod
    def _actor(
        cls,
        state: IdentityState,
        *,
        identity_id: str,
        principal_kind: PrincipalKind,
        scope: TenantScope,
        assurance: AuthenticationAssurance,
        session_id: str | None = None,
        service_token_id: str | None = None,
        service_scopes: Iterable[str] = (),
    ) -> AuthenticationActor:
        memberships = cls._active_memberships(
            state,
            identity_id,
            scope,
            principal_kind=principal_kind,
        )
        if not memberships:
            raise TenantIsolationError("identity has no active membership in requested workspace")
        roles = tuple(
            dict.fromkeys(role for item in memberships for role in item.roles)
        )
        team_ids = tuple(
            sorted({team_id for item in memberships for team_id in item.team_ids})
        )
        return AuthenticationActor(
            identity_id=identity_id,
            principal_kind=principal_kind,
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            roles=roles,
            team_ids=team_ids,
            assurance=assurance,
            session_id=session_id,
            service_token_id=service_token_id,
            service_scopes=tuple(sorted(set(service_scopes))),
        )

    def local_trusted_actor(self) -> AuthenticationActor:
        state = self.bootstrap_local()
        return self._actor(
            state,
            identity_id=DEFAULT_HUMAN_IDENTITY_ID,
            principal_kind=PrincipalKind.HUMAN,
            scope=TenantScope(),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )

    def actor_for_identity(
        self,
        identity_id: str,
        *,
        scope: TenantScope,
    ) -> AuthenticationActor:
        """Resolve current canonical membership/team context for an identity.

        This is intentionally credential-free and is for server-side
        authorization re-evaluation only. It does not authenticate a request.
        """
        state = self.store.load()
        human = next(
            (
                item
                for item in state.humans
                if item.id == identity_id and item.disabled_at is None
            ),
            None,
        )
        service = next(
            (
                item
                for item in state.services
                if item.id == identity_id and item.disabled_at is None
            ),
            None,
        )
        if human is not None and service is not None:
            raise AuthenticationError("identity kind is ambiguous")
        if human is not None:
            return self._actor(
                state,
                identity_id=identity_id,
                principal_kind=PrincipalKind.HUMAN,
                scope=scope,
                assurance=AuthenticationAssurance.PRIMARY,
            )
        if service is not None:
            return self._actor(
                state,
                identity_id=identity_id,
                principal_kind=PrincipalKind.SERVICE,
                scope=scope,
                assurance=AuthenticationAssurance.SERVICE_TOKEN,
            )
        raise AuthenticationError("identity not found or disabled")

    def create_session(
        self,
        *,
        identity_id: str,
        scope: TenantScope,
        assurance: AuthenticationAssurance,
        idle_seconds: int = 3600,
        absolute_seconds: int = 43200,
    ) -> SessionCredentials:
        state = self.store.load()
        human = next(
            (item for item in state.humans if item.id == identity_id and item.disabled_at is None),
            None,
        )
        if human is None:
            raise AuthenticationError("human identity not found or disabled")
        self._actor(
            state,
            identity_id=identity_id,
            principal_kind=PrincipalKind.HUMAN,
            scope=scope,
            assurance=assurance,
        )
        now = time.time()
        session_token = _token()
        refresh_token = _token()
        csrf_token = _token()
        record = SessionRecord(
            identity_id=identity_id,
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            assurance=assurance,
            session_token_hash=_hash(session_token),
            refresh_token_hash=_hash(refresh_token),
            csrf_token_hash=_hash(csrf_token),
            created_at=now,
            last_seen_at=now,
            idle_expires_at=min(now + idle_seconds, now + absolute_seconds),
            absolute_expires_at=now + absolute_seconds,
        )

        def apply(current: IdentityState) -> IdentityState:
            current.sessions.append(record)
            return current

        self.store.update(apply)
        return SessionCredentials(
            session_id=record.id,
            session_token=session_token,
            refresh_token=refresh_token,
            csrf_token=csrf_token,
            idle_expires_at=record.idle_expires_at,
            absolute_expires_at=record.absolute_expires_at,
        )

    def authenticate_session(
        self,
        session_token: str,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
        touch: bool = True,
    ) -> AuthenticatedSession:
        now = time.time()
        state = self.store.load()
        matched = next(
            (
                item
                for item in state.sessions
                if item.revoked_at is None and _matches(session_token, item.session_token_hash)
            ),
            None,
        )
        if matched is None:
            raise AuthenticationError("invalid session")
        if now >= matched.idle_expires_at or now >= matched.absolute_expires_at:
            self.revoke_session(matched.id, reason="expired")
            raise AuthenticationError("session expired")
        if require_csrf and (not csrf_token or not _matches(csrf_token, matched.csrf_token_hash)):
            raise AuthenticationError("csrf validation failed")
        scope = TenantScope(
            organization_id=matched.organization_id,
            workspace_id=matched.workspace_id,
        )
        actor = self._actor(
            state,
            identity_id=matched.identity_id,
            principal_kind=PrincipalKind.HUMAN,
            scope=scope,
            assurance=(
                AuthenticationAssurance.MFA
                if matched.step_up_until is not None and matched.step_up_until > now
                else matched.assurance
            ),
            session_id=matched.id,
        )
        if touch:
            def apply(current: IdentityState) -> IdentityState:
                for index, item in enumerate(current.sessions):
                    if item.id == matched.id and item.revoked_at is None:
                        current.sessions[index] = item.model_copy(
                            update={
                                "last_seen_at": now,
                                "idle_expires_at": min(now + 3600, item.absolute_expires_at),
                            }
                        )
                        break
                return current
            self.store.update(apply)
        return AuthenticatedSession(actor=actor, csrf_token_hash=matched.csrf_token_hash)

    def refresh_session(self, session_id: str, refresh_token: str) -> SessionCredentials:
        now = time.time()
        session_token = _token()
        new_refresh = _token()
        csrf = _token()
        result: list[SessionRecord] = []
        replay_detected: list[bool] = []

        def apply(state: IdentityState) -> IdentityState:
            for index, item in enumerate(state.sessions):
                if item.id != session_id:
                    continue
                if item.revoked_at is not None or now >= item.absolute_expires_at:
                    raise AuthenticationError("session is revoked or expired")
                incoming_hash = _hash(refresh_token)
                if incoming_hash in item.used_refresh_hashes:
                    state.sessions[index] = item.model_copy(
                        update={"revoked_at": now, "revoke_reason": "refresh-token-replay"}
                    )
                    replay_detected.append(True)
                    return state
                if not hmac.compare_digest(incoming_hash, item.refresh_token_hash):
                    raise AuthenticationError("invalid refresh token")
                updated = item.model_copy(
                    update={
                        "session_token_hash": _hash(session_token),
                        "refresh_token_hash": _hash(new_refresh),
                        "used_refresh_hashes": [*item.used_refresh_hashes[-15:], incoming_hash],
                        "csrf_token_hash": _hash(csrf),
                        "last_seen_at": now,
                        "idle_expires_at": min(now + 3600, item.absolute_expires_at),
                        "rotation": item.rotation + 1,
                    }
                )
                state.sessions[index] = updated
                result.append(updated)
                return state
            raise AuthenticationError("session not found")

        self.store.update(apply)
        if replay_detected:
            raise TokenReplayError("refresh token replay detected")
        record = result[0]
        return SessionCredentials(
            session_id=record.id,
            session_token=session_token,
            refresh_token=new_refresh,
            csrf_token=csrf,
            idle_expires_at=record.idle_expires_at,
            absolute_expires_at=record.absolute_expires_at,
        )

    def revoke_session(self, session_id: str, *, reason: str = "revoked") -> None:
        now = time.time()

        def apply(state: IdentityState) -> IdentityState:
            for index, item in enumerate(state.sessions):
                if item.id == session_id:
                    state.sessions[index] = item.model_copy(
                        update={"revoked_at": item.revoked_at or now, "revoke_reason": reason}
                    )
                    return state
            raise AuthenticationError("session not found")

        self.store.update(apply)

    def revoke_other_sessions(self, actor: AuthenticationActor) -> int:
        if actor.principal_kind != PrincipalKind.HUMAN or not actor.session_id:
            raise AuthorizationError("human session required")
        now = time.time()
        count = 0

        def apply(state: IdentityState) -> IdentityState:
            nonlocal count
            for index, item in enumerate(state.sessions):
                if (
                    item.identity_id == actor.identity_id
                    and item.id != actor.session_id
                    and item.revoked_at is None
                ):
                    state.sessions[index] = item.model_copy(
                        update={"revoked_at": now, "revoke_reason": "revoked-other-sessions"}
                    )
                    count += 1
            return state

        self.store.update(apply)
        return count

    def step_up(self, actor: AuthenticationActor, *, duration_seconds: int = 900) -> None:
        if actor.principal_kind != PrincipalKind.HUMAN or not actor.session_id:
            raise AuthorizationError("human session required for step-up")
        now = time.time()

        def apply(state: IdentityState) -> IdentityState:
            for index, item in enumerate(state.sessions):
                if item.id == actor.session_id and item.revoked_at is None:
                    state.sessions[index] = item.model_copy(
                        update={"step_up_until": min(now + duration_seconds, item.absolute_expires_at)}
                    )
                    return state
            raise AuthenticationError("session not found")

        self.store.update(apply)

    @staticmethod
    def require_assurance(
        actor: AuthenticationActor,
        required: AuthenticationAssurance,
    ) -> None:
        if ASSURANCE_RANK[actor.assurance] < ASSURANCE_RANK[required]:
            raise AuthorizationError(
                f"authentication assurance {required.value!r} required"
            )

    @staticmethod
    def require_admin(actor: AuthenticationActor) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "identity:admin" not in actor.service_scopes:
                raise AuthorizationError("identity:admin service scope required")
            return
        if not actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN):
            raise AuthorizationError("organization/workspace administrator role required")

    @staticmethod
    def require_scope(actor: AuthenticationActor, scope: TenantScope) -> None:
        if (
            actor.organization_id != scope.organization_id
            or actor.workspace_id != scope.workspace_id
        ):
            raise TenantIsolationError("cross-tenant access denied")

    def bootstrap_service_actor(
        self,
        *,
        identity_id: str,
        name: str,
        scope: TenantScope,
        service_scopes: Iterable[str] = (),
    ) -> AuthenticationActor:
        """Ensure one internal service principal exists and return its actor."""

        def apply(state: IdentityState) -> IdentityState:
            if not any(item.id == identity_id for item in state.services):
                state.services.append(
                    ServiceIdentity(
                        id=identity_id,
                        name=name,
                        description="Internal codex-web service principal",
                    )
                )
            if not any(
                item.identity_id == identity_id
                and item.principal_kind == PrincipalKind.SERVICE
                and item.organization_id == scope.organization_id
                and item.workspace_id == scope.workspace_id
                and item.revoked_at is None
                for item in state.memberships
            ):
                state.memberships.append(
                    Membership(
                        identity_id=identity_id,
                        principal_kind=PrincipalKind.SERVICE,
                        organization_id=scope.organization_id,
                        workspace_id=scope.workspace_id,
                        roles=[MembershipRole.MEMBER],
                    )
                )
            return state

        state = self.store.update(apply)
        return self._actor(
            state,
            identity_id=identity_id,
            principal_kind=PrincipalKind.SERVICE,
            scope=scope,
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=service_scopes,
        )

    def create_service_identity(self, name: str, description: str | None = None) -> ServiceIdentity:
        identity = ServiceIdentity(name=name, description=description)

        def apply(state: IdentityState) -> IdentityState:
            state.services.append(identity)
            return state

        self.store.update(apply)
        return identity

    def create_service_token(
        self,
        *,
        service_identity_id: str,
        scope: TenantScope,
        scopes: Iterable[str],
        expires_at: float | None = None,
    ) -> ServiceTokenCredentials:
        state = self.store.load()
        identity = next(
            (
                item
                for item in state.services
                if item.id == service_identity_id and item.disabled_at is None
            ),
            None,
        )
        if identity is None:
            raise AuthenticationError("service identity not found or disabled")
        self._actor(
            state,
            identity_id=service_identity_id,
            principal_kind=PrincipalKind.SERVICE,
            scope=scope,
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
        )
        raw = _token()
        record = ServiceTokenRecord(
            service_identity_id=service_identity_id,
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            token_hash=_hash(raw),
            scopes=list(scopes),
            created_at=time.time(),
            expires_at=expires_at,
        )

        def apply(current: IdentityState) -> IdentityState:
            current.service_tokens.append(record)
            return current

        self.store.update(apply)
        return ServiceTokenCredentials(
            token_id=record.id,
            token=raw,
            expires_at=record.expires_at,
        )

    def authenticate_service_token(self, raw_token: str) -> AuthenticationActor:
        now = time.time()
        state = self.store.load()
        record = next(
            (
                item
                for item in state.service_tokens
                if item.revoked_at is None and _matches(raw_token, item.token_hash)
            ),
            None,
        )
        if record is None:
            raise AuthenticationError("invalid service token")
        if record.expires_at is not None and now >= record.expires_at:
            raise AuthenticationError("service token expired")
        scope = TenantScope(
            organization_id=record.organization_id,
            workspace_id=record.workspace_id,
        )
        actor = self._actor(
            state,
            identity_id=record.service_identity_id,
            principal_kind=PrincipalKind.SERVICE,
            scope=scope,
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_token_id=record.id,
            service_scopes=record.scopes,
        )

        def apply(current: IdentityState) -> IdentityState:
            for index, item in enumerate(current.service_tokens):
                if item.id == record.id and item.revoked_at is None:
                    current.service_tokens[index] = item.model_copy(
                        update={"last_used_at": now}
                    )
                    break
            return current

        self.store.update(apply)
        return actor

    def revoke_service_token(self, token_id: str, *, reason: str = "revoked") -> None:
        now = time.time()

        def apply(state: IdentityState) -> IdentityState:
            for index, item in enumerate(state.service_tokens):
                if item.id == token_id:
                    state.service_tokens[index] = item.model_copy(
                        update={"revoked_at": item.revoked_at or now, "revoke_reason": reason}
                    )
                    return state
            raise AuthenticationError("service token not found")

        self.store.update(apply)

    def link_external_identity(
        self,
        identity_id: str,
        result: ExternalAuthenticationResult,
    ) -> HumanIdentity:
        updated: list[HumanIdentity] = []

        def apply(state: IdentityState) -> IdentityState:
            for human in state.humans:
                for link in human.external_links:
                    if (
                        link.provider == result.provider
                        and link.issuer == result.issuer
                        and link.subject == result.subject
                        and human.id != identity_id
                    ):
                        raise AuthorizationError("external identity is already linked")
            for index, human in enumerate(state.humans):
                if human.id != identity_id:
                    continue
                links = [
                    link
                    for link in human.external_links
                    if not (
                        link.provider == result.provider
                        and link.issuer == result.issuer
                        and link.subject == result.subject
                    )
                ]
                links.append(
                    ExternalIdentityLink(
                        provider=result.provider,
                        issuer=result.issuer,
                        subject=result.subject,
                        email=result.email,
                        groups_snapshot=list(result.groups),
                        last_seen_at=time.time(),
                    )
                )
                value = human.model_copy(update={"external_links": links})
                state.humans[index] = value
                updated.append(value)
                return state
            raise AuthenticationError("human identity not found")

        self.store.update(apply)
        return updated[0]

    def authenticate_external(
        self,
        result: ExternalAuthenticationResult,
        *,
        scope: TenantScope,
    ) -> AuthenticationActor:
        state = self.store.load()
        human = next(
            (
                human
                for human in state.humans
                if human.disabled_at is None
                and any(
                    link.provider == result.provider
                    and link.issuer == result.issuer
                    and link.subject == result.subject
                    for link in human.external_links
                )
            ),
            None,
        )
        if human is None:
            raise AuthenticationError("external identity is not linked")
        # Deliberately ignore IdP group claims for authority. They are snapshots
        # for future explicit mapping/reconciliation only.
        return self._actor(
            state,
            identity_id=human.id,
            principal_kind=PrincipalKind.HUMAN,
            scope=scope,
            assurance=result.assurance,
        )

    def list_sessions(self, actor: AuthenticationActor) -> list[SessionRecord]:
        state = self.store.load()
        return [
            item
            for item in state.sessions
            if item.identity_id == actor.identity_id
            and item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        ]

    def create_organization(self, *, name: str, organization_id: str | None = None) -> Organization:
        organization = Organization(
            id=(organization_id or f"org-{secrets.token_hex(6)}"),
            name=name,
        )

        def apply(state: IdentityState) -> IdentityState:
            if any(item.id == organization.id for item in state.organizations):
                raise IdentityError("organization already exists")
            state.organizations.append(organization)
            return state

        self.store.update(apply)
        return organization

    def create_workspace(
        self,
        *,
        organization_id: str,
        name: str,
        workspace_id: str | None = None,
    ) -> Workspace:
        workspace = Workspace(
            id=(workspace_id or f"ws-{secrets.token_hex(6)}"),
            organization_id=organization_id,
            name=name,
        )

        def apply(state: IdentityState) -> IdentityState:
            organization = next(
                (item for item in state.organizations if item.id == organization_id and item.disabled_at is None),
                None,
            )
            if organization is None:
                raise IdentityError("organization not found or disabled")
            if any(item.id == workspace.id for item in state.workspaces):
                raise IdentityError("workspace already exists")
            state.workspaces.append(workspace)
            return state

        self.store.update(apply)
        return workspace

    def create_human_user(
        self,
        payload: HumanUserCreate,
        *,
        actor: AuthenticationActor,
    ) -> tuple[HumanIdentity, Membership]:
        self.require_admin(actor)
        human = HumanIdentity(
            id=payload.id or f"human-{uuid.uuid4().hex}",
            display_name=payload.display_name,
            email=payload.email,
        )
        membership = Membership(
            identity_id=human.id,
            principal_kind=PrincipalKind.HUMAN,
            organization_id=actor.organization_id,
            workspace_id=None if payload.organization_wide else actor.workspace_id,
            roles=payload.roles,
            team_ids=payload.team_ids,
            created_by=actor.identity_id,
        )

        def apply(state: IdentityState) -> IdentityState:
            if any(item.id == human.id for item in state.humans):
                raise IdentityError("human identity already exists")
            if not any(
                item.id == actor.organization_id and item.disabled_at is None
                for item in state.organizations
            ):
                raise IdentityError("active organization not found")
            if membership.workspace_id is not None:
                workspace = next(
                    (
                        item
                        for item in state.workspaces
                        if item.id == membership.workspace_id
                        and item.organization_id == actor.organization_id
                        and item.disabled_at is None
                    ),
                    None,
                )
                if workspace is None:
                    raise TenantIsolationError("active workspace is unavailable")
            state.humans.append(human)
            state.memberships.append(membership)
            return state

        self.store.update(apply)
        return human, membership

    def create_human_identity(
        self,
        *,
        display_name: str,
        email: str | None = None,
        identity_id: str | None = None,
    ) -> HumanIdentity:
        human = HumanIdentity(
            id=identity_id or f"human-{uuid.uuid4().hex}",
            display_name=display_name,
            email=email,
        )

        def apply(state: IdentityState) -> IdentityState:
            if any(item.id == human.id for item in state.humans):
                raise IdentityError("human identity already exists")
            state.humans.append(human)
            return state

        self.store.update(apply)
        return human

    @staticmethod
    def _membership_in_actor_scope(
        membership: Membership,
        actor: AuthenticationActor,
    ) -> bool:
        return (
            membership.organization_id == actor.organization_id
            and membership.workspace_id in {None, actor.workspace_id}
        )

    def update_membership(
        self,
        membership_id: str,
        payload: MembershipUpdate,
        *,
        actor: AuthenticationActor,
    ) -> Membership:
        self.require_admin(actor)
        now = time.time()
        updated: list[Membership] = []

        def apply(state: IdentityState) -> IdentityState:
            for index, item in enumerate(state.memberships):
                if item.id != membership_id:
                    continue
                if not self._membership_in_actor_scope(item, actor):
                    raise TenantIsolationError("membership is outside active organization/workspace")
                if item.revoked_at is not None:
                    raise IdentityError("revoked membership cannot be updated")
                replacement = item.model_copy(
                    update={
                        "roles": payload.roles if payload.roles is not None else item.roles,
                        "team_ids": payload.team_ids if payload.team_ids is not None else item.team_ids,
                        "updated_at": now,
                        "updated_by": actor.identity_id,
                    }
                )
                replacement = Membership.model_validate(replacement.model_dump(mode="json"))
                state.memberships[index] = replacement
                updated.append(replacement)
                return state
            raise IdentityError("membership not found")

        self.store.update(apply)
        return updated[0]

    def revoke_membership(
        self,
        membership_id: str,
        *,
        actor: AuthenticationActor,
    ) -> Membership:
        self.require_admin(actor)
        now = time.time()
        revoked: list[Membership] = []

        def apply(state: IdentityState) -> IdentityState:
            for index, item in enumerate(state.memberships):
                if item.id != membership_id:
                    continue
                if not self._membership_in_actor_scope(item, actor):
                    raise TenantIsolationError("membership is outside active organization/workspace")
                replacement = item.model_copy(
                    update={
                        "updated_at": item.updated_at or now,
                        "updated_by": item.updated_by or actor.identity_id,
                        "revoked_at": item.revoked_at or now,
                        "revoked_by": item.revoked_by or actor.identity_id,
                    }
                )
                state.memberships[index] = replacement
                revoked.append(replacement)
                return state
            raise IdentityError("membership not found")

        self.store.update(apply)
        return revoked[0]

    def add_membership(
        self,
        membership: Membership,
        *,
        actor: AuthenticationActor | None = None,
    ) -> Membership:
        if actor is not None:
            self.require_admin(actor)
            if (
                membership.organization_id != actor.organization_id
                or membership.workspace_id not in {None, actor.workspace_id}
            ):
                raise TenantIsolationError(
                    "membership is outside active organization/workspace"
                )
            if membership.created_by is None:
                membership = membership.model_copy(
                    update={"created_by": actor.identity_id}
                )
        state = self.store.load()
        if membership.principal_kind == PrincipalKind.HUMAN:
            exists = any(item.id == membership.identity_id for item in state.humans)
        else:
            exists = any(item.id == membership.identity_id for item in state.services)
        if not exists:
            raise IdentityError("membership principal does not exist")
        if not any(item.id == membership.organization_id for item in state.organizations):
            raise IdentityError("membership organization does not exist")
        if membership.workspace_id is not None:
            workspace = next(
                (item for item in state.workspaces if item.id == membership.workspace_id),
                None,
            )
            if workspace is None or workspace.organization_id != membership.organization_id:
                raise TenantIsolationError("membership workspace does not belong to organization")

        def apply(current: IdentityState) -> IdentityState:
            current.memberships.append(membership)
            return current

        self.store.update(apply)
        return membership


def identity_http_error(exc: IdentityError) -> HTTPException:
    if isinstance(exc, AuthenticationError):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))
