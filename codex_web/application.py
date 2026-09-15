from __future__ import annotations

from codex_web.api.approvals import build_approvals_router
from codex_web.api.integrations import build_integrations_router
from codex_web.api.projects import build_projects_router
from codex_web.api.runtime import build_runtime_router
from codex_web.api.system import build_system_router
from codex_web.api.ui import build_ui_router
from codex_web.composition import replace_routes
from codex_web.executive_integration import install_executive_integrated
from codex_web.integrations.webhook_security import install_webhook_security
from codex_web.paths import PROJECTS_FILE
from codex_web.runtime import core
from codex_web.services.approvals import ApprovalService
from codex_web.services.projects import ProjectService
from codex_web.services.runtime import RuntimeService
from codex_web.storage.json_files import atomic_write_text, state_file_lock
from codex_web.storage.projects import ProjectRepository


# Keep one FastAPI application and one runtime lifecycle while domains are
# extracted. The legacy runtime is now a compatibility host for the portions
# that have not moved yet, rather than the place new API behavior is added.
app = core.app

# Shared persistence primitives are owned outside the legacy runtime. Existing
# unextracted state helpers resolve these globals at call time, so they use the
# same atomic implementation without maintaining a second persistence path.
core._state_file_lock = state_file_lock
core._atomic_write_text = atomic_write_text

project_repository = ProjectRepository(PROJECTS_FILE)
project_service = ProjectService(project_repository)
runtime_service = RuntimeService(core)
approval_service = ApprovalService(core)

# Legacy code still needing project state consumes the extracted repository.
core._load_projects = project_repository.load
core._save_projects = project_repository.save

install_webhook_security(core)

EXTRACTED_ROUTE_COUNTS = {
    "projects": replace_routes(
        app,
        build_projects_router(project_service),
        paths={"/api/projects", "/api/projects/{project_id}"},
        key="projects",
    ),
    "runtime": replace_routes(
        app,
        build_runtime_router(runtime_service),
        paths={
            "/api/status",
            "/api/healthz",
            "/api/account/rate-limits",
            "/api/models",
        },
        key="runtime",
    ),
    "approvals": replace_routes(
        app,
        build_approvals_router(approval_service),
        paths={"/api/approvals", "/api/approvals/{request_id}"},
        key="approvals",
    ),
    "ui": replace_routes(
        app,
        build_ui_router(core),
        paths={"/", "/devstatus", "/devhealth", "/ws"},
        key="ui",
    ),
    "system": replace_routes(
        app,
        build_system_router(core),
        paths={
            "/api/livez",
            "/api/auth-verifier",
            "/api/diagnostics",
            "/api/diagnostics/route-test",
        },
        key="system",
    ),
    "integrations": replace_routes(
        app,
        build_integrations_router(core),
        paths={
            "/api/integrations/agent-presence",
            "/api/integrations/gitlab",
            "/api/integrations/gitlab/support-servicedesk/sweep",
        },
        key="integrations",
    ),
}
app.state.extracted_route_counts = EXTRACTED_ROUTE_COUNTS

core.executive_service = install_executive_integrated(app, core)


def main() -> None:
    core.main()
