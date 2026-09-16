from __future__ import annotations

import sys

from codex_web import application as _application
from codex_web.runtime import core as _runtime
from codex_web.runtime.workers import install_worker_supervisor
from codex_web.storage.operational_state import install_operational_state


# Complete state wiring before lifecycle startup so queue recovery and bot
# runtimes read the SQLite-authoritative repositories from their first cycle.
install_operational_state(_application.app, _runtime)

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
