from __future__ import annotations

import sys

from codex_web import application as _application
from codex_web.runtime import core as _runtime
from codex_web.runtime.bots import install_bot_runtime
from codex_web.runtime.codex import install_codex_runtime
from codex_web.runtime.deployment import install_deployment_configuration
from codex_web.runtime.workers import install_worker_supervisor
from codex_web.services.autonomy import install_autonomy_service
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

# The Codex subprocess/JSON-RPC lifecycle is now owned outside the compatibility
# runtime. Install it before any worker or provider runtime can start making
# requests so every service observes the extracted client from first startup.
install_codex_runtime(_application.app, _runtime)

# Active provider connection lifecycle is extracted from the compatibility
# runtime. Install it before worker supervision so startup synchronizes the
# extracted Slack/Telegram runtime rather than the legacy class instance.
install_bot_runtime(_application.app, _runtime)

# Autonomous work-item decisions are owned by a dedicated service. Install it
# before worker supervision so periodic workers invoke the extracted cycle
# implementations from their first iteration.
install_autonomy_service(_application.app, _runtime)

# The executable entrypoint owns lifecycle composition. This replaces the
# compatibility runtime's startup/shutdown handlers with an extracted worker
# supervisor while preserving the single FastAPI application instance.
install_worker_supervisor(_application.app, _runtime)


if __name__ == "__main__":
    _application.main()
else:
    # Preserve the historical `import server` API while application.py becomes
    # composition-only. Existing tests and integrations can keep patching the
    # runtime module during the incremental decomposition.
    sys.modules[__name__] = _runtime
