from __future__ import annotations

import sys

from codex_web import application as _application
from codex_web.observability import install_observability
from codex_web.runtime import core as _runtime
from codex_web.runtime.bots import install_bot_runtime
from codex_web.runtime.deployment import install_deployment_configuration
from codex_web.runtime.workers import install_worker_supervisor
from codex_web.storage.configuration_state import install_configuration_state
from codex_web.storage.operational_state import install_operational_state


# Complete state wiring before lifecycle startup so queue recovery, bot
# runtimes, and autonomy workers read SQLite-authoritative state from their
# first cycle.
install_configuration_state(_application.app, _runtime)
install_operational_state(_application.app, _runtime)

# Deployment-specific fallbacks are resolved only after persisted integration
# settings are available. This preserves legacy installations while keeping new
# deployments generic and explicitly configured.
install_deployment_configuration(_application.app, _runtime)

# Codex and autonomy runtime ownership is composed in application.py so direct
# application imports see the same implementation as this executable entrypoint.
# Structured logs and local diagnostics attach before long-lived provider and
# worker tasks begin.
install_observability(_application.app, _runtime)

# Active provider connection lifecycle is extracted from the compatibility
# runtime. Install it before worker supervision so startup synchronizes the
# extracted Slack/Telegram runtime rather than the legacy class instance.
install_bot_runtime(_application.app, _runtime)

# The executable entrypoint owns lifecycle supervision. The active Codex and
# autonomy implementations are already present on the compatibility host.
install_worker_supervisor(_application.app, _runtime)


if __name__ == "__main__":
    _application.main()
else:
    # Preserve the historical `import server` API while application.py becomes
    # composition-only. Existing tests and integrations can keep patching the
    # runtime module during the incremental decomposition.
    sys.modules[__name__] = _runtime
