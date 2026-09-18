from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.crypto_keys import build_crypto_keys_router
from codex_web.crypto import (
    EncryptionContext,
    KeyPurpose,
    KeyVersionStatus,
    ManagedKeyCreate,
    encryption_required,
)
from codex_web.data_governance import DataClassification
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.key_backends import LocalFileKeyBackend
from codex_web.services.crypto_keys import (
    CryptoDecryptError,
    CryptoKeyConflictError,
    CryptoKeyNotFoundError,
    CryptoKeyService,
)
from codex_web.services.identity import TenantIsolationError
from codex_web.storage.crypto_keys import CryptoKeyStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class CryptoKeyServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.backend = LocalFileKeyBackend(root / "keys")
        self.service = CryptoKeyService(
            CryptoKeyStore(self.sqlite),
            {"local": self.backend},
        )
        self.actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.other = AuthenticationActor(
            identity_id="other-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-b",
            workspace_id="ws-b",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.context = EncryptionContext(
            organization_id="org-a",
            workspace_id="ws-a",
            project_id="project-a",
            object_type="decision",
            object_id="decision-1",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _key(self):
        return self.service.create_key(
            ManagedKeyCreate(
                project_id="project-a",
                purpose=KeyPurpose.DECISION,
            ),
            actor=self.actor,
        )

    def test_envelope_round_trip_and_plaintext_never_enters_key_state(self) -> None:
        key = self._key()
        plaintext = b"private decision payload"

        envelope = self.service.encrypt(
            key.id,
            plaintext,
            self.context,
            actor=self.actor,
        )
        recovered = self.service.decrypt(
            envelope,
            self.context,
            actor=self.actor,
        )

        self.assertEqual(recovered, plaintext)
        persisted = json.dumps(self.sqlite.get("crypto_keys"), sort_keys=True)
        self.assertNotIn("private decision payload", persisted)
        self.assertNotIn(envelope.ciphertext_b64, persisted)
        self.assertEqual(len(self.backend.get(key.versions[0].backend_ref)), 32)

    def test_context_and_tenant_substitution_fail_closed(self) -> None:
        key = self._key()
        envelope = self.service.encrypt(
            key.id,
            b"payload",
            self.context,
            actor=self.actor,
        )
        wrong_object = self.context.model_copy(update={"object_id": "decision-2"})

        with self.assertRaises(CryptoDecryptError):
            self.service.decrypt(envelope, wrong_object, actor=self.actor)
        with self.assertRaises(TenantIsolationError):
            self.service.decrypt(envelope, self.context, actor=self.other)
        with self.assertRaises(CryptoKeyNotFoundError):
            self.service.get_key(key.id, self.other)

    def test_project_scoped_key_cannot_encrypt_for_another_project(self) -> None:
        key = self._key()
        wrong_project = self.context.model_copy(update={"project_id": "project-b"})

        with self.assertRaises(CryptoKeyConflictError):
            self.service.encrypt(
                key.id,
                b"payload",
                wrong_project,
                actor=self.actor,
            )

    def test_rotation_keeps_old_ciphertext_readable_and_reencrypts_online(self) -> None:
        key = self._key()
        old = self.service.encrypt(
            key.id,
            b"payload",
            self.context,
            actor=self.actor,
        )

        rotation = self.service.rotate(key.id, actor=self.actor)
        self.assertEqual(rotation.previous_version, 1)
        self.assertEqual(rotation.current_version, 2)

        current = self.service.get_key(key.id, self.actor)
        statuses = {item.version: item.status for item in current.versions}
        self.assertEqual(statuses[1], KeyVersionStatus.DECRYPT_ONLY)
        self.assertEqual(statuses[2], KeyVersionStatus.ACTIVE)
        self.assertEqual(
            self.service.decrypt(old, self.context, actor=self.actor),
            b"payload",
        )

        new = self.service.encrypt(
            key.id,
            b"payload-2",
            self.context,
            actor=self.actor,
        )
        self.assertEqual(new.key_version, 2)

        migrated = self.service.reencrypt(old, self.context, actor=self.actor)
        self.assertEqual(migrated.key_version, 2)
        self.assertEqual(
            self.service.decrypt(migrated, self.context, actor=self.actor),
            b"payload",
        )

    def test_revoked_old_version_and_missing_backend_material_fail_closed(self) -> None:
        key = self._key()
        old = self.service.encrypt(
            key.id,
            b"payload",
            self.context,
            actor=self.actor,
        )
        self.service.rotate(key.id, actor=self.actor)
        self.service.revoke_version(
            key.id,
            1,
            "retired",
            actor=self.actor,
        )
        with self.assertRaises(CryptoDecryptError):
            self.service.decrypt(old, self.context, actor=self.actor)

        current = self.service.get_key(key.id, self.actor)
        current_version = next(
            item for item in current.versions if item.version == current.current_version
        )
        fresh = self.service.encrypt(
            key.id,
            b"fresh",
            self.context,
            actor=self.actor,
        )
        self.backend.delete(current_version.backend_ref)
        with self.assertRaises(CryptoDecryptError):
            self.service.decrypt(fresh, self.context, actor=self.actor)

    def test_manifest_validation_detects_missing_and_revoked_versions(self) -> None:
        key = self._key()
        self.service.rotate(key.id, actor=self.actor)
        manifest = self.service.manifest(self.actor)
        valid = self.service.validate_manifest(manifest, actor=self.actor)
        self.assertTrue(valid.valid)

        self.service.revoke_version(key.id, 1, "retired", actor=self.actor)
        revoked = self.service.validate_manifest(manifest, actor=self.actor)
        self.assertFalse(revoked.valid)
        self.assertIn(f"{key.id}:v1", revoked.revoked_refs)

        current = self.service.get_key(key.id, self.actor)
        version_two = next(item for item in current.versions if item.version == 2)
        self.backend.delete(version_two.backend_ref)
        missing = self.service.validate_manifest(manifest, actor=self.actor)
        self.assertIn(f"{key.id}:v2", missing.missing_refs)

    def test_audit_contains_references_not_sensitive_payloads_or_key_material(self) -> None:
        key = self._key()
        envelope = self.service.encrypt(
            key.id,
            b"do-not-log-this",
            self.context,
            actor=self.actor,
        )
        self.service.decrypt(envelope, self.context, actor=self.actor)

        serialized = " ".join(
            item.model_dump_json() for item in self.service.events(self.actor)
        )
        self.assertIn(key.id, serialized)
        self.assertIn("decision-1", serialized)
        self.assertNotIn("do-not-log-this", serialized)
        self.assertNotIn(envelope.ciphertext_b64, serialized)

    def test_classification_baseline_requires_encryption_for_confidential_plus(self) -> None:
        self.assertFalse(encryption_required(DataClassification.PUBLIC))
        self.assertFalse(encryption_required(DataClassification.INTERNAL))
        self.assertTrue(encryption_required(DataClassification.CONFIDENTIAL))
        self.assertTrue(encryption_required(DataClassification.RESTRICTED))
        self.assertTrue(encryption_required(DataClassification.SECRET))


class CryptoKeyApiAssuranceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.service = CryptoKeyService(
            CryptoKeyStore(SQLiteStateStore(root / "state.sqlite3")),
            {"local": LocalFileKeyBackend(root / "keys")},
        )
        self.actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_crypto_keys_router(self.service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def test_low_assurance_human_can_inspect_but_cannot_create_key(self) -> None:
        self.assertEqual(self.client.get("/api/crypto/keys").status_code, 200)

        response = self.client.post(
            "/api/crypto/keys",
            json={"purpose": "application_data", "backend_type": "local"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("mfa", response.json()["detail"].lower())

    def test_mfa_human_can_create_and_rotate_key(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )
        created = self.client.post(
            "/api/crypto/keys",
            json={"purpose": "application_data", "backend_type": "local"},
        )
        self.assertEqual(created.status_code, 200)
        key_id = created.json()["item"]["id"]

        rotated = self.client.post(f"/api/crypto/keys/{key_id}/rotate")

        self.assertEqual(rotated.status_code, 200)
        self.assertEqual(rotated.json()["current_version"], 2)

    def test_crypto_admin_service_actor_does_not_require_human_mfa(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="crypto-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("crypto:admin",),
        )

        response = self.client.post(
            "/api/crypto/keys",
            json={"purpose": "backup", "backend_type": "local"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["purpose"], "backup")


if __name__ == "__main__":
    unittest.main()
