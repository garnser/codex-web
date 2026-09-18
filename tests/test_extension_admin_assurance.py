from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.extensions import build_extensions_router
from codex_web.extension_packages import LocalExtensionPackageCatalog
from codex_web.extensions import (
    ExtensionConfigureRequest,
    ExtensionGrantRequest,
    ExtensionInstallRequest,
    ExtensionManifest,
    ExtensionRemoveRequest,
    ExtensionType,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.extensions import ExtensionService
from codex_web.services.identity import AuthorizationError
from codex_web.storage.extensions import ExtensionStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def manifest(
    extension_type: ExtensionType,
    *,
    extension_id: str,
) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "publisher": {"id": "com.example", "name": "Example"},
            "provenance": {
                "source": f"local-test:{extension_id}:1.0.0",
                "digest": "sha256:" + "a" * 64,
            },
            "compatibility": {"codex_web": ">=3.0.0 <4.0.0"},
            "types": [extension_type.value],
            "capabilities": {
                "requested": ["extension.read"],
                "mandatory": ["extension.read"],
            },
            "entrypoints": {
                extension_type.value: f"extension:{extension_type.value}",
            },
        }
    )


class ExtensionAdminAssuranceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.service = ExtensionService(
            ExtensionStateStore(SQLiteStateStore(root / "state.sqlite3"))
        )
        self.package_catalog = LocalExtensionPackageCatalog(root / "packages")
        self.low_admin = AuthenticationActor(
            identity_id="human-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        self.mfa_admin = self.low_admin.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )
        self.service_admin = AuthenticationActor(
            identity_id="extension-admin-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("extensions:admin",),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _request(self) -> ExtensionInstallRequest:
        item_manifest = manifest(
            ExtensionType.TASK_SOURCE,
            extension_id="com.example.assurance-test",
        )
        return ExtensionInstallRequest(
            manifest=item_manifest,
            observed_digest=item_manifest.provenance.digest,
        )

    def test_low_assurance_human_admin_can_read_and_discover_but_not_mutate(self) -> None:
        self.assertEqual(self.service.list(self.low_admin), [])
        self.service._require_admin_role(self.low_admin)

        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.low_admin
            return await call_next(request)

        app.include_router(
            build_extensions_router(self.service, self.package_catalog)
        )
        with TestClient(app) as client:
            discovery = client.get("/api/extensions/packages")
        self.assertEqual(discovery.status_code, 200)
        self.assertEqual(discovery.json()["items"], [])

        with self.assertRaisesRegex(AuthorizationError, "mfa"):
            self.service.install(self._request(), actor=self.low_admin)

    def test_low_assurance_human_admin_is_blocked_at_shared_mutation_boundary(self) -> None:
        installed = self.service.install(self._request(), actor=self.mfa_admin)

        operations = (
            lambda: self.service.configure(
                installed.id,
                ExtensionConfigureRequest(),
                actor=self.low_admin,
            ),
            lambda: self.service.grant(
                installed.id,
                ExtensionGrantRequest(capabilities=("extension.read",)),
                actor=self.low_admin,
            ),
            lambda: self.service.enable(installed.id, actor=self.low_admin),
            lambda: self.service.disable(
                installed.id,
                actor=self.low_admin,
                reason="operator disable",
            ),
            lambda: self.service.quarantine(
                installed.id,
                actor=self.low_admin,
                reason="operator quarantine",
            ),
            lambda: self.service.clear_quarantine(
                installed.id,
                actor=self.low_admin,
                reason="clear",
            ),
            lambda: self.service.remove(
                installed.id,
                ExtensionRemoveRequest(reason="remove"),
                actor=self.low_admin,
            ),
        )

        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(AuthorizationError, "mfa"):
                    operation()

    def test_mfa_human_admin_and_scoped_service_can_administer_extensions(self) -> None:
        first = self.service.install(self._request(), actor=self.mfa_admin)
        self.assertEqual(first.manifest.id, "com.example.assurance-test")

        second_manifest = manifest(
            ExtensionType.TASK_SOURCE,
            extension_id="com.example.service-assurance-test",
        )
        second = self.service.install(
            ExtensionInstallRequest(
                manifest=second_manifest,
                observed_digest=second_manifest.provenance.digest,
            ),
            actor=self.service_admin,
        )
        self.assertEqual(
            second.manifest.id,
            "com.example.service-assurance-test",
        )

    def test_health_reporting_remains_separately_scoped_from_admin_mutation(self) -> None:
        self.service._require_health_reporter(self.low_admin)

        health_service = self.service_admin.model_copy(
            update={"service_scopes": ("extensions:health",)}
        )
        self.service._require_health_reporter(health_service)

        unscoped = self.service_admin.model_copy(update={"service_scopes": ()})
        with self.assertRaisesRegex(AuthorizationError, "extensions:health"):
            self.service._require_health_reporter(unscoped)


if __name__ == "__main__":
    unittest.main()
