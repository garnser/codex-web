from __future__ import annotations

import unittest

from codex_web.extensions import (
    ExtensionInstallation,
    ExtensionDeploymentMode,
    ExtensionLifecycleState,
    ExtensionManifest,
    ExtensionPackageVerification,
    ExtensionSignatureStatus,
)
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.services.extension_lifecycle_runtime import reconcile_extension_runtime


class RuntimeStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, AuthenticationActor]] = []

    def unregister_installation(
        self,
        installation_id: str,
        *,
        actor: AuthenticationActor,
    ) -> None:
        self.calls.append((installation_id, actor))


def actor() -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="identity-admin",
        principal_kind=PrincipalKind.SERVICE,
        organization_id="org-1",
        workspace_id="ws-1",
        assurance=AuthenticationAssurance.SERVICE_TOKEN,
        service_scopes=("extensions:admin",),
    )


def installation(lifecycle: ExtensionLifecycleState) -> ExtensionInstallation:
    manifest = ExtensionManifest.model_validate(
        {
            "id": "com.example.lifecycle-test",
            "version": "1.0.0",
            "publisher": {"id": "com.example", "name": "Example"},
            "provenance": {
                "source": "test",
                "digest": "sha256:" + "a" * 64,
            },
            "compatibility": {"codex_web": ">=3.0.0 <4.0.0"},
            "types": ["task_source"],
            "entrypoints": {"task_source": "example:source"},
        }
    )
    return ExtensionInstallation(
        id="ext-installation-1",
        organization_id="org-1",
        workspace_id="ws-1",
        manifest=manifest,
        deployment_mode=ExtensionDeploymentMode.SELF_HOSTED,
        package_verification=ExtensionPackageVerification(
            digest_verified=True,
            signature_status=ExtensionSignatureStatus.UNSIGNED,
            verifier="test",
            observed_digest=manifest.provenance.digest,
        ),
        lifecycle=lifecycle,
        installed_by="identity-admin",
    )


class ExtensionLifecycleRuntimeTests(unittest.TestCase):
    def test_enabled_installation_keeps_runtime_registration(self) -> None:
        runtime = RuntimeStub()
        current_actor = actor()

        changed = reconcile_extension_runtime(
            runtime,
            installation(ExtensionLifecycleState.ENABLED),
            actor=current_actor,
        )

        self.assertFalse(changed)
        self.assertEqual(runtime.calls, [])

    def test_every_non_enabled_lifecycle_unregisters_runtime_registration(self) -> None:
        current_actor = actor()
        for lifecycle in ExtensionLifecycleState:
            if lifecycle == ExtensionLifecycleState.ENABLED:
                continue
            with self.subTest(lifecycle=lifecycle):
                runtime = RuntimeStub()
                changed = reconcile_extension_runtime(
                    runtime,
                    installation(lifecycle),
                    actor=current_actor,
                )
                self.assertTrue(changed)
                self.assertEqual(
                    runtime.calls,
                    [("ext-installation-1", current_actor)],
                )


if __name__ == "__main__":
    unittest.main()
