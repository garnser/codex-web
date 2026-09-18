from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.api.identity import install_identity_middleware
from codex_web.api.secrets import build_secrets_router
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    Membership,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.models import BotConnectionCreate, Project
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate, SecretRotate
from codex_web.services.bot_connections import BotConnectionService
from codex_web.services.identity import IdentityService
from codex_web.services.secrets import (
    SecretBroker,
    SecretRevealDeniedError,
    SecretUseDeniedError,
    scrub_secret,
)
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _BotHost:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.connections = []
        self.bindings = []

    def _project(self, project_id):
        if project_id != self.project.id:
            raise RuntimeError("missing project")
        return self.project

    def _load_bot_connections(self):
        return [item.model_copy(deep=True) for item in self.connections]

    def _save_bot_connections(self, connections):
        self.connections = [item.model_copy(deep=True) for item in connections]

    def _load_bot_bindings(self):
        return list(self.bindings)

    def _save_bot_bindings(self, bindings):
        self.bindings = list(bindings)

    def _binding_prefix(self, binding):
        return None


class SecretBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(self.sqlite))
        self.identity.bootstrap_local()
        self.backend = LocalFileSecretBackend(root / "material")
        self.store = SecretStateStore(self.sqlite)
        self.broker = SecretBroker(self.store, {"local": self.backend})
        self.admin = self.identity.local_trusted_actor()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_secret_material_never_enters_metadata_state_or_public_reference(self) -> None:
        raw = "very-private-token-123"
        reference = self.broker.create(
            SecretCreate(name="provider token", value=raw, purpose="api"),
            actor=self.admin,
        )
        metadata = self.sqlite.get("secret_metadata")
        self.assertNotIn(raw, json.dumps(metadata))
        self.assertNotIn(raw, reference.model_dump_json())

        material_path = self.backend.root / reference.id
        self.assertEqual(material_path.read_text(), raw)
        if os.name != "nt":
            self.assertEqual(material_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.backend.root.stat().st_mode & 0o777, 0o700)

    def test_service_can_use_without_reveal_and_result_is_scrubbed(self) -> None:
        service_actor = self.identity.bootstrap_service_actor(
            identity_id="service-provider",
            name="Provider Runtime",
            scope=TenantScope(),
            service_scopes=("secret:use",),
        )
        reference = self.broker.create(
            SecretCreate(
                name="provider token",
                value="secret-value",
                allowed_identity_ids=[service_actor.identity_id],
            ),
            actor=self.admin,
        )
        result = self.broker.use(
            reference.id,
            actor=service_actor,
            operation="provider.call",
            consumer=lambda value: {"authorization": value, "ok": True},
        )
        self.assertEqual(result["authorization"], "[REDACTED]")
        with self.assertRaises(SecretRevealDeniedError):
            self.broker.reveal(reference.id, actor=service_actor)

        audit = self.broker.audit(self.admin)
        self.assertTrue(any(item.action == "use" and item.outcome == "succeeded" for item in audit))
        self.assertNotIn("secret-value", json.dumps([item.model_dump() for item in audit]))

    def test_rotation_and_revocation_are_reference_stable(self) -> None:
        reference = self.broker.create(
            SecretCreate(name="token", value="one"),
            actor=self.admin,
        )
        self.assertEqual(
            self.broker.use(
                reference.id,
                actor=self.admin,
                operation="read-test",
                consumer=lambda value: value.upper(),
            ),
            "ONE",
        )
        rotated = self.broker.rotate(
            reference.id,
            SecretRotate(value="two"),
            actor=self.admin,
        )
        self.assertEqual(rotated.id, reference.id)
        self.assertEqual(rotated.rotation, 1)
        self.assertEqual(
            self.broker.use(
                reference.id,
                actor=self.admin,
                operation="read-test",
                consumer=lambda value: value.upper(),
            ),
            "TWO",
        )

        self.broker.revoke(reference.id, actor=self.admin)
        with self.assertRaises(SecretUseDeniedError):
            self.broker.use(
                reference.id,
                actor=self.admin,
                operation="read-test",
                consumer=lambda value: value,
            )

    def test_cross_tenant_access_fails_closed(self) -> None:
        reference = self.broker.create(
            SecretCreate(name="token", value="secret"),
            actor=self.admin,
        )
        foreign = AuthenticationActor(
            identity_id="foreign",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="other",
            workspace_id="other",
            roles=(MembershipRole.OWNER,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )
        with self.assertRaises(Exception):
            self.broker.use(
                reference.id,
                actor=foreign,
                operation="provider.call",
                consumer=lambda value: True,
            )

    def test_scrubber_redacts_nested_material(self) -> None:
        payload = {
            "header": "Bearer abc123",
            "nested": ["abc123", {"message": "token=abc123"}],
        }
        scrubbed = scrub_secret(payload, ["abc123"])
        self.assertNotIn("abc123", json.dumps(scrubbed))


class SecretApiTests(unittest.TestCase):
    def test_admin_api_returns_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sqlite = SQLiteStateStore(root / "state.sqlite3")
            identity = IdentityService(IdentityStateStore(sqlite))
            identity.bootstrap_local()
            broker = SecretBroker(
                SecretStateStore(sqlite),
                {"local": LocalFileSecretBackend(root / "material")},
            )
            app = FastAPI()
            install_identity_middleware(app, identity)
            app.include_router(build_secrets_router(broker))
            client = TestClient(app)

            created = client.post(
                "/api/secrets",
                json={"name": "api", "value": "raw-secret-value"},
            )
            self.assertEqual(created.status_code, 200)
            self.assertNotIn("raw-secret-value", created.text)
            secret_id = created.json()["item"]["id"]

            listed = client.get("/api/secrets")
            self.assertEqual(listed.status_code, 200)
            self.assertNotIn("raw-secret-value", listed.text)

            rotated = client.post(
                f"/api/secrets/{secret_id}/rotate",
                json={"value": "new-secret-value"},
            )
            self.assertEqual(rotated.status_code, 200)
            self.assertNotIn("new-secret-value", rotated.text)


class BotCredentialMigrationTests(unittest.TestCase):
    def test_authenticated_bot_save_persists_secret_reference_not_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sqlite = SQLiteStateStore(root / "state.sqlite3")
            identity = IdentityService(IdentityStateStore(sqlite))
            identity.bootstrap_local()
            broker = SecretBroker(
                SecretStateStore(sqlite),
                {"local": LocalFileSecretBackend(root / "material")},
            )
            host = _BotHost(
                Project(
                    id="home",
                    organization_id="local",
                    workspace_id="default",
                    name="Home",
                    path=str(root),
                )
            )
            service = BotConnectionService(
                host,
                secret_broker=broker,
                identity_service=identity,
            )
            connection = service.upsert(
                BotConnectionCreate(
                    provider="slack",
                    name="Slack",
                    project_id="home",
                    bot_token="xoxb-raw-token",
                    slack_app_token="xapp-raw-app-token",
                ),
                actor=identity.local_trusted_actor(),
            )
            self.assertIsNone(connection.bot_token)
            self.assertIsNone(connection.slack_app_token)
            self.assertIsNotNone(connection.bot_token_secret_id)
            self.assertIsNotNone(connection.slack_app_token_secret_id)
            self.assertNotIn("xoxb-raw-token", json.dumps(host.connections[0].model_dump()))
            self.assertEqual(service.public(connection)["bot_token"], "stored")

            runtime_actor = service.runtime_actor("home")
            observed = broker.use(
                connection.bot_token_secret_id,
                actor=runtime_actor,
                operation="test-provider",
                consumer=lambda value: value == "xoxb-raw-token",
            )
            self.assertTrue(observed)


if __name__ == "__main__":
    unittest.main()
