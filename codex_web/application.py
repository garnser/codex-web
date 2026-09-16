from __future__ import annotations

from codex_web.api.approvals import build_approvals_router
from codex_web.api.bots import build_bots_router
from codex_web.api.context import build_context_router
from codex_web.api.integrations import build_integrations_router
from codex_web.api.projects import build_projects_router
from codex_web.api.runtime import build_runtime_router
from codex_web.api.system import build_system_router
from codex_web.api.threads import build_threads_router
from codex_web.api.turns import build_turns_router
from codex_web.api.ui import build_ui_router
from codex_web.api.work_items import build_work_items_router
from codex_web.composition import replace_routes
from codex_web.executive_integration import install_executive_integrated
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.integrations.slack_client import SlackClient
from codex_web.integrations.telegram_client import TelegramClient
from codex_web.integrations.webhook_security import install_webhook_security
from codex_web.paths import (
    ACTIVE_TURNS_FILE,
    PROJECTS_FILE,
    STATE_DB_FILE,
    THREAD_SETTINGS_FILE,
    WORK_ITEM_STATES_FILE,
)
from codex_web.runtime import core
from codex_web.runtime.codex import install_codex_runtime
from codex_web.services.approvals import ApprovalService
from codex_web.services.autonomy import install_autonomy_service
from codex_web.services.bot_delivery import install_bot_delivery_service
from codex_web.services.bot_routing import install_bot_routing_service
from codex_web.services.bots import BotService
from codex_web.services.context import ContextCompactionService
from codex_web.services.gitlab import GitLabService
from codex_web.services.projects import ProjectService
from codex_web.services.runtime import RuntimeService
from codex_web.services.threads import ThreadService
from codex_web.services.turns import TurnService
from codex_web.services.work_items import WorkItemService
from codex_web.storage.json_files import atomic_write_text, state_file_lock
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.runtime_state import RuntimeStateRepositories
from codex_web.storage.sqlite_state import SQLiteStateStore


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
state_store = SQLiteStateStore(STATE_DB_FILE)
runtime_state = RuntimeStateRepositories(
    state_store,
    thread_settings_file=THREAD_SETTINGS_FILE,
    active_turns_file=ACTIVE_TURNS_FILE,
    work_item_states_file=WORK_ITEM_STATES_FILE,
)
project_service = ProjectService(project_repository)
runtime_service = RuntimeService(core)
approval_service = ApprovalService(core)
thread_service = ThreadService(core)
turn_service = TurnService(core)
context_service = ContextCompactionService(core)
gitlab_client = GitLabClient()
work_item_service = WorkItemService(core, gitlab_client)
gitlab_service = GitLabService(core, gitlab_client)

# Legacy code still needing project/runtime state consumes the extracted
# repositories. SQLite is primary for high-churn runtime documents; the
# repository mirrors legacy JSON on every write during the migration window so
# rolling back to the previous release remains safe.
core._load_projects = project_repository.load
core._save_projects = project_repository.save
core._load_thread_settings = runtime_state.thread_settings.load
core._save_thread_settings = runtime_state.thread_settings.save
core._load_active_turns = runtime_state.active_turns.load
core._save_active_turns = runtime_state.active_turns.save
core._load_work_item_states = runtime_state.work_item_states.load
core._save_work_item_states = runtime_state.work_item_states.save
app.state.sqlite_state_store = state_store
app.state.runtime_state_repositories = runtime_state

# Compose extracted runtime ownership here rather than in server.py so direct
# application imports and tests observe the same implementation as the CLI
# entrypoint. The installers are idempotent and preserve the compatibility
# attributes expected by services that have not moved out of core.py yet.
codex_runtime = install_codex_runtime(app, core)
autonomy_service = install_autonomy_service(app, core)

# Bot routing/delivery share the same async provider clients used by management
# and long-lived runtime paths. Rebind the historical host entrypoints before
# routers or provider workers can receive traffic.
slack_client = SlackClient()
telegram_client = TelegramClient()
bot_delivery_service = install_bot_delivery_service(
    app,
    core,
    slack_client=slack_client,
    telegram_client=telegram_client,
)
bot_routing_service = install_bot_routing_service(app, core, bot_delivery_service)
bot_service = BotService(
    core,
    slack_client=slack_client,
    routing_service=bot_routing_service,
)
app.state.slack_client = slack_client
app.state.telegram_client = telegram_client

# Preserve the legacy ServiceDesk hook used by both the route and the worker,
# but route it through the extracted async GitLab service.
core._run_support_servicedesk_sweep_once = gitlab_service.sweep_support_servicedesk
app.state.gitlab_service = gitlab_service

install_webhook_security(core)
previous_context_service = getattr(app.state, "context_compaction_service", None)
if previous_context_service is not None:
    core.hub.unsubscribe(previous_context_service.observe)
core.hub.subscribe(context_service.observe)
app.state.context_compaction_service = context_service

EXTRACTED_ROUTE_COUNTS = {
    "projects": replace_routes(
        app,
        build_projects_router(project_service),
        paths={"/api/projects", "/api/projects/{project_id}"},
        key="projects",
    ),
    "threads": replace_routes(
        app,
        build_threads_router(thread_service),
        paths={
            "/api/threads",
            "/api/threads/{thread_id}",
            "/api/threads/{thread_id}/name",
            "/api/threads/{thread_id}/settings",
            "/api/thread-settings",
            "/api/threads/{thread_id}/primary",
            "/api/threads/{thread_id}/primary-channel",
            "/api/threads/{thread_id}/archive",
            "/api/threads/{thread_id}/unarchive",
            "/api/turns/interrupt",
        },
        key="threads",
    ),
    "turns": replace_routes(
        app,
        build_turns_router(turn_service),
        paths={
            "/api/threads/{thread_id}/resume",
            "/api/threads/{thread_id}/replace",
            "/api/threads/{thread_id}/turns",
            "/api/threads/{thread_id}/queue",
            "/api/threads/{thread_id}/queue/steer",
            "/api/threads/{thread_id}/queue/{queued_id}/steer",
        },
        key="turns",
    ),
    "context": replace_routes(
        app,
        build_context_router(context_service),
        paths={
            "/api/threads/{thread_id}/context",
            "/api/threads/{thread_id}/compact",
        },
        key="context",
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
    "bots": replace_routes(
        app,
        build_bots_router(bot_service),
        paths={
            "/api/bots",
            "/api/bots/connections",
            "/api/bots/bindings",
            "/api/bots/channels",
            "/api/bots/inbound",
        },
        key="bots",
    ),
    "work-items": replace_routes(
        app,
        build_work_items_router(work_item_service),
        paths={
            "/api/work-items",
            "/api/work-items/sync-from-gitlab",
            "/api/work-items/{ref:path}",
            "/api/work-items/{ref:path}/handoff",
            "/api/work-items/{ref:path}/ack",
            "/api/work-items/{ref:path}/progress",
        },
        key="work-items",
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
