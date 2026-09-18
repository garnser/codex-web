from __future__ import annotations

from typing import Protocol

from codex_web.extensions import ExtensionInstallation, ExtensionLifecycleState
from codex_web.identity import AuthenticationActor


class ExtensionRuntimeUnregister(Protocol):
    def unregister_installation(
        self,
        installation_id: str,
        *,
        actor: AuthenticationActor,
    ) -> None: ...


def reconcile_extension_runtime(
    runtime: ExtensionRuntimeUnregister,
    installation: ExtensionInstallation,
    *,
    actor: AuthenticationActor,
) -> bool:
    """Remove process-local adapters when canonical lifecycle forbids dispatch.

    Canonical extension state remains the authority. Runtime registrations are
    only an execution cache/bridge and must not outlive an enabled installation.
    The helper is intentionally idempotent because lifecycle mutations, health
    circuit breakers and capability revocation may all converge on the same
    inactive state.

    Returns True when unregister was requested, False while the installation is
    still enabled.
    """
    if installation.lifecycle == ExtensionLifecycleState.ENABLED:
        return False
    runtime.unregister_installation(installation.id, actor=actor)
    return True
