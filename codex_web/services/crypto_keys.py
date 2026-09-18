from __future__ import annotations

import base64
import hashlib
import json
import os
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from codex_web.crypto import (
    CryptoKeyState,
    EncryptedEnvelope,
    EncryptionContext,
    KeyAuditEvent,
    KeyManifestEntry,
    KeyManifestValidation,
    KeyRotationResult,
    KeyScope,
    KeyStatus,
    KeyVersion,
    KeyVersionStatus,
    ManagedKey,
    ManagedKeyCreate,
)
from codex_web.identity import AuthenticationActor, MembershipRole, PrincipalKind
from codex_web.key_backends import KeyBackend, KeyBackendError
from codex_web.services.identity import AuthorizationError, TenantIsolationError
from codex_web.storage.crypto_keys import CryptoKeyStore


class CryptoKeyError(RuntimeError):
    pass


class CryptoKeyNotFoundError(CryptoKeyError):
    pass


class CryptoKeyConflictError(CryptoKeyError):
    pass


class CryptoDecryptError(CryptoKeyError):
    pass


class CryptoKeyService:
    def __init__(
        self,
        store: CryptoKeyStore,
        backends: dict[str, KeyBackend],
    ) -> None:
        self.store = store
        self.backends = dict(backends)

    @staticmethod
    def _require_admin(actor: AuthenticationActor) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "crypto:admin" not in actor.service_scopes:
                raise AuthorizationError("crypto:admin service scope required")
            return
        if not actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN):
            raise AuthorizationError("tenant administrator required")

    @staticmethod
    def _same_scope(key: ManagedKey, actor: AuthenticationActor) -> bool:
        return (
            key.scope.organization_id == actor.organization_id
            and key.scope.workspace_id == actor.workspace_id
        )

    @staticmethod
    def _audit(
        state: CryptoKeyState,
        *,
        actor: AuthenticationActor,
        operation: str,
        outcome: str,
        key: ManagedKey | None = None,
        version: int | None = None,
        context: EncryptionContext | None = None,
        reason_code: str | None = None,
    ) -> None:
        state.events.append(
            KeyAuditEvent(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                actor_id=actor.identity_id,
                operation=operation,
                outcome=outcome,
                key_id=key.id if key is not None else None,
                key_version=version,
                object_type=context.object_type if context is not None else None,
                object_id=context.object_id if context is not None else None,
                reason_code=reason_code,
            )
        )

    def _key(
        self,
        state: CryptoKeyState,
        key_id: str,
        actor: AuthenticationActor,
    ) -> ManagedKey:
        item = next(
            (
                value
                for value in state.keys
                if value.id == key_id and self._same_scope(value, actor)
            ),
            None,
        )
        if item is None:
            raise CryptoKeyNotFoundError("managed key not found")
        return item

    @staticmethod
    def _version(key: ManagedKey, version: int) -> KeyVersion:
        item = next((value for value in key.versions if value.version == version), None)
        if item is None:
            raise CryptoKeyNotFoundError("key version not found")
        return item

    def _backend(self, backend_type: str) -> KeyBackend:
        backend = self.backends.get(backend_type)
        if backend is None:
            raise CryptoKeyError(f"key backend unavailable: {backend_type}")
        if not backend.healthy():
            raise CryptoKeyError(f"key backend unhealthy: {backend_type}")
        return backend

    @staticmethod
    def _context_payload(context: EncryptionContext) -> bytes:
        return json.dumps(
            context.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def _context_digest(cls, context: EncryptionContext) -> str:
        return hashlib.sha256(cls._context_payload(context)).hexdigest()

    @classmethod
    def _data_aad(cls, context: EncryptionContext) -> bytes:
        return b"codex-web:data:v1:" + cls._context_payload(context)

    @classmethod
    def _wrap_aad(
        cls,
        context: EncryptionContext,
        key_id: str,
        key_version: int,
    ) -> bytes:
        return (
            b"codex-web:dek-wrap:v1:"
            + key_id.encode("utf-8")
            + b":"
            + str(key_version).encode("ascii")
            + b":"
            + cls._context_payload(context)
        )

    @staticmethod
    def _ensure_context_scope(
        context: EncryptionContext,
        actor: AuthenticationActor,
    ) -> None:
        if (
            context.organization_id != actor.organization_id
            or context.workspace_id != actor.workspace_id
        ):
            raise TenantIsolationError("cross-tenant encryption context denied")

    @staticmethod
    def _ensure_key_context(key: ManagedKey, context: EncryptionContext) -> None:
        scope = key.scope
        if (
            scope.organization_id != context.organization_id
            or scope.workspace_id != context.workspace_id
            or (scope.project_id is not None and scope.project_id != context.project_id)
            or (scope.resource_id is not None and scope.resource_id != context.resource_id)
        ):
            raise CryptoKeyConflictError("key scope does not match encryption context")

    def list_keys(self, actor: AuthenticationActor) -> list[ManagedKey]:
        return sorted(
            [item for item in self.store.load().keys if self._same_scope(item, actor)],
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )

    def get_key(self, key_id: str, actor: AuthenticationActor) -> ManagedKey:
        return self._key(self.store.load(), key_id, actor)

    def create_key(
        self,
        payload: ManagedKeyCreate,
        *,
        actor: AuthenticationActor,
    ) -> ManagedKey:
        self._require_admin(actor)
        backend = self._backend(payload.backend_type)
        key = ManagedKey(
            scope=KeyScope(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                project_id=payload.project_id,
                resource_id=payload.resource_id,
            ),
            purpose=payload.purpose,
            backend_type=payload.backend_type,
            versions=[KeyVersion(version=1, backend_ref="pending")],
            created_by=actor.identity_id,
        )
        try:
            backend_ref = backend.create(key.id, 1)
        except KeyBackendError as exc:
            raise CryptoKeyError(str(exc)) from exc
        key = key.model_copy(
            update={
                "versions": [
                    key.versions[0].model_copy(update={"backend_ref": backend_ref})
                ]
            }
        )

        def apply(state: CryptoKeyState) -> CryptoKeyState:
            state.keys.append(key)
            self._audit(
                state,
                actor=actor,
                operation="key_created",
                outcome="success",
                key=key,
                version=1,
            )
            return state

        try:
            self.store.update(apply)
        except Exception:
            backend.delete(backend_ref)
            raise
        return key

    def rotate(
        self,
        key_id: str,
        *,
        actor: AuthenticationActor,
    ) -> KeyRotationResult:
        self._require_admin(actor)
        state = self.store.load()
        key = self._key(state, key_id, actor)
        if key.status != KeyStatus.ACTIVE:
            raise CryptoKeyConflictError("revoked key cannot be rotated")
        backend = self._backend(key.backend_type)
        previous = key.current_version
        next_version = max(item.version for item in key.versions) + 1
        try:
            backend_ref = backend.create(key.id, next_version)
        except KeyBackendError as exc:
            raise CryptoKeyError(str(exc)) from exc
        now = time.time()
        updated_key = key.model_copy(
            update={
                "current_version": next_version,
                "updated_at": now,
                "versions": [
                    (
                        item.model_copy(
                            update={
                                "status": KeyVersionStatus.DECRYPT_ONLY,
                                "rotated_at": now,
                            }
                        )
                        if item.version == previous
                        else item
                    )
                    for item in key.versions
                ]
                + [
                    KeyVersion(
                        version=next_version,
                        backend_ref=backend_ref,
                        created_at=now,
                    )
                ],
            }
        )

        def apply(current: CryptoKeyState) -> CryptoKeyState:
            persisted = self._key(current, key_id, actor)
            if persisted.current_version != previous:
                raise CryptoKeyConflictError("key rotated concurrently")
            current.keys = [
                updated_key if item.id == key_id else item for item in current.keys
            ]
            self._audit(
                current,
                actor=actor,
                operation="key_rotated",
                outcome="success",
                key=updated_key,
                version=next_version,
            )
            return current

        try:
            self.store.update(apply)
        except Exception:
            backend.delete(backend_ref)
            raise
        return KeyRotationResult(
            key_id=key_id,
            previous_version=previous,
            current_version=next_version,
        )

    def revoke_version(
        self,
        key_id: str,
        version: int,
        reason: str,
        *,
        actor: AuthenticationActor,
    ) -> ManagedKey:
        self._require_admin(actor)
        updated: list[ManagedKey] = []

        def apply(state: CryptoKeyState) -> CryptoKeyState:
            key = self._key(state, key_id, actor)
            selected = self._version(key, version)
            if selected.status == KeyVersionStatus.REVOKED:
                updated.append(key)
                return state
            if key.current_version == version and key.status == KeyStatus.ACTIVE:
                raise CryptoKeyConflictError(
                    "rotate or revoke the whole key before revoking its active version"
                )
            now = time.time()
            replacement = key.model_copy(
                update={
                    "versions": [
                        (
                            item.model_copy(
                                update={
                                    "status": KeyVersionStatus.REVOKED,
                                    "revoked_at": now,
                                    "revoke_reason": reason,
                                }
                            )
                            if item.version == version
                            else item
                        )
                        for item in key.versions
                    ],
                    "updated_at": now,
                }
            )
            state.keys = [
                replacement if item.id == key_id else item for item in state.keys
            ]
            self._audit(
                state,
                actor=actor,
                operation="key_version_revoked",
                outcome="success",
                key=replacement,
                version=version,
                reason_code="revoked",
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def revoke_key(
        self,
        key_id: str,
        reason: str,
        *,
        actor: AuthenticationActor,
    ) -> ManagedKey:
        self._require_admin(actor)
        updated: list[ManagedKey] = []

        def apply(state: CryptoKeyState) -> CryptoKeyState:
            key = self._key(state, key_id, actor)
            if key.status == KeyStatus.REVOKED:
                updated.append(key)
                return state
            now = time.time()
            replacement = key.model_copy(
                update={
                    "status": KeyStatus.REVOKED,
                    "updated_at": now,
                    "versions": [
                        item.model_copy(
                            update={
                                "status": KeyVersionStatus.REVOKED,
                                "revoked_at": item.revoked_at or now,
                                "revoke_reason": item.revoke_reason or reason,
                            }
                        )
                        for item in key.versions
                    ],
                }
            )
            state.keys = [
                replacement if item.id == key_id else item for item in state.keys
            ]
            self._audit(
                state,
                actor=actor,
                operation="key_revoked",
                outcome="success",
                key=replacement,
                version=key.current_version,
                reason_code="revoked",
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def encrypt(
        self,
        key_id: str,
        plaintext: bytes,
        context: EncryptionContext,
        *,
        actor: AuthenticationActor,
    ) -> EncryptedEnvelope:
        self._ensure_context_scope(context, actor)
        state = self.store.load()
        key = self._key(state, key_id, actor)
        self._ensure_key_context(key, context)
        if key.status != KeyStatus.ACTIVE:
            raise CryptoKeyConflictError("key is revoked")
        version = self._version(key, key.current_version)
        if version.status != KeyVersionStatus.ACTIVE:
            raise CryptoKeyConflictError("current key version is not active")
        backend = self._backend(key.backend_type)
        try:
            kek = backend.get(version.backend_ref)
        except KeyBackendError as exc:
            raise CryptoKeyError(str(exc)) from exc

        dek = os.urandom(32)
        wrap_nonce = os.urandom(12)
        data_nonce = os.urandom(12)
        wrapped = AESGCM(kek).encrypt(
            wrap_nonce,
            dek,
            self._wrap_aad(context, key.id, version.version),
        )
        ciphertext = AESGCM(dek).encrypt(
            data_nonce,
            plaintext,
            self._data_aad(context),
        )
        envelope = EncryptedEnvelope(
            key_id=key.id,
            key_version=version.version,
            wrap_nonce_b64=base64.b64encode(wrap_nonce).decode("ascii"),
            wrapped_data_key_b64=base64.b64encode(wrapped).decode("ascii"),
            data_nonce_b64=base64.b64encode(data_nonce).decode("ascii"),
            ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
            context_digest=self._context_digest(context),
        )

        def audit(current: CryptoKeyState) -> CryptoKeyState:
            persisted = self._key(current, key.id, actor)
            self._audit(
                current,
                actor=actor,
                operation="encrypt",
                outcome="success",
                key=persisted,
                version=version.version,
                context=context,
            )
            return current

        self.store.update(audit)
        return envelope

    def decrypt(
        self,
        envelope: EncryptedEnvelope,
        context: EncryptionContext,
        *,
        actor: AuthenticationActor,
    ) -> bytes:
        self._ensure_context_scope(context, actor)
        if envelope.context_digest != self._context_digest(context):
            raise CryptoDecryptError("encryption context mismatch")
        state = self.store.load()
        key = self._key(state, envelope.key_id, actor)
        self._ensure_key_context(key, context)
        version = self._version(key, envelope.key_version)
        if key.status == KeyStatus.REVOKED or version.status == KeyVersionStatus.REVOKED:
            raise CryptoDecryptError("key version is revoked")
        backend = self._backend(key.backend_type)
        try:
            kek = backend.get(version.backend_ref)
            wrap_nonce = base64.b64decode(envelope.wrap_nonce_b64, validate=True)
            wrapped = base64.b64decode(envelope.wrapped_data_key_b64, validate=True)
            data_nonce = base64.b64decode(envelope.data_nonce_b64, validate=True)
            ciphertext = base64.b64decode(envelope.ciphertext_b64, validate=True)
            dek = AESGCM(kek).decrypt(
                wrap_nonce,
                wrapped,
                self._wrap_aad(context, key.id, version.version),
            )
            plaintext = AESGCM(dek).decrypt(
                data_nonce,
                ciphertext,
                self._data_aad(context),
            )
        except (KeyBackendError, ValueError, InvalidTag) as exc:
            raise CryptoDecryptError(
                "ciphertext or key material could not be authenticated"
            ) from exc

        def audit(current: CryptoKeyState) -> CryptoKeyState:
            persisted = self._key(current, key.id, actor)
            self._audit(
                current,
                actor=actor,
                operation="decrypt",
                outcome="success",
                key=persisted,
                version=version.version,
                context=context,
            )
            return current

        self.store.update(audit)
        return plaintext

    def reencrypt(
        self,
        envelope: EncryptedEnvelope,
        context: EncryptionContext,
        *,
        actor: AuthenticationActor,
    ) -> EncryptedEnvelope:
        plaintext = self.decrypt(envelope, context, actor=actor)
        return self.encrypt(envelope.key_id, plaintext, context, actor=actor)

    def manifest(self, actor: AuthenticationActor) -> tuple[KeyManifestEntry, ...]:
        return tuple(
            KeyManifestEntry(
                key_id=key.id,
                versions=tuple(item.version for item in key.versions),
            )
            for key in self.list_keys(actor)
        )

    def validate_manifest(
        self,
        entries: tuple[KeyManifestEntry, ...],
        *,
        actor: AuthenticationActor,
    ) -> KeyManifestValidation:
        state = self.store.load()
        missing: list[str] = []
        revoked: list[str] = []
        for entry in entries:
            try:
                key = self._key(state, entry.key_id, actor)
            except CryptoKeyNotFoundError:
                missing.extend(f"{entry.key_id}:v{version}" for version in entry.versions)
                continue
            backend = self.backends.get(key.backend_type)
            for requested in entry.versions:
                try:
                    version = self._version(key, requested)
                except CryptoKeyNotFoundError:
                    missing.append(f"{key.id}:v{requested}")
                    continue
                ref = f"{key.id}:v{requested}"
                if version.status == KeyVersionStatus.REVOKED:
                    revoked.append(ref)
                if backend is None or not backend.exists(version.backend_ref):
                    missing.append(ref)
        return KeyManifestValidation(
            valid=not missing and not revoked,
            missing_refs=tuple(sorted(set(missing))),
            revoked_refs=tuple(sorted(set(revoked))),
        )

    def backend_health(self) -> dict[str, bool]:
        return {
            name: bool(backend.healthy())
            for name, backend in sorted(self.backends.items())
        }

    def events(self, actor: AuthenticationActor) -> list[KeyAuditEvent]:
        self._require_admin(actor)
        return sorted(
            [
                item
                for item in self.store.load().events
                if item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
            ],
            key=lambda item: (item.occurred_at, item.id),
            reverse=True,
        )
