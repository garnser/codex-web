from __future__ import annotations

import sys

from codex_web import application as _application
from codex_web.observability import install_observability
from codex_web.runtime import core as _runtime
from codex_web.runtime.bots import install_bot_runtime
from codex_web.runtime.deployment import install_deployment_configuration
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

# Codex, autonomy and bot routing/delivery ownership is composed in
# application.py so direct application imports see the same implementation as
# this executable entrypoint. Structured logs attach before long-lived tasks.
install_observability(_application.app, _runtime)

# Active provider connection lifecycle shares the exact async Slack/Telegram
# clients used by routing/delivery and management services.
install_bot_runtime(
    _application.app,
    _runtime,
    slack_client=_application.app.state.slack_client,
    telegram_client=_application.app.state.telegram_client,
)

# Lifecycle supervision is composed once in application.py through
# RuntimeSupervisor. The server module is only a CLI/compatibility edge.


if __name__ == "__main__":
    _application.main()
else:
    # Preserve the historical `import server` API while application.py becomes
    # composition-only. Existing tests and integrations can keep patching the
    # runtime module during the incremental decomposition.
    sys.modules[__name__] = _runtime
