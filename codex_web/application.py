from __future__ import annotations

from codex_web.api.approvals import build_approvals_router
from codex_web.api.bots import build_bots_router
from codex_web.api.context import build_context_router
from codex_web.api.integrations import build_integrations_router
from codex_web.api.projects import build_projects_router
from codex_web.api.runtime import build_runtime_router
from codex_web.api.slack import build_slack_router
from codex_web.api.system import build_system_router
from codex_web.api.telegram import build_telegram_router
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
    THREAD_INDEX_FILE,
    THREAD_SETTINGS_FILE,
    WORK_ITEM_STATES_FILE,
)
from codex_web.runtime import core
from codex_web.runtime.bots import install_bot_runtime
from codex_web.runtime.codex import install_codex_runtime
from codex_web.runtime.execution import install_turn_execution_service
from codex_web.services.approvals import ApprovalService
from codex_web.services.autonomy import install_autonomy_service
from codex_web.services.agent_channel_preferences import install_agent_channel_preference_service
from codex_web.services.bot_binding_selection import install_bot_binding_selection_service
from codex_web.services.bot_connections import install_bot_connection_service
from codex_web.services.bot_delivery import install_bot_delivery_service
from codex_web.services.bot_routing import install_bot_routing_service
from codex_web.services.bots import BotService
from codex_web.services.context import ContextCompactionService
from codex_web.services.gitlab import install_gitlab_service
from codex_web.services.projects import ProjectService
from codex_web.services.runtime import RuntimeService
from codex_web.services.runtime_supervisor import install_runtime_supervisor
from codex_web.services.slack_provider import install_slack_provider_service
from codex_web.services.thread_recovery import install_thread_recovery_service
from codex_web.services.thread_execution_settings import install_thread_execution_settings_service
from codex_web.services.threads import ThreadService
from codex_web.services.turn_queue_policy import install_turn_queue_policy
from codex_web.services.turns import TurnService
from codex_web.services.work_item_state import install_work_item_state_machine
from codex_web.services.work_item_timing import install_work_item_timing_policy
from codex_web.services.work_item_contracts import install_work_item_contract_service
from codex_web.services.work_items import WorkItemService
from codex_web.storage.auxiliary_state import install_auxiliary_state
from codex_web.storage.json_files import atomic_write_text, state_file_lock
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.runtime_state import RuntimeStateRepositories
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.thread_index import install_thread_index_repository


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
thread_index_repository = install_thread_index_repository(
    app,
    core,
    store=state_store,
    legacy_path=THREAD_INDEX_FILE,
)
project_service = ProjectService(project_repository)
runtime_service = RuntimeService(core)
approval_service = ApprovalService(core)
thread_service = ThreadService(core)
context_service = ContextCompactionService(core)
gitlab_client = GitLabClient()
work_item_state_machine = install_work_item_state_machine(app, core, gitlab_client)
work_item_contract_service = install_work_item_contract_service(app, core)
work_item_service = WorkItemService(core, gitlab_client, work_item_state_machine)
gitlab_service = install_gitlab_service(app, core, gitlab_client)

# Legacy code still needing project/runtime state consumes the extracted
# repositories. SQLite is primary for mutable runtime documents; repositories
# mirror legacy JSON on every write during the migration window so rolling back
# to the previous release remains safe.
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
auxiliary_state = install_auxiliary_state(app, core)

# Compose extracted runtime ownership here rather than in server.py so direct
# application imports and tests observe the same implementation as the CLI
# entrypoint. The installers are idempotent and preserve the compatibility
# attributes expected by services that have not moved out of core.py yet.
codex_runtime = install_codex_runtime(app, core)
thread_execution_settings_service = install_thread_execution_settings_service(app, core)
turn_execution_service = install_turn_execution_service(app, core)
work_item_timing_policy = install_work_item_timing_policy(app, core)
autonomy_service = install_autonomy_service(app, core)
turn_queue_policy = install_turn_queue_policy(app, core)
turn_service = TurnService(core)

# Preserve the small historical function surface still used by direct
# `import server` callers while the actual implementations live in services.
# These are aliases to extracted owners, not duplicate legacy implementations.
core._default_thread_message_limit = thread_service.default_message_limit
core._coerce_thread_message_limit = thread_service.coerce_message_limit
core._trim_thread_messages = thread_service.trim_messages
core.read_thread = thread_service.read
core.resume_thread = turn_service.resume
core.start_turn = turn_service.start

# Bot routing/delivery share the same async provider clients used by management
# and long-lived runtime paths. Rebind the historical host entrypoints before
# routers or provider workers can receive traffic.
slack_client = SlackClient()
telegram_client = TelegramClient()
bot_connection_service = install_bot_connection_service(app, core)
bot_binding_selection_service = install_bot_binding_selection_service(app, core)
agent_channel_preference_service = install_agent_channel_preference_service(app, core)
bot_runtime = install_bot_runtime(
    app,
    core,
    slack_client=slack_client,
    telegram_client=telegram_client,
)
bot_delivery_service = install_bot_delivery_service(
    app,
    core,
    slack_client=slack_client,
    telegram_client=telegram_client,
)
thread_recovery_service = install_thread_recovery_service(app, core)
bot_routing_service = install_bot_routing_service(app, core, bot_delivery_service)
slack_provider_service = install_slack_provider_service(
    app,
    core,
    slack_client=slack_client,
    routing_service=bot_routing_service,
)
bot_service = BotService(
    core,
    slack_client=slack_client,
    routing_service=bot_routing_service,
)
app.state.slack_client = slack_client
app.state.telegram_client = telegram_client

# Replace the legacy core startup/shutdown callbacks after all runtime and
# provider services have been composed. The supervisor keeps the historical
# task globals populated for diagnostics while owning cancellation and shutdown.
runtime_supervisor = install_runtime_supervisor(app, core)

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
            "/api/operations",
            "/api/recovery/resume",
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
    "slack": replace_routes(
        app,
        build_slack_router(slack_provider_service),
        paths={"/bots/slack/events"},
        key="slack",
    ),
    "telegram": replace_routes(
        app,
        build_telegram_router(core, bot_routing_service),
        paths={"/bots/telegram/webhook"},
        key="telegram",
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
        build_integrations_router(core, gitlab_service),
        paths={
            "/api/integrations/agent-presence",
            "/api/integrations/gitlab",
            "/api/integrations/gitlab/support-servicedesk/sweep",
            "/bots/gitlab/events",
        },
        key="integrations",
    ),
}
app.state.extracted_route_counts = EXTRACTED_ROUTE_COUNTS

core.executive_service = install_executive_integrated(app, core)


def main() -> None:
    core.main()
