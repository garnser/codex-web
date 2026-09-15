from __future__ import annotations

from codex_web.api.system import install_system_routes
from codex_web.executive_integration import install_executive_integrated
from codex_web.integrations.webhook_security import install_webhook_security
from codex_web.runtime import core


# Application composition lives here. The runtime module still contains the
# legacy implementation while domains are extracted incrementally, but feature
# registration and cross-cutting policy no longer need to grow that module.
app = core.app

install_webhook_security(core)
install_system_routes(app, core)
core.executive_service = install_executive_integrated(app, core)


def main() -> None:
    core.main()
