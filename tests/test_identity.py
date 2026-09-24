from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.api.identity import CSRF_HEADER, build_identity_router, install_identity_middleware
from codex_web.identity import (
    AuthenticationAssurance,
    ExternalAuthenticationResult,
    HumanIdentity,
    HumanUserCreate,
    Membership,
    MembershipRole,
    MembershipUpdate,
    Organization,
    PrincipalKind,
    ServiceIdentity,
    TenantScope,
    Workspace,
)
from codex_web.services.identity import (
    AuthenticationError,
    AuthenticationRateLimiter,
    IdentityService,
    TenantIsolationError,
    TokenReplayError,
)
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class IdentityServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        store = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.state_store = IdentityStateStore(store)
        self.service = IdentityService(self.state_store)
        self.service.bootstrap_local()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_local_bootstrap_is_idempotent_and_separates_identity_from_membership(self) -> None:
        first = self.service.bootstrap_local()
        second = self.service.bootstrap_local()
        self.assertEqual(len(second.organizations), len(first.organizations))
        self.assertEqual(len(second.workspaces), len(first.workspaces))
        self.assertEqual(len(second.humans), len(first.humans))
        self.assertEqual(len(second.memberships), len(first.memberships))

        actor = self.service.local_trusted_actor()
        self.assertEqual(actor.principal_kind, PrincipalKind.HUMAN)
        self.assertIn(MembershipRole.OWNER, actor.roles)
        self.assertEqual(actor.organization_id, "local")
        self.assertEqual(actor.workspace_id, "default")

    def test_session_tokens_are_hashed_rotated_revocable_and_csrf_bound(self) -> None:
        credentials = self.service.create_session(
            identity_id="local-admin",
            scope=TenantScope(),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        state = self.state_store.load()
        record = next(item for item in state.sessions if item.id == credentials.session_id)
        self.assertNotEqual(record.session_token_hash, credentials.session_token)
        self.assertNotEqual(record.refresh_token_hash, credentials.refresh_token)
        self.assertNotEqual(record.csrf_token_hash, credentials.csrf_token)

        authenticated = self.service.authenticate_session(
            credentials.session_token,
            csrf_token=credentials.csrf_token,
            require_csrf=True,
        )
        self.assertEqual(authenticated.actor.identity_id, "local-admin")

        with self.assertRaises(AuthenticationError):
            self.service.authenticate_session(
                credentials.session_token,
                csrf_token="wrong",
                require_csrf=True,
            )

        rotated = self.service.refresh_session(
            credentials.session_id,
            credentials.refresh_token,
        )
        self.assertNotEqual(rotated.session_token, credentials.session_token)
        with self.assertRaises(AuthenticationError):
            self.service.authenticate_session(credentials.session_token)
        self.assertEqual(
            self.service.authenticate_session(rotated.session_token).actor.identity_id,
            "local-admin",
        )

        with self.assertRaises(TokenReplayError):
            self.service.refresh_session(
                credentials.session_id,
                credentials.refresh_token,
            )
        replayed = next(
            item
            for item in self.state_store.load().sessions
            if item.id == credentials.session_id
        )
        self.assertIsNotNone(replayed.revoked_at)
        self.assertEqual(replayed.revoke_reason, "refresh-token-replay")

    def test_external_identity_claims_do_not_grant_membership(self) -> None:
        def add_external_user(state):
            state.humans.append(HumanIdentity(id="human-ext", display_name="External"))
            return state

        self.state_store.update(add_external_user)
        result = ExternalAuthenticationResult(
            provider="oidc",
            issuer="https://idp.example",
            subject="subject-1",
            groups=["super-admins"],
        )
        self.service.link_external_identity("human-ext", result)

        with self.assertRaises(TenantIsolationError):
            self.service.authenticate_external(result, scope=TenantScope())

        self.service.add_membership(
            Membership(
                identity_id="human-ext",
                principal_kind=PrincipalKind.HUMAN,
                organization_id="local",
                workspace_id="default",
                roles=[MembershipRole.MEMBER],
            )
        )
        actor = self.service.authenticate_external(result, scope=TenantScope())
        self.assertEqual(actor.roles, (MembershipRole.MEMBER,))
        self.assertNotIn(MembershipRole.ADMIN, actor.roles)

    def test_service_token_cannot_cross_workspace_or_masquerade_as_human(self) -> None:
        service_identity = self.service.create_service_identity("worker")
        self.service.add_membership(
            Membership(
                identity_id=service_identity.id,
                principal_kind=PrincipalKind.SERVICE,
                organization_id="local",
                workspace_id="default",
                roles=[MembershipRole.MEMBER],
            )
        )
        credentials = self.service.create_service_token(
            service_identity_id=service_identity.id,
            scope=TenantScope(),
            scopes=["work:read"],
        )
        actor = self.service.authenticate_service_token(credentials.token)
        self.assertEqual(actor.principal_kind, PrincipalKind.SERVICE)
        self.assertEqual(actor.service_token_id, credentials.token_id)
        self.assertEqual(actor.service_scopes, ("work:read",))

        with self.assertRaises(TenantIsolationError):
            self.service.require_scope(
                actor,
                TenantScope(organization_id="local", workspace_id="other"),
            )

        self.service.revoke_service_token(credentials.token_id)
        with self.assertRaises(AuthenticationError):
            self.service.authenticate_service_token(credentials.token)

    def test_membership_rejects_workspace_from_another_organization(self) -> None:
        def seed(state):
            state.organizations.append(Organization(id="other", name="Other"))
            state.workspaces.append(
                Workspace(id="other-ws", organization_id="other", name="Other")
            )
            state.humans.append(HumanIdentity(id="human-2", display_name="Second"))
            return state

        self.state_store.update(seed)
        with self.assertRaises(TenantIsolationError):
            self.service.add_membership(
                Membership(
                    identity_id="human-2",
                    principal_kind=PrincipalKind.HUMAN,
                    organization_id="local",
                    workspace_id="other-ws",
                )
            )

    def test_membership_roles_can_be_replaced_and_revocation_removes_authority(self) -> None:
        human = self.service.create_human_identity(
            display_name="Scoped User",
            identity_id="human-scoped",
        )
        membership = self.service.add_membership(
            Membership(
                identity_id=human.id,
                principal_kind=PrincipalKind.HUMAN,
                organization_id="local",
                workspace_id="default",
                roles=[MembershipRole.MEMBER],
            )
        )
        admin = self.service.local_trusted_actor()

        updated = self.service.update_membership(
            membership.id,
            MembershipUpdate(roles=[MembershipRole.APPROVER, MembershipRole.MEMBER]),
            actor=admin,
        )
        self.assertEqual(
            updated.roles,
            [MembershipRole.APPROVER, MembershipRole.MEMBER],
        )
        self.assertEqual(updated.updated_by, admin.identity_id)
        self.assertIsNotNone(updated.updated_at)

        actor = self.service.actor_for_identity(human.id, scope=TenantScope())
        self.assertIn(MembershipRole.APPROVER, actor.roles)

        revoked = self.service.revoke_membership(membership.id, actor=admin)
        self.assertIsNotNone(revoked.revoked_at)
        self.assertEqual(revoked.revoked_by, admin.identity_id)
        with self.assertRaises(TenantIsolationError):
            self.service.actor_for_identity(human.id, scope=TenantScope())

    def test_membership_mutation_cannot_cross_active_workspace(self) -> None:
        def seed(state):
            state.workspaces.append(
                Workspace(id="other-workspace", organization_id="local", name="Other")
            )
            state.humans.append(HumanIdentity(id="human-other", display_name="Other User"))
            state.memberships.append(
                Membership(
                    id="membership-other",
                    identity_id="human-other",
                    principal_kind=PrincipalKind.HUMAN,
                    organization_id="local",
                    workspace_id="other-workspace",
                    roles=[MembershipRole.MEMBER],
                )
            )
            return state

        self.state_store.update(seed)
        admin = self.service.local_trusted_actor()
        with self.assertRaises(TenantIsolationError):
            self.service.update_membership(
                "membership-other",
                MembershipUpdate(roles=[MembershipRole.ADMIN]),
                actor=admin,
            )
        with self.assertRaises(TenantIsolationError):
            self.service.revoke_membership("membership-other", actor=admin)

    def test_admin_can_atomically_create_human_with_active_scope_membership(self) -> None:
        admin = self.service.local_trusted_actor()
        human, membership = self.service.create_human_user(
            HumanUserCreate(
                id="human-new",
                display_name="New User",
                email="new@example.test",
                roles=[MembershipRole.APPROVER, MembershipRole.MEMBER],
            ),
            actor=admin,
        )

        self.assertEqual(human.id, "human-new")
        self.assertEqual(membership.identity_id, human.id)
        self.assertEqual(membership.organization_id, admin.organization_id)
        self.assertEqual(membership.workspace_id, admin.workspace_id)
        self.assertEqual(membership.created_by, admin.identity_id)
        actor = self.service.actor_for_identity(human.id, scope=TenantScope())
        self.assertIn(MembershipRole.APPROVER, actor.roles)

    def test_atomic_human_creation_leaves_no_partial_identity_on_invalid_workspace(self) -> None:
        admin = self.service.local_trusted_actor().model_copy(
            update={"workspace_id": "missing-workspace"}
        )
        with self.assertRaises(TenantIsolationError):
            self.service.create_human_user(
                HumanUserCreate(
                    id="human-should-not-exist",
                    display_name="Should Not Exist",
                ),
                actor=admin,
            )
        self.assertFalse(
            any(
                item.id == "human-should-not-exist"
                for item in self.service.state().humans
            )
        )

    def test_actor_scoped_membership_creation_records_creator(self) -> None:
        human = self.service.create_human_identity(
            display_name="Membership Target",
            identity_id="human-membership-provenance",
        )
        admin = self.service.local_trusted_actor()
        membership = self.service.add_membership(
            Membership(
                identity_id=human.id,
                principal_kind=PrincipalKind.HUMAN,
                organization_id=admin.organization_id,
                workspace_id=admin.workspace_id,
                roles=[MembershipRole.MEMBER],
            ),
            actor=admin,
        )
        self.assertEqual(membership.created_by, admin.identity_id)
        persisted = next(
            item
            for item in self.service.state().memberships
            if item.id == membership.id
        )
        self.assertEqual(persisted.created_by, admin.identity_id)

    def test_admin_scoped_membership_creation_rejects_other_workspace(self) -> None:
        def seed(state):
            state.workspaces.append(
                Workspace(id="other-workspace", organization_id="local", name="Other")
            )
            state.humans.append(HumanIdentity(id="human-create", display_name="Create Target"))
            return state

        self.state_store.update(seed)
        admin = self.service.local_trusted_actor()
        with self.assertRaises(TenantIsolationError):
            self.service.add_membership(
                Membership(
                    identity_id="human-create",
                    principal_kind=PrincipalKind.HUMAN,
                    organization_id="local",
                    workspace_id="other-workspace",
                    roles=[MembershipRole.MEMBER],
                ),
                actor=admin,
            )

    def test_rate_limiter_blocks_after_bounded_failures_and_resets(self) -> None:
        limiter = AuthenticationRateLimiter(max_failures=2, window_seconds=60)
        limiter.failure("ip:1")
        limiter.check("ip:1")
        limiter.failure("ip:1")
        with self.assertRaises(AuthenticationError):
            limiter.check("ip:1")
        limiter.success("ip:1")
        limiter.check("ip:1")


class IdentityMiddlewareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        store = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.service = IdentityService(IdentityStateStore(store))
        self.service.bootstrap_local()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _app(self) -> FastAPI:
        app = FastAPI()
        install_identity_middleware(app, self.service)
        app.include_router(build_identity_router(self.service))

        @app.post("/mutation")
        async def mutation():
            return {"ok": True}

        @app.get("/probe")
        async def probe():
            return {"ok": True}

        return app

    def test_local_trusted_mode_preserves_existing_single_user_installations(self) -> None:
        with patch.dict(os.environ, {"CODEX_WEB_IDENTITY_MODE": "local-trusted"}):
            with TestClient(self._app()) as client:
                response = client.get("/api/identity/me")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["identity_id"], "local-admin")
                self.assertEqual(response.json()["organization_id"], "local")
                self.assertEqual(response.json()["workspace_id"], "default")

    def test_enforced_mode_rejects_missing_credentials_and_cross_scope_headers(self) -> None:
        credentials = self.service.create_session(
            identity_id="local-admin",
            scope=TenantScope(),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        with patch.dict(os.environ, {"CODEX_WEB_IDENTITY_MODE": "enforced"}):
            with TestClient(self._app()) as client:
                self.assertEqual(client.get("/probe").status_code, 401)

                response = client.get(
                    "/probe",
                    headers={
                        "Authorization": f"Bearer {credentials.session_token}",
                        "X-Codex-Workspace": "other",
                    },
                )
                self.assertEqual(response.status_code, 403)

    def test_cookie_session_requires_csrf_for_mutation(self) -> None:
        credentials = self.service.create_session(
            identity_id="local-admin",
            scope=TenantScope(),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        with patch.dict(os.environ, {"CODEX_WEB_IDENTITY_MODE": "enforced"}):
            with TestClient(self._app()) as client:
                client.cookies.set("codex_web_session", credentials.session_token)
                self.assertEqual(client.post("/mutation").status_code, 401)
                response = client.post(
                    "/mutation",
                    headers={CSRF_HEADER: credentials.csrf_token},
                )
                self.assertEqual(response.status_code, 200)

    def test_admin_can_update_and_revoke_membership_through_canonical_api(self) -> None:
        human = self.service.create_human_identity(
            display_name="API User",
            identity_id="human-api",
        )
        membership = self.service.add_membership(
            Membership(
                identity_id=human.id,
                principal_kind=PrincipalKind.HUMAN,
                organization_id="local",
                workspace_id="default",
                roles=[MembershipRole.MEMBER],
            )
        )
        with patch.dict(os.environ, {"CODEX_WEB_IDENTITY_MODE": "local-trusted"}):
            with TestClient(self._app()) as client:
                response = client.patch(
                    f"/api/identity/memberships/{membership.id}",
                    json={"roles": ["approver", "member"]},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["roles"], ["approver", "member"])
                self.assertEqual(response.json()["updated_by"], "local-admin")

                response = client.delete(
                    f"/api/identity/memberships/{membership.id}",
                )
                self.assertEqual(response.status_code, 200)
                self.assertIsNotNone(response.json()["revoked_at"])
                self.assertEqual(response.json()["revoked_by"], "local-admin")

                snapshot = client.get("/api/identity").json()
                persisted = next(
                    item
                    for item in snapshot["memberships"]
                    if item["id"] == membership.id
                )
                self.assertIsNotNone(persisted["revoked_at"])
                self.assertEqual(persisted["revoked_by"], "local-admin")

    def test_admin_can_atomically_create_scoped_user_through_api(self) -> None:
        with patch.dict(os.environ, {"CODEX_WEB_IDENTITY_MODE": "local-trusted"}):
            with TestClient(self._app()) as client:
                response = client.post(
                    "/api/identity/users",
                    json={
                        "id": "human-api-created",
                        "display_name": "API Created",
                        "email": "created@example.test",
                        "roles": ["member"],
                    },
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["human"]["id"], "human-api-created")
                self.assertEqual(
                    payload["membership"]["workspace_id"],
                    "default",
                )
                snapshot = client.get("/api/identity").json()
                self.assertTrue(
                    any(
                        item["identity_id"] == "human-api-created"
                        and item["revoked_at"] is None
                        for item in snapshot["memberships"]
                    )
                )

    def test_admin_identity_snapshot_never_returns_credential_hashes(self) -> None:
        credentials = self.service.create_session(
            identity_id="local-admin",
            scope=TenantScope(),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        with patch.dict(os.environ, {"CODEX_WEB_IDENTITY_MODE": "enforced"}):
            with TestClient(self._app()) as client:
                response = client.get(
                    "/api/identity",
                    headers={"Authorization": f"Bearer {credentials.session_token}"},
                )
                self.assertEqual(response.status_code, 200)
                serialized = response.text
                self.assertNotIn("session_token_hash", serialized)
                self.assertNotIn("refresh_token_hash", serialized)
                self.assertNotIn("csrf_token_hash", serialized)
                self.assertNotIn(credentials.session_token, serialized)


if __name__ == "__main__":
    unittest.main()
