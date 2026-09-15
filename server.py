from __future__ import annotations

import sys

from codex_web import application as _application
from codex_web.executive_integration import install_executive_integrated


# Compose optional feature routers around the core application in one place.
# install_executive_integrated is idempotent so this is safe when `python server.py`
# causes uvicorn to import `server:app` a second time.
_application.executive_service = install_executive_integrated(_application.app, _application)


if __name__ == "__main__":
    _application.main()
else:
    # Preserve the historical `import server` API used by tests and integrations,
    # including monkey-patching of private helpers. Importers receive the actual
    # application module rather than a proxy wrapper.
    sys.modules[__name__] = _application
