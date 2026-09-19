from __future__ import annotations

import asyncio
import os

from codex_web.api.action_intents import build_action_intents_router
from codex_web.api.agent_providers import build_agent_providers_router
from codex_web.api.agent_routing import build_agent_routing_router
from codex_web.api.agent_runtime_usage import build_agent_runtime_usage_router
from codex_web.api.agent_sessions import build_agent_sessions_router
from codex_web.api.action_providers import build_action_providers_router
from codex_web.api.approvals import build_approvals_router
from codex_web.api.approval_requests import build_approval_requests_router
from codex_web.api.authorization import install_api_authorization
from codex_web.api.attention import build_attention_router
from codex_web.api.artifact_evidence import build_artifact_evidence_router
from codex_web.api.authority import build_authority_router
from codex_web.api.autonomy import build_autonomy_router
from codex_web.api.autonomy_control_center import build_autonomy_control_center_router
from codex_web.api.autonomy_audit import build_autonomy_audit_router
from codex_web.api.bots import build_bots_router
from codex_web.api.configuration import build_configuration_router
from codex_web.api.capacity import build_capacity_router
from codex_web.api.crypto_keys import build_crypto_keys_router
from codex_web.api.context import build_context_router
from codex_web.api.definitions import build_definitions_router
from codex_web.api.data_governance import build_data_governance_router
from codex_web.api.business_context import build_business_context_router
from codex_web.api.business_data_sources import build_business_data_sources_router
from codex_web.api.business_kpis import build_business_kpis_router
from codex_web.api.company_operations import build_company_operations_router
from codex_web.api.decisions import build_decisions_router
from codex_web.api.entitlements import build_entitlements_router
from codex_web.api.evaluations import build_evaluations_router
from codex_web.api.executive_management import build_executive_management_router
from codex_web.api.extensions import build_extensions_router
from codex_web.api.execution_workspaces import build_execution_workspaces_router
from codex_web.api.execution_workers import build_execution_workers_router
from codex_web.api.integrations import build_integrations_router
from codex_web.api.incidents import build_incidents_router
from codex_web.api.goals import build_goals_router
from codex_web.api.metrics import build_metrics_router
from codex_web.api.goal_decompositions import build_goal_decompositions_router
from codex_web.api.input_plugins import build_input_plugins_router
from codex_web.api.model_gateway import build_model_gateway_router
from codex_web.api.orchestration import build_orchestration_router
from codex_web.api.organizational_memory import build_organizational_memory_router
from codex_web.api.identity import build_identity_router, install_identity_middleware
from codex_web.api.projects import build_projects_router
from codex_web.api.provider_capacity import build_provider_capacity_router
from codex_web.api.resources import build_resources_router
from codex_web.api.recovery import build_recovery_router
from codex_web.api.releases import build_releases_router
from codex_web.api.upgrades import build_upgrades_router
from codex_web.api.secrets import build_secrets_router
from codex_web.api.security import build_security_router
from codex_web.api.runtime import build_runtime_router
from codex_web.api.scheduler import build_scheduler_router
from codex_web.api.slack import build_slack_router
from codex_web.api.system import build_system_router
from codex_web.api.telegram import build_telegram_router
from codex_web.api.threads import build_threads_router
from codex_web.api.turns import build_turns_router
from codex_web.api.ui import build_ui_router
from codex_web.api.work_items import build_work_items_router
from codex_web.api.work_graph import build_work_graph_router
from codex_web.canonical_events import CanonicalEventType
from codex_web.composition import replace_routes
from codex_web.configuration import ConfigurationContext
from codex_web.executive_integration import install_executive_integrated
from codex_web.extension_packages import LocalExtensionPackageCatalog
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.integrations.slack_client import SlackClient
from codex_web.integrations.telegram_client import TelegramClient
from codex_web.integrations.webhook_security import install_webhook_security
from codex_web.model_providers import AnthropicModelProviderAdapter, OpenAIModelProviderAdapter
from codex_web.key_backends import LocalFileKeyBackend
from codex_web.execution_workspace_backend import LocalGitWorkspaceBackend
from codex_web.local_execution_backend import BubblewrapExecutionBackend
from codex_web.execution_workers import ExecutionRuntimeBinding, WorkerCapability
from codex_web.paths import (
    ACTIVE_TURNS_FILE,
    ARTIFACT_CONTENT_DIR,
    EXECUTION_WORKSPACE_DIR,
    EXTENSION_PACKAGE_DIR,
    KEY_MATERIAL_DIR,
    PROJECTS_FILE,
    SECRET_MATERIAL_DIR,
    STATE_DB_FILE,
    THREAD_INDEX_FILE,
    THREAD_SETTINGS_FILE,
    WORK_ITEM_STATES_FILE,
)
from codex_web.runtime import core
from codex_web.runtime.bots import install_bot_runtime
from codex_web.runtime.codex import install_codex_runtime
from codex_web.runtime.execution import install_turn_execution_service
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.agent_providers import AgentProviderService
from codex_web.agent_providers import AgentProviderHealth, AgentProviderUpsert
from codex_web.services.agent_routing import AgentRoutingService
from codex_web.services.agent_routing_configuration import install_agent_routing_configuration
from codex_web.services.agent_routing_definitions import install_agent_routing_definitions
from codex_web.services.agent_runtime import AgentRuntimeRegistry, AgentSessionService
from codex_web.services.agent_runtime_telemetry import AgentRuntimeTelemetryService
from codex_web.services.agent_session_trace import AgentSessionTraceService
from codex_web.services.action_providers import ActionExecutionService, ActionProviderRegistry
from codex_web.services.approvals import ApprovalService
from codex_web.services.approval_requests import ApprovalRequestService
from codex_web.services.attention import AttentionService, install_attention_event_bridges
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.artifact_content import ArtifactContentRegistry, ArtifactContentService
from codex_web.services.artifact_content_configuration import (
    ARTIFACT_CONTENT_BACKEND_CONFIG,
    install_artifact_content_configuration,
)
from codex_web.services.authority_policy_explorer import AuthorityPolicyExplorerService
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.autonomy import install_autonomy_service
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.autonomy_control_center import AutonomyControlCenterService
from codex_web.services.autonomy_policy import AutonomyPolicyService
from codex_web.services.autonomy_audit import AutonomyAuditService
from codex_web.services.watchdog_dispatch import install_watchdog_dispatch_policy
from codex_web.services.agent_channel_preferences import install_agent_channel_preference_service
from codex_web.services.bot_binding_selection import install_bot_binding_selection_service
from codex_web.services.bot_connections import install_bot_connection_service
from codex_web.services.bot_delivery import install_bot_delivery_service
from codex_web.services.bot_routing import install_bot_routing_service
from codex_web.services.bots import BotService
from codex_web.services.configuration import ConfigurationService
from codex_web.services.capacity import CapacityService
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.coordination import StateStoreCoordinationBackend
from codex_web.services.event_transport import (
    EventTransportRuntime,
    build_event_transport,
)
from codex_web.services.replicated_ownership import ReplicatedOwnershipService
from codex_web.services.codex_auth_delegation import CodexAuthDelegationService
from codex_web.services.anthropic_auth_delegation import AnthropicAuthDelegationService
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.services.claude_agent_runtime import ClaudeAgentRuntimeAdapter
from codex_web.services.codex_worker_configuration import CODEX_WORKER_ACCESS_TOKEN_CONFIG, install_codex_worker_configuration
from codex_web.services.anthropic_worker_configuration import ANTHROPIC_WORKER_API_KEY_CONFIG, install_anthropic_worker_configuration
from codex_web.services.agent_model_egress import (
    AgentRuntimeModelEgressEndpoint,
    model_egress_endpoints_from_base_urls,
)
from codex_web.services.codex_worker_session import AssignmentBoundCodexSessionManager
from codex_web.services.code_hosts import CodeHostRegistry, CodeHostService
from codex_web.services.claude_worker_session import AssignmentBoundClaudeSessionManager
from codex_web.services.context import ContextCompactionService
from codex_web.services.crypto_keys import CryptoKeyService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_role_definitions import install_execution_role_definitions
from codex_web.services.executive_roles import install_executive_role_definitions
from codex_web.services.executive_management import ExecutiveManagementService
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.business_context import BusinessContextService
from codex_web.services.business_data_sources import (
    BusinessDataSourceRegistry,
    BusinessDataSourceService,
)
from codex_web.services.business_kpis import BusinessKPIService
from codex_web.services.company_operations import CompanyOperationsService
from codex_web.services.decisions import DecisionService
from codex_web.services.decision_deliberation import DecisionDeliberationService
from codex_web.services.decision_work import DecisionWorkService
from codex_web.services.entitlements import EntitlementService
from codex_web.services.evaluations import EvaluationService
from codex_web.services.extensions import ExtensionService
from codex_web.services.extension_runtime import ExtensionRuntimeRegistry
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.services.local_artifact_content import LocalArtifactContentStore
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.gitlab import install_gitlab_service
from codex_web.services.gitlab_code_host import GitLabCodeHostProvider
from codex_web.services.github_code_host import GitHubCodeHostProvider
from codex_web.services.goals import GoalService
from codex_web.services.metrics import MetricService
from codex_web.services.goal_decomposition_commit import (
    GoalDecompositionCommitService,
)
from codex_web.services.goal_decomposition_generation import (
    GoalDecompositionGenerationService,
)
from codex_web.services.goal_decompositions import GoalDecompositionService
from codex_web.services.input_plugin_definitions import install_input_plugin_definitions
from codex_web.services.model_gateway import ModelGatewayService
from codex_web.services.orchestration_inspector import OrchestrationInspectorService
from codex_web.services.organizational_memory import OrganizationalMemoryService
from codex_web.services.retrieval_embedding import (
    ModelGatewayEmbeddingIdentityValidator,
)
from codex_web.services.projects import ProjectService
from codex_web.services.provider_capacity import (
    ProviderCapacityService,
    install_provider_capacity_event_bridge,
)
from codex_web.services.reference_action_provider import ReferenceActionProvider
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.recovery import LocalBackupDestination, RecoveryService
from codex_web.services.releases import ReleaseService
from codex_web.services.upgrades import UpgradeService
from codex_web.services.identity import IdentityService
from codex_web.services.incidents import IncidentService
from codex_web.services.runtime import RuntimeService
from codex_web.services.scheduler import SchedulerService
from codex_web.services.secrets import SecretBroker
from codex_web.services.security_boundary import SecurityBoundaryService
from codex_web.services.runtime_supervisor import install_runtime_supervisor
from codex_web.services.slack_provider import install_slack_provider_service
from codex_web.services.task_source_action_provider import TaskSourceActionProvider
from codex_web.services.thread_recovery import install_thread_recovery_service
from codex_web.services.thread_execution_settings import install_thread_execution_settings_service
from codex_web.services.thread_bootstrap_bindings import (
    ThreadBootstrapBindingService,
)
from codex_web.services.threads import ThreadService
from codex_web.services.turn_queue_policy import install_turn_queue_policy
from codex_web.services.turn_execution_binding import TurnExecutionBindingService
from codex_web.services.turns import TurnService
from codex_web.services.work_item_state import install_work_item_state_machine
from codex_web.services.work_item_timing import install_work_item_timing_policy
from codex_web.services.work_item_wakeups import install_work_item_wakeup_queue_policy
from codex_web.services.work_item_watchdog_candidates import install_work_item_watchdog_candidate_policy
from codex_web.services.work_item_watchdog_prompts import install_work_item_watchdog_prompt_policy
from codex_web.services.work_item_contracts import install_work_item_contract_service
from codex_web.services.work_items import WorkItemService
from codex_web.services.work_graph import WorkGraphService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.agent_providers import AgentProviderStore
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
from codex_web.storage.approval_requests import ApprovalRequestStore
from codex_web.storage.attention import AttentionStore
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.autonomy_audit import AutonomyAuditStore
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.auxiliary_state import install_auxiliary_state
from codex_web.storage.entitlements import EntitlementStore
from codex_web.storage.evaluations import EvaluationStore
from codex_web.storage.extensions import ExtensionStateStore
from codex_web.storage.crypto_keys import CryptoKeyStore
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.executive_activations import ExecutiveActivationStore
from codex_web.storage.goals import GoalStore
from codex_web.storage.metrics import MetricStore
from codex_web.storage.goal_decompositions import GoalDecompositionStore
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.incidents import IncidentStore
from codex_web.storage.model_gateway import ModelGatewayStore
from codex_web.storage.organizational_memory import OrganizationalMemoryStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.security_events import SecurityEventStore
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.storage.capacity import CapacityStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.decisions import DecisionStore
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.business_context import BusinessContextStore
from codex_web.storage.business_data_sources import BusinessDataSourceStore
from codex_web.storage.business_kpis import BusinessKPIStore
from codex_web.storage.json_files import atomic_write_text, state_file_lock
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.provider_capacity import ProviderCapacityStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.recovery import RecoveryStore
from codex_web.storage.releases import ReleaseStore
from codex_web.storage.upgrades import UpgradeStore
from codex_web.storage.runtime_state import RuntimeStateRepositories
from codex_web.storage.scheduler import SchedulerStore
from codex_web.storage.state_store import build_state_store
from codex_web.storage.work_graph import WorkGraphStore
from codex_web.storage.thread_index import install_thread_index_repository
from codex_web.secret_backends import LocalFileSecretBackend


# Keep one FastAPI application and one runtime lifecycle while domains are
# extracted. The legacy runtime is now a compatibility host for the portions
# that have not moved yet, rather than the place new API behavior is added.
app = core.app

# Shared persistence primitives are owned outside the legacy runtime. Existing
# unextracted state helpers resolve these globals at call time, so they use the
# same atomic implementation without maintaining a second persistence path.
core._state_file_lock = state_file_lock
core._atomic_write_text = atomic_write_text

state_store = build_state_store(
    sqlite_path=STATE_DB_FILE,
    backend=os.environ.get("CODEX_WEB_STATE_BACKEND", "sqlite"),
    postgres_dsn=os.environ.get("CODEX_WEB_POSTGRES_DSN"),
)
project_repository = ProjectRepository(
    PROJECTS_FILE,
    store=state_store,
)
event_transport = build_event_transport(
    os.environ.get("CODEX_WEB_EVENT_TRANSPORT", "in-process"),
    redis_url=os.environ.get("CODEX_WEB_REDIS_URL"),
    redis_stream=os.environ.get("CODEX_WEB_REDIS_STREAM", "codex-web:events"),
    redis_group=os.environ.get("CODEX_WEB_REDIS_GROUP", "codex-web"),
)
instance_id = (
    os.environ.get("CODEX_WEB_INSTANCE_ID")
    or f"control-plane-{os.getpid()}"
)
coordination_backend = StateStoreCoordinationBackend(
    state_store,
    backend_id=f"coordination:{state_store.status().get('backend', 'unknown')}",
)
replicated_ownership_service = ReplicatedOwnershipService(
    coordination_backend,
    instance_id=instance_id,
    lease_seconds=float(
        os.environ.get("CODEX_WEB_COORDINATION_LEASE_SECONDS", "30")
    ),
)
deployment_mode = os.environ.get(
    "CODEX_WEB_DEPLOYMENT_MODE",
    "local",
).strip().casefold()
if deployment_mode not in {"local", "single", "replicated"}:
    raise RuntimeError(
        f"unsupported CODEX_WEB_DEPLOYMENT_MODE: {deployment_mode}"
    )
if deployment_mode == "replicated":
    if not bool(state_store.status().get("shared", False)):
        raise RuntimeError(
            "replicated deployment requires a shared StateStore backend"
        )
    if not coordination_backend.shared:
        raise RuntimeError(
            "replicated deployment requires shared CoordinationBackend"
        )
    if (
        event_transport is None
        or not bool(event_transport.capabilities.durable)
        or not bool(event_transport.capabilities.consumer_groups)
    ):
        raise RuntimeError(
            "replicated deployment requires durable consumer-group EventTransport"
        )

canonical_event_store = CanonicalEventStore(state_store)
canonical_event_bus = CanonicalEventBus(
    canonical_event_store,
    transport=event_transport,
    instance_id=instance_id,
)
canonical_event_ingestion = CanonicalEventIngestionService(canonical_event_bus)
event_transport_runtime = (
    EventTransportRuntime(
        canonical_event_bus,
        consumer_id=instance_id,
    )
    if event_transport is not None
    else None
)
app.state.state_store = state_store
app.state.deployment_mode = deployment_mode
app.state.instance_id = instance_id
app.state.coordination_backend = coordination_backend
app.state.replicated_ownership_service = replicated_ownership_service
app.state.event_transport = event_transport
app.state.event_transport_runtime = event_transport_runtime
app.state.canonical_event_store = canonical_event_store
app.state.canonical_event_bus = canonical_event_bus
app.state.canonical_event_ingestion = canonical_event_ingestion

scheduler_store = SchedulerStore(state_store)
scheduler_service = SchedulerService(scheduler_store, canonical_event_ingestion)
app.state.scheduler_store = scheduler_store
app.state.scheduler_service = scheduler_service
app.include_router(build_scheduler_router(scheduler_service))

configuration_registry_store = ConfigurationRegistryStore(state_store)
configuration_service = ConfigurationService(configuration_registry_store)
codex_worker_configuration_spec = install_codex_worker_configuration(
    configuration_service
)
artifact_content_configuration_spec = install_artifact_content_configuration(
    configuration_service
)
anthropic_worker_configuration_spec = install_anthropic_worker_configuration(
    configuration_service
)
agent_routing_configuration_specs = install_agent_routing_configuration(
    configuration_service
)
app.state.configuration_service = configuration_service
app.state.codex_worker_configuration_spec = codex_worker_configuration_spec
app.state.artifact_content_configuration_spec = artifact_content_configuration_spec
app.state.anthropic_worker_configuration_spec = anthropic_worker_configuration_spec
app.state.agent_routing_configuration_specs = agent_routing_configuration_specs

def _definition_change_notifier(event: dict[str, object]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(core.hub.publish(event))

definition_registry_store = DefinitionRegistryStore(state_store)
definition_registry_service = DefinitionRegistryService(
    definition_registry_store,
    notifier=_definition_change_notifier,
)
execution_role_definition_service = install_execution_role_definitions(
    definition_registry_service
)
executive_role_definition_service = install_executive_role_definitions(
    definition_registry_service
)
agent_routing_definition_service = install_agent_routing_definitions(
    definition_registry_service
)
input_pipeline_definition_service = install_input_plugin_definitions(
    definition_registry_service
)
app.state.definition_registry_service = definition_registry_service
app.state.execution_role_definition_service = execution_role_definition_service
app.state.executive_role_definition_service = executive_role_definition_service
app.state.agent_routing_definition_service = agent_routing_definition_service
app.state.input_pipeline_definition_service = input_pipeline_definition_service
app.include_router(build_input_plugins_router(input_pipeline_definition_service))
core._execution_role_definition_service = execution_role_definition_service

def _work_item_definition_usage(reference):
    items = []
    try:
        states = core._load_work_item_states()
    except Exception:
        return items
    for state in states.values():
        refs = getattr(getattr(state, "execution", None), "definition_refs", []) or []
        if any(item.record_id == reference.record_id for item in refs):
            items.append(
                {
                    "object_type": "work_item",
                    "object_id": state.ref,
                    "project_id": state.project_id,
                    "stage": state.current_stage,
                }
            )
    return items

definition_registry_service.register_usage_provider(_work_item_definition_usage)

identity_state_store = IdentityStateStore(state_store)
identity_service = IdentityService(identity_state_store)
identity_service.bootstrap_local()
install_identity_middleware(app, identity_service)
app.include_router(build_identity_router(identity_service))
app.state.identity_state_store = identity_state_store
app.state.identity_service = identity_service

data_governance_store = DataGovernanceStore(state_store)
data_governance_service = DataGovernanceService(data_governance_store)
app.include_router(build_data_governance_router(data_governance_service))
app.state.data_governance_store = data_governance_store
app.state.data_governance_service = data_governance_service

business_context_store = BusinessContextStore(state_store)
business_context_service = BusinessContextService(
    business_context_store,
    governance=data_governance_service,
)
for business_object_type in (
    "business_entity",
    "external_record_ref",
    "company_fact",
):
    data_governance_service.register_action_handler(
        business_object_type,
        business_context_service.governance_action_handler,
    )
app.include_router(build_business_context_router(business_context_service))
app.state.business_context_store = business_context_store
app.state.business_context_service = business_context_service

entitlement_store = EntitlementStore(state_store)
entitlement_service = EntitlementService(entitlement_store)
app.include_router(build_entitlements_router(entitlement_service))
app.state.entitlement_store = entitlement_store
app.state.entitlement_service = entitlement_service

crypto_key_store = CryptoKeyStore(state_store)
local_key_backend = LocalFileKeyBackend(KEY_MATERIAL_DIR)
crypto_key_service = CryptoKeyService(
    crypto_key_store,
    {"local": local_key_backend},
)
app.include_router(build_crypto_keys_router(crypto_key_service))
app.state.crypto_key_store = crypto_key_store
app.state.crypto_key_service = crypto_key_service
app.state.local_key_backend = local_key_backend

secret_state_store = SecretStateStore(state_store)
local_secret_backend = LocalFileSecretBackend(SECRET_MATERIAL_DIR)
secret_broker = SecretBroker(secret_state_store, {"local": local_secret_backend})
app.include_router(build_secrets_router(secret_broker))
app.state.secret_state_store = secret_state_store
app.state.secret_broker = secret_broker
app.state.local_secret_backend = local_secret_backend

agent_session_store = AgentSessionStore(state_store)
agent_runtime_registry = AgentRuntimeRegistry()
agent_session_service = AgentSessionService(agent_session_store, agent_runtime_registry)
app.state.agent_session_store = agent_session_store
app.state.agent_runtime_registry = agent_runtime_registry
app.state.agent_session_service = agent_session_service

provider_capacity_store = ProviderCapacityStore(state_store)
provider_capacity_service = ProviderCapacityService(
    provider_capacity_store,
    scheduler=scheduler_service,
)
provider_capacity_event_unsubscribe = install_provider_capacity_event_bridge(
    canonical_event_bus,
    provider_capacity_service,
)
app.state.provider_capacity_store = provider_capacity_store
app.state.provider_capacity_service = provider_capacity_service
app.state.provider_capacity_event_unsubscribe = provider_capacity_event_unsubscribe
app.include_router(build_provider_capacity_router(provider_capacity_service))

business_data_source_store = BusinessDataSourceStore(state_store)
business_data_source_registry = BusinessDataSourceRegistry()
business_data_source_service = BusinessDataSourceService(
    business_data_source_store,
    business_data_source_registry,
    business_context_service,
    canonical_event_ingestion,
    scheduler=scheduler_service,
    provider_capacity=provider_capacity_service,
)
business_data_source_event_unsubscribe = canonical_event_bus.subscribe(
    business_data_source_service.handle_canonical_event,
    event_types=(
        CanonicalEventType.BUSINESS_DATA,
        CanonicalEventType.SCHEDULE,
    ),
)
app.include_router(
    build_business_data_sources_router(business_data_source_service)
)
app.state.business_data_source_store = business_data_source_store
app.state.business_data_source_registry = business_data_source_registry
app.state.business_data_source_service = business_data_source_service
app.state.business_data_source_event_unsubscribe = (
    business_data_source_event_unsubscribe
)

model_gateway_store = ModelGatewayStore(state_store)
model_gateway_service = ModelGatewayService(
    model_gateway_store,
    secret_broker=secret_broker,
    entitlements=entitlement_service,
    input_pipeline_resolver=input_pipeline_definition_service.pipeline_for,
    provider_capacity=provider_capacity_service,
)
model_gateway_service.register_adapter(OpenAIModelProviderAdapter())
model_gateway_service.register_adapter(AnthropicModelProviderAdapter())
app.include_router(build_model_gateway_router(model_gateway_service))
app.state.model_gateway_store = model_gateway_store
app.state.model_gateway_service = model_gateway_service

security_event_store = SecurityEventStore(state_store)
security_boundary_service = SecurityBoundaryService(security_event_store)
input_pipeline_definition_service.set_audit_sink(
    security_boundary_service.record_input_plugin_event
)
app.include_router(build_security_router(security_boundary_service))
app.state.security_event_store = security_event_store
app.state.security_boundary_service = security_boundary_service

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
resource_catalog_store = ResourceCatalogStore(state_store)
resource_catalog_service = ResourceCatalogService(resource_catalog_store)
app.include_router(build_resources_router(resource_catalog_service, project_service))
app.state.resource_catalog_store = resource_catalog_store
app.state.resource_catalog_service = resource_catalog_service

code_host_registry = CodeHostRegistry()
code_host_registry.register_provider(GitLabCodeHostProvider())
code_host_registry.register_provider(GitHubCodeHostProvider())
code_host_service = CodeHostService(
    code_host_registry,
    resource_catalog_service,
    secrets=secret_broker,
    canonical_events=canonical_event_ingestion,
)
app.state.code_host_registry = code_host_registry
app.state.code_host_service = code_host_service

authority_role_service = install_authority_roles(
    definition_registry_service,
    resource_catalog_service,
)
app.state.authority_role_service = authority_role_service

organizational_memory_store = OrganizationalMemoryStore(state_store)
organizational_memory_service = OrganizationalMemoryService(
    organizational_memory_store,
    data_governance_service,
    authority_role_service,
    embedding_identity_validator=ModelGatewayEmbeddingIdentityValidator(
        model_gateway_service
    ),
)
app.state.organizational_memory_store = organizational_memory_store
app.state.organizational_memory_service = organizational_memory_service
app.include_router(
    build_organizational_memory_router(organizational_memory_service)
)

approval_request_store = ApprovalRequestStore(state_store)
approval_request_service = ApprovalRequestService(
    approval_request_store,
    identity_service,
    canonical_event_ingestion,
    scheduler=scheduler_service,
    authority_roles=authority_role_service,
)
app.state.approval_request_store = approval_request_store
app.state.approval_request_service = approval_request_service
app.include_router(build_approval_requests_router(approval_request_service))

attention_store = AttentionStore(state_store)
attention_service = AttentionService(
    attention_store,
    canonical_event_ingestion,
    scheduler=scheduler_service,
)
app.state.attention_store = attention_store
app.state.attention_service = attention_service
app.state.attention_event_unsubscribe = install_attention_event_bridges(
    canonical_event_bus,
    attention_service,
)
app.include_router(build_attention_router(attention_service))

action_provider_state_store = ActionProviderStateStore(state_store)
action_provider_registry = ActionProviderRegistry(action_provider_state_store)
reference_action_provider = ReferenceActionProvider()
action_provider_registry.register(reference_action_provider)
action_execution_service = ActionExecutionService(
    action_provider_registry,
    resource_catalog_service,
    secret_broker=secret_broker,
)
app.include_router(
    build_action_providers_router(
        action_provider_registry,
        action_execution_service,
    )
)
app.state.action_provider_state_store = action_provider_state_store
app.state.action_provider_registry = action_provider_registry
app.state.action_execution_service = action_execution_service

execution_workspace_state_store = ExecutionWorkspaceStateStore(state_store)
execution_workspace_backend = LocalGitWorkspaceBackend(EXECUTION_WORKSPACE_DIR)
execution_workspace_service = ExecutionWorkspaceService(
    execution_workspace_state_store,
    execution_workspace_backend,
    resource_catalog_service,
    project_service.get,
    work_item_host=core,
)
app.include_router(build_execution_workspaces_router(execution_workspace_service))
app.state.execution_workspace_state_store = execution_workspace_state_store
app.state.execution_workspace_backend = execution_workspace_backend
app.state.execution_workspace_service = execution_workspace_service

execution_worker_store = ExecutionWorkerStore(state_store)
execution_worker_service = ExecutionWorkerService(
    execution_worker_store,
    identity=identity_service,
    workspaces=execution_workspace_service,
)
local_execution_backend = BubblewrapExecutionBackend()
local_execution_backend_status = local_execution_backend.probe()
local_worker_actor = identity_service.bootstrap_service_actor(
    identity_id="execution-worker-local",
    name="Local Execution Worker",
    scope=identity_service.local_trusted_actor().tenant,
    service_scopes=("execution-worker:run", "secret:use"),
)
local_execution_worker = execution_worker_service.ensure_local_worker(
    service_identity_id=local_worker_actor.identity_id,
    version="local-v2",
    capabilities=local_execution_backend_status.capabilities,
    actor=identity_service.local_trusted_actor(),
)
app.include_router(build_execution_workers_router(execution_worker_service))
app.state.execution_worker_store = execution_worker_store
app.state.execution_worker_service = execution_worker_service
app.state.local_execution_worker = local_execution_worker
app.state.local_execution_backend = local_execution_backend
app.state.local_execution_backend_status = local_execution_backend_status

codex_execution_runtime_binding = ExecutionRuntimeBinding(
    provider_id="openai",
    runtime_id="codex",
    capability_revision=1,
)
claude_execution_runtime_binding = ExecutionRuntimeBinding(
    provider_id="anthropic",
    runtime_id="claude-code",
    capability_revision=1,
)

turn_execution_binding_service = TurnExecutionBindingService(
    configuration_service,
    project_service,
    resource_catalog_service,
    execution_workspace_service,
    execution_worker_service,
    control_actor=identity_service.local_trusted_actor(),
    runtime_binding=codex_execution_runtime_binding,
    runtime_credential_configs={
        ("openai", "codex"): CODEX_WORKER_ACCESS_TOKEN_CONFIG,
        ("anthropic", "claude-code"): ANTHROPIC_WORKER_API_KEY_CONFIG,
    },
)
app.state.turn_execution_binding_service = turn_execution_binding_service

thread_bootstrap_binding_store = ThreadBootstrapBindingStore(state_store)
thread_bootstrap_binding_service = ThreadBootstrapBindingService(
    thread_bootstrap_binding_store
)
app.state.thread_bootstrap_binding_store = thread_bootstrap_binding_store
app.state.thread_bootstrap_binding_service = thread_bootstrap_binding_service

artifact_content_registry = ArtifactContentRegistry()
local_artifact_content_store = LocalArtifactContentStore(ARTIFACT_CONTENT_DIR)
artifact_content_registry.register(local_artifact_content_store)
artifact_content_service = ArtifactContentService(
    artifact_content_registry,
    default_backend_id="local",
)

def _artifact_content_backend(actor, project_id):
    effective = configuration_service.resolve(
        ARTIFACT_CONTENT_BACKEND_CONFIG,
        ConfigurationContext(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=project_id,
        ),
    )
    return str(effective.value)

artifact_evidence_store = ArtifactEvidenceStore(state_store)
artifact_evidence_service = ArtifactEvidenceService(
    artifact_evidence_store,
    resources=resource_catalog_service,
    work_item_host=core,
    governance=data_governance_service,
    content=artifact_content_service,
    content_backend_resolver=_artifact_content_backend,
)
data_governance_service.register_action_handler(
    "artifact",
    artifact_evidence_service.apply_governance_action,
)
data_governance_service.register_action_handler(
    "evidence",
    artifact_evidence_service.apply_governance_action,
)
app.include_router(build_artifact_evidence_router(artifact_evidence_service))
app.state.artifact_content_registry = artifact_content_registry
app.state.local_artifact_content_store = local_artifact_content_store
app.state.artifact_content_service = artifact_content_service
app.state.artifact_evidence_store = artifact_evidence_store
app.state.artifact_evidence_service = artifact_evidence_service

evaluation_store = EvaluationStore(state_store)
evaluation_service = EvaluationService(
    evaluation_store,
    definition_registry_service,
    artifact_evidence=artifact_evidence_service,
)
app.state.evaluation_store = evaluation_store
app.state.evaluation_service = evaluation_service
app.include_router(build_evaluations_router(evaluation_service))

agent_runtime_usage_store = AgentRuntimeUsageStore(state_store)

def _runtime_usage_attribution(session):
    assignment = next(
        (
            item
            for item in execution_worker_store.load().assignments
            if session.assignment_id
            and item.id == session.assignment_id
            and item.organization_id == session.organization_id
            and item.workspace_id == session.workspace_id
        ),
        None,
    )
    work_item_ref = assignment.work_item_ref if assignment is not None else None
    work_state = None
    if work_item_ref:
        try:
            work_state = core._load_work_item_states().get(work_item_ref)
        except Exception:
            work_state = None
    return {
        "work_item_ref": work_item_ref,
        "goal_id": getattr(work_state, "goal_id", None) if work_state is not None else None,
        "decision_id": (
            getattr(work_state, "decision_id", None)
            if work_state is not None
            else None
        ),
    }


agent_runtime_telemetry_service = AgentRuntimeTelemetryService(
    agent_runtime_usage_store,
    agent_session_store,
    agent_runtime_registry,
    artifact_evidence=artifact_evidence_service,
    attribution_resolver=_runtime_usage_attribution,
)
app.state.agent_runtime_usage_store = agent_runtime_usage_store
app.state.agent_runtime_telemetry_service = agent_runtime_telemetry_service
app.include_router(build_agent_runtime_usage_router(agent_runtime_telemetry_service))

extension_state_store = ExtensionStateStore(state_store)
extension_package_catalog = LocalExtensionPackageCatalog(EXTENSION_PACKAGE_DIR)
extension_service = ExtensionService(
    extension_state_store,
    secrets=secret_broker,
    configuration=configuration_service,
    resources=resource_catalog_service,
    artifact_evidence=artifact_evidence_service,
)
app.include_router(
    build_extensions_router(
        extension_service,
        extension_package_catalog,
    )
)
app.state.extension_state_store = extension_state_store
app.state.extension_package_catalog = extension_package_catalog
app.state.extension_service = extension_service

agent_provider_store = AgentProviderStore(state_store)
agent_provider_service = AgentProviderService(
    agent_provider_store,
    model_gateway=model_gateway_store,
    extensions=extension_state_store,
)
app.include_router(build_agent_providers_router(agent_provider_service))
app.state.agent_provider_store = agent_provider_store
app.state.agent_provider_service = agent_provider_service

agent_routing_service = AgentRoutingService(
    agent_provider_service,
    agent_runtime_registry,
    model_gateway=model_gateway_service,
    configuration=configuration_service,
    role_defaults=agent_routing_definition_service,
    provider_capacity=provider_capacity_service,
)
app.include_router(build_agent_routing_router(agent_routing_service))
app.state.agent_routing_service = agent_routing_service

codex_auth_delegation_service = CodexAuthDelegationService(secret_broker)
anthropic_auth_delegation_service = AnthropicAuthDelegationService(secret_broker)
app.state.codex_auth_delegation_service = codex_auth_delegation_service
app.state.anthropic_auth_delegation_service = anthropic_auth_delegation_service

local_execution_worker_runtime = LocalExecutionWorkerRuntime(
    execution_worker_service,
    execution_workspace_service,
    local_execution_backend,
    worker=local_execution_worker,
    worker_actor=local_worker_actor,
    control_actor=identity_service.local_trusted_actor(),
    artifact_evidence=artifact_evidence_service,
    codex_auth_delegation=codex_auth_delegation_service,
)
app.state.local_execution_worker_runtime = local_execution_worker_runtime

def _codex_model_egress_endpoints():
    providers = model_gateway_service.list_providers(
        identity_service.local_trusted_actor()
    )
    active = [
        provider
        for provider in providers
        if getattr(provider.status, "value", provider.status) == "active"
    ]
    include_first_party = (
        not active
        or any(
            provider.adapter_type == "openai" and not provider.base_url
            for provider in active
        )
    )
    default_endpoints = (
        (
            AgentRuntimeModelEgressEndpoint("api.openai.com", 443),
            AgentRuntimeModelEgressEndpoint("chatgpt.com", 443),
        )
        if include_first_party
        else ()
    )
    return model_egress_endpoints_from_base_urls(
        [provider.base_url for provider in active],
        default_endpoints=default_endpoints,
    )


assignment_bound_codex_session_manager = AssignmentBoundCodexSessionManager(
    local_execution_worker_runtime,
    core,
    egress_endpoints_resolver=_codex_model_egress_endpoints,
    runtime_binding=codex_execution_runtime_binding,
)
app.state.assignment_bound_codex_session_manager = assignment_bound_codex_session_manager

def _claude_model_egress_endpoints():
    providers = model_gateway_service.list_providers(
        identity_service.local_trusted_actor()
    )
    active = [
        provider
        for provider in providers
        if getattr(provider.status, "value", provider.status) == "active"
        and provider.adapter_type == "anthropic"
    ]
    return model_egress_endpoints_from_base_urls(
        [provider.base_url for provider in active],
        default_endpoints=(
            AgentRuntimeModelEgressEndpoint("api.anthropic.com", 443),
        ),
    )


assignment_bound_claude_session_manager = AssignmentBoundClaudeSessionManager(
    local_execution_worker_runtime,
    core,
    egress_endpoints_resolver=_claude_model_egress_endpoints,
    credential_provider=anthropic_auth_delegation_service,
    runtime_binding=claude_execution_runtime_binding,
)
app.state.assignment_bound_claude_session_manager = assignment_bound_claude_session_manager

capacity_store = CapacityStore(state_store)
capacity_service = CapacityService(
    capacity_store,
    evidence=artifact_evidence_service,
)
app.state.capacity_store = capacity_store
app.state.capacity_service = capacity_service
app.include_router(build_capacity_router(capacity_service))

action_intent_store = ActionIntentStore(state_store)
action_intent_service = ActionIntentService(
    action_intent_store,
    action_execution_service,
    artifact_evidence=artifact_evidence_service,
    work_item_host=core,
    security_boundary=security_boundary_service,
    entitlements=entitlement_service,
    authority=authority_role_service,
    identity=identity_service,
    capacity=capacity_service,
)
app.include_router(build_action_intents_router(action_intent_service))
app.state.action_intent_store = action_intent_store
app.state.action_intent_service = action_intent_service

release_store = ReleaseStore(state_store)
release_service = ReleaseService(
    release_store,
    artifacts=artifact_evidence_service,
    resources=resource_catalog_service,
    approvals=approval_request_service,
    action_intents=action_intent_service,
)
app.state.release_store = release_store
app.state.release_service = release_service
app.include_router(build_releases_router(release_service))

incident_store = IncidentStore(state_store)
incident_service = IncidentService(
    incident_store,
    attention=attention_service,
    artifacts=artifact_evidence_service,
    action_intents=action_intent_service,
    resources=resource_catalog_service,
    memory=organizational_memory_service,
    canonical_events=canonical_event_ingestion,
)
app.state.incident_store = incident_store
app.state.incident_service = incident_service
app.include_router(build_incidents_router(incident_service))

agent_session_trace_service = AgentSessionTraceService(
    agent_session_service,
    agent_runtime_telemetry_service,
    execution_worker_store,
    execution_workspace_state_store,
    action_intent_store,
    artifact_evidence_store,
)
app.state.agent_session_trace_service = agent_session_trace_service
app.include_router(
    build_agent_sessions_router(
        agent_session_service,
        agent_runtime_registry,
        agent_runtime_telemetry_service,
        agent_session_trace_service,
    )
)

autonomy_state_store = AutonomyStateStore(state_store)
autonomy_audit_store = AutonomyAuditStore(state_store)
autonomy_audit_service = AutonomyAuditService(
    autonomy_audit_store,
    autonomy_store=autonomy_state_store,
    action_intents=action_intent_service,
    evidence=artifact_evidence_service,
    canonical_events=canonical_event_ingestion,
    scheduler=scheduler_service,
)
autonomy_policy_service = AutonomyPolicyService(
    autonomy_state_store,
    authority=authority_role_service,
    resources=resource_catalog_service,
    evidence=artifact_evidence_service,
    approvals=approval_request_service,
)
autonomy_controller = AutonomyController(
    autonomy_state_store,
    action_intents=action_intent_service,
    policy=autonomy_policy_service,
    audit=autonomy_audit_service,
)
app.state.autonomy_state_store = autonomy_state_store
app.state.autonomy_audit_store = autonomy_audit_store
app.state.autonomy_audit_service = autonomy_audit_service
app.state.autonomy_policy_service = autonomy_policy_service
app.state.autonomy_controller = autonomy_controller
app.include_router(
    build_autonomy_router(
        autonomy_controller,
        autonomy_policy_service,
    )
)
app.include_router(build_autonomy_audit_router(autonomy_audit_service))

recovery_store = RecoveryStore(state_store)
recovery_service_actor = identity_service.bootstrap_service_actor(
    identity_id="recovery-service",
    name="Recovery Service",
    scope=identity_service.local_trusted_actor().tenant,
    service_scopes=(
        "crypto:admin",
        "recovery:admin",
        "artifact-evidence:admin",
    ),
)
local_backup_destination = LocalBackupDestination(
    STATE_DB_FILE.parent / "backups"
)
recovery_service = RecoveryService(
    recovery_store,
    state_store=state_store,
    crypto=crypto_key_service,
    evidence=artifact_evidence_service,
    audit=autonomy_audit_service,
    scheduler=scheduler_service,
    canonical_events=canonical_event_ingestion,
    service_actor=recovery_service_actor,
    destinations=(local_backup_destination,),
)
app.state.recovery_store = recovery_store
app.state.recovery_service = recovery_service
app.state.local_backup_destination = local_backup_destination
app.include_router(build_recovery_router(recovery_service))

upgrade_store = UpgradeStore(state_store)
upgrade_service = UpgradeService(
    upgrade_store,
    state_store=state_store,
    definitions=definition_registry_service,
    workers=execution_worker_service,
    extensions=extension_service,
    recovery=recovery_service,
    releases=release_service,
    action_intents=action_intent_service,
    approvals=approval_request_service,
    evidence=artifact_evidence_service,
)
action_intent_service.maintenance_guard = (
    upgrade_service.action_execution_allowed
)
execution_worker_service.maintenance_guard = (
    upgrade_service.worker_assignment_allowed
)
app.state.upgrade_store = upgrade_store
app.state.upgrade_service = upgrade_service
app.include_router(build_upgrades_router(upgrade_service))

orchestration_inspector_service = OrchestrationInspectorService(
    canonical_event_store,
    autonomy_controller,
    scheduler=scheduler_store,
    evaluations=evaluation_store,
    attention=attention_store,
    approvals=approval_request_store,
    action_intents=action_intent_store,
    agent_sessions=agent_session_store,
    runtime_usage=agent_runtime_usage_store,
    model_gateway=model_gateway_store,
    artifact_evidence=artifact_evidence_store,
)
app.state.orchestration_inspector_service = orchestration_inspector_service
app.include_router(build_orchestration_router(orchestration_inspector_service))

autonomy_control_center_service = AutonomyControlCenterService(
    autonomy=autonomy_controller,
    policy=autonomy_policy_service,
    approvals=approval_request_service,
    attention=attention_service,
    incidents=incident_service,
    releases=release_service,
    recovery=recovery_service,
    capacity=capacity_service,
    provider_capacity=provider_capacity_service,
    upgrades=upgrade_service,
    workers=execution_worker_service,
    audit=autonomy_audit_service,
    orchestration=orchestration_inspector_service,
    coordination=coordination_backend,
    ownership=replicated_ownership_service,
)
app.state.autonomy_control_center_service = autonomy_control_center_service
app.include_router(
    build_autonomy_control_center_router(
        autonomy_control_center_service
    )
)

def _resource_ids_for_project(project_id: str) -> list[str]:
    project = project_service.get(project_id)
    return resource_catalog_service.resource_ids_for_project(project)

core._resource_ids_for_project = _resource_ids_for_project
runtime_service = RuntimeService(core)
approval_compatibility_actor = identity_service.local_trusted_actor()
codex_approval_requester = identity_service.bootstrap_service_actor(
    identity_id="service-codex-approval-requester",
    name="Codex approval requester",
    scope=approval_compatibility_actor.tenant,
    service_scopes=("approvals:request",),
)
approval_service = ApprovalService(
    core,
    assignment_sessions=(assignment_bound_codex_session_manager, assignment_bound_claude_session_manager),
    canonical=approval_request_service,
    canonical_requester=codex_approval_requester,
    compatibility_actor=approval_compatibility_actor,
)
def _assignment_runtime_adapter(binding, session):
    key = (binding.provider_id, binding.runtime_id)
    if key == ("openai", "codex"):
        return CodexAgentRuntimeAdapter(session)
    if key == ("anthropic", "claude-code"):
        return ClaudeAgentRuntimeAdapter(session)
    raise RuntimeError(
        f"unsupported assignment-bound agent runtime: {binding.provider_id}/{binding.runtime_id}"
    )


assignment_session_managers = {
    ("openai", "codex"): assignment_bound_codex_session_manager,
    ("anthropic", "claude-code"): assignment_bound_claude_session_manager,
}

thread_service = ThreadService(
    core,
    binding_service=turn_execution_binding_service,
    session_manager=assignment_bound_codex_session_manager,
    bootstrap_bindings=thread_bootstrap_binding_service,
    control_actor=identity_service.local_trusted_actor(),
    agent_sessions=agent_session_service,
    routing_service=agent_routing_service,
    session_managers=assignment_session_managers,
    runtime_adapter_factory=_assignment_runtime_adapter,
)
context_service = ContextCompactionService(core)
gitlab_client = GitLabClient()
work_item_state_machine = install_work_item_state_machine(
    app,
    core,
    gitlab_client,
    store=state_store,
)
work_item_contract_service = install_work_item_contract_service(
    app,
    core,
    execution_role_definition_service,
)
work_item_service = WorkItemService(core, gitlab_client, work_item_state_machine)
authority_policy_explorer_service = AuthorityPolicyExplorerService(
    authority_role_service,
    definition_registry_service,
)
app.include_router(
    build_authority_router(
        authority_policy_explorer_service,
        authority_role_service,
        identity_service,
        work_item_service,
        project_service,
    )
)
app.state.authority_policy_explorer_service = authority_policy_explorer_service
task_source_action_provider = TaskSourceActionProvider(work_item_service)
action_provider_registry.register(task_source_action_provider)
app.state.task_source_action_provider = task_source_action_provider
work_graph_store = WorkGraphStore(state_store)
work_graph_service = WorkGraphService(
    work_graph_store,
    runtime_state.work_item_states.load,
)
app.state.work_graph_store = work_graph_store
app.state.work_graph_service = work_graph_service
app.include_router(build_work_graph_router(work_graph_service, project_service))
metric_store = MetricStore(state_store)
metric_service = MetricService(metric_store)
app.state.metric_store = metric_store
app.state.metric_service = metric_service
app.include_router(build_metrics_router(metric_service))

decision_store = DecisionStore(state_store)
decision_service = DecisionService(
    decision_store,
    approval_request_service,
    metric_service,
    artifact_evidence_service,
    canonical_event_ingestion,
)
decision_deliberation_service = DecisionDeliberationService(
    decision_service,
    model_gateway_service,
)
app.state.decision_store = decision_store
app.state.decision_service = decision_service
app.state.decision_deliberation_service = decision_deliberation_service
goal_store = GoalStore(state_store)
goal_service = GoalService(goal_store, project_service, work_graph_service)
decision_service.goals = goal_service

business_kpi_store = BusinessKPIStore(state_store)
business_kpi_service = BusinessKPIService(
    business_kpi_store,
    metric_service,
    business_context_service,
    goal_service,
    decision_service,
)
app.state.business_kpi_store = business_kpi_store
app.state.business_kpi_service = business_kpi_service
app.include_router(build_business_kpis_router(business_kpi_service))

decision_work_service = DecisionWorkService(
    decision_service,
    goal_service,
    work_item_service,
    work_graph_service,
    action_intent_service,
    action_execution_service,
    action_provider_registry,
)
app.state.decision_work_service = decision_work_service
app.include_router(
    build_decisions_router(
        decision_service,
        decision_deliberation_service,
        decision_work_service,
    )
)
executive_activation_store = ExecutiveActivationStore(state_store)
executive_management_service = ExecutiveManagementService(
    executive_activation_store,
    executive_role_definition_service,
    model_gateway_service,
    authority_role_service,
    goal_service,
    decision_service,
    decision_work_service,
    work_item_service,
    work_graph_service,
    artifact_evidence_service,
    organizational_memory_service,
    business_context_service,
    business_kpi_service,
    data_governance_service,
)
app.state.executive_activation_store = executive_activation_store
app.state.executive_management_service = executive_management_service
app.include_router(build_executive_management_router(executive_management_service))

company_operations_service = CompanyOperationsService(
    business_context_service,
    business_data_source_service,
    business_kpi_service,
    goal_service,
    decision_service,
    executive_management_service,
    attention_service,
    provider_capacity_service,
    approval_request_service,
    action_intent_service,
    artifact_evidence_service,
    extension_service,
)
app.state.company_operations_service = company_operations_service
app.include_router(build_company_operations_router(company_operations_service))

def _executive_role_definition_usage(reference):
    if reference.kind != "executive-role-catalog":
        return []
    return [
        {
            "object_type": "executive_activation",
            "object_id": item.id,
            "project_id": item.project_id,
            "status": item.status.value,
        }
        for item in executive_activation_store.load().activations
        if item.role_catalog.record_id == reference.record_id
    ]

definition_registry_service.register_usage_provider(
    _executive_role_definition_usage
)
goal_decomposition_store = GoalDecompositionStore(state_store)
goal_decomposition_service = GoalDecompositionService(
    goal_decomposition_store,
    goal_service,
    project_service,
)
goal_decomposition_generation_service = GoalDecompositionGenerationService(
    goal_decomposition_service,
    goal_service,
    project_service,
    work_graph_service,
    model_gateway_service,
)
goal_decomposition_commit_service = GoalDecompositionCommitService(
    goal_decomposition_service,
    goal_service,
    work_graph_service,
    action_intent_service,
    action_execution_service,
    action_provider_registry,
)
app.state.goal_store = goal_store
app.state.goal_service = goal_service
app.state.goal_decomposition_store = goal_decomposition_store
app.state.goal_decomposition_service = goal_decomposition_service
app.state.goal_decomposition_generation_service = (
    goal_decomposition_generation_service
)
app.state.goal_decomposition_commit_service = (
    goal_decomposition_commit_service
)
app.include_router(build_goals_router(goal_service))
app.include_router(
    build_goal_decompositions_router(
        goal_decomposition_service,
        goal_decomposition_generation_service,
        goal_decomposition_commit_service,
    )
)
extension_runtime_registry = ExtensionRuntimeRegistry(
    extension_service,
    work_item_service.task_source_registry,
    action_provider_registry,
)
app.state.extension_runtime_registry = extension_runtime_registry
gitlab_service = install_gitlab_service(
    app,
    core,
    gitlab_client,
    canonical_events=canonical_event_ingestion,
    autonomy_controller=autonomy_controller,
)

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
# Release expired resource locks and clean abandoned worktrees on startup.
execution_workspace_service.recover_expired()
execution_worker_service.mark_stale_workers_offline(
    actor=identity_service.local_trusted_actor(),
)
execution_worker_service.recover_expired(
    actor=identity_service.local_trusted_actor(),
)
artifact_evidence_service.expire_retention()
action_intent_service.recover_stale_claims()

# Compose extracted runtime ownership here rather than in server.py so direct
# application imports and tests observe the same implementation as the CLI
# entrypoint. The installers are idempotent and preserve the compatibility
# attributes expected by services that have not moved out of core.py yet.
codex_runtime = install_codex_runtime(app, core)
codex_agent_adapter = CodexAgentRuntimeAdapter(codex_runtime)
agent_runtime_registry.register(
    codex_agent_adapter,
    capability_revision=1,
    sandbox_profiles=("read-only", "workspace-write"),
    network_profiles=("brokered-model-egress",),
)
app.state.codex_agent_runtime_adapter = agent_runtime_registry.get("openai", "codex")
provider_capacity_service.register_probe(
    "openai",
    "codex",
    app.state.codex_agent_runtime_adapter.capacity_snapshot,
)
agent_runtime_telemetry_service.subscribe(app.state.codex_agent_runtime_adapter)
if not any(
    provider.id == "openai"
    and provider.organization_id == identity_service.local_trusted_actor().organization_id
    and provider.workspace_id == identity_service.local_trusted_actor().workspace_id
    for provider in agent_provider_store.list()
):
    agent_provider_service.upsert(
        AgentProviderUpsert(
            id="openai",
            display_name="OpenAI Codex",
            declared_capabilities=codex_agent_adapter.capabilities,
            granted_capabilities=codex_agent_adapter.capabilities,
            health=AgentProviderHealth.HEALTHY,
        ),
        actor=identity_service.local_trusted_actor(),
    )

class _ClaudeRegistryTransport:
    """Resolve the assignment-bound Claude session named in canonical requests."""

    def __init__(self, manager):
        self.manager = manager
        self.host = core

    async def _session(self, params):
        assignment_id = str((params or {}).get("assignment_id") or "").strip()
        if not assignment_id:
            raise RuntimeError("Claude runtime requires canonical assignment_id")
        return await self.manager.start(assignment_id)

    async def request(self, method, params=None):
        values = params if isinstance(params, dict) else {}
        if method == "session/create":
            session = await self._session(values)
            return await session.request(method, values)
        assignment_id = str(values.get("assignment_id") or "").strip()
        if assignment_id:
            session = await self.manager.start(assignment_id)
            return await session.request(method, values)
        native_session_id = values.get("session_id")
        if native_session_id:
            for session in self.manager.sessions.values():
                runtime = session.runtime
                if runtime is not None and runtime.native_session_id == str(native_session_id):
                    return await session.request(method, values)
        raise RuntimeError("Claude runtime session is not active")

    async def respond_to_server_request(self, request_id, result):
        for session in self.manager.sessions.values():
            runtime = session.runtime
            if runtime is not None and request_id in runtime.pending_approvals:
                await runtime.respond_to_server_request(request_id, result)
                return
        raise RuntimeError("Claude approval request is not active")

    async def stop(self):
        await self.manager.stop_all()


claude_registry_transport = _ClaudeRegistryTransport(
    assignment_bound_claude_session_manager
)
agent_runtime_registry.register(
    ClaudeAgentRuntimeAdapter(claude_registry_transport),
    capability_revision=1,
    sandbox_profiles=("read-only", "workspace-write"),
    network_profiles=("brokered-model-egress",),
)
app.state.claude_agent_runtime_adapter = agent_runtime_registry.get(
    "anthropic",
    "claude-code",
)
agent_runtime_telemetry_service.subscribe(app.state.claude_agent_runtime_adapter)
thread_execution_settings_service = install_thread_execution_settings_service(app, core)
turn_execution_service = install_turn_execution_service(
    app,
    core,
    binding_service=turn_execution_binding_service,
    session_manager=assignment_bound_codex_session_manager,
    bootstrap_bindings=thread_bootstrap_binding_service,
    control_actor=identity_service.local_trusted_actor(),
    routing_service=agent_routing_service,
    session_managers=assignment_session_managers,
    runtime_adapter_factory=_assignment_runtime_adapter,
    provider_capacity=provider_capacity_service,
    ownership=replicated_ownership_service,
)
async def _resume_provider_capacity_wait(wait):
    if wait.thread_id:
        turn_execution_service.schedule_queue_drain(wait.thread_id)
        return

    if wait.work_item_ref:
        state = core._load_work_item_states().get(wait.work_item_ref)
        if state is not None and state.project_id:
            owner = core._coerce_owner(state.current_owner or state.next_owner)
            binding = (
                core._binding_for_agent(
                    owner,
                    state.project_id,
                    preferred_conversation_id=core.HANDOFF_COORDINATION_CHANNEL,
                )
                if owner
                else None
            )
            if binding is not None:
                turn_execution_service.enqueue_turn(
                    thread_id=binding.thread_id,
                    project_id=state.project_id,
                    message=core._work_item_dispatch_text(state),
                    source=f"provider-capacity-resume:{wait.id}",
                    execution_id=wait.execution_id,
                )
                turn_execution_service.schedule_queue_drain(binding.thread_id)
                return

    if core._autonomy_enabled():
        core._schedule_native_recovery_cycles(
            reason="provider-capacity-resumed"
        )


provider_capacity_service.register_resume_handler(
    _resume_provider_capacity_wait
)
work_item_timing_policy = install_work_item_timing_policy(app, core)
work_item_watchdog_candidate_policy = install_work_item_watchdog_candidate_policy(app, core)
work_item_watchdog_prompt_policy = install_work_item_watchdog_prompt_policy(app, core)
watchdog_dispatch_policy = install_watchdog_dispatch_policy(app, core)
autonomy_service = install_autonomy_service(
    app,
    core,
    action_execution_service,
    action_intent_service,
    controller=autonomy_controller,
    canonical_events=canonical_event_ingestion,
)
turn_queue_policy = install_turn_queue_policy(app, core)
work_item_wakeup_queue_policy = install_work_item_wakeup_queue_policy(app, core)
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
bot_connection_service = install_bot_connection_service(
    app,
    core,
    secret_broker=secret_broker,
    identity_service=identity_service,
)
bot_binding_selection_service = install_bot_binding_selection_service(app, core)
agent_channel_preference_service = install_agent_channel_preference_service(app, core)
bot_runtime = install_bot_runtime(
    app,
    core,
    slack_client=slack_client,
    telegram_client=telegram_client,
    ownership=replicated_ownership_service,
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
    secret_broker=secret_broker,
)
app.state.slack_client = slack_client
app.state.telegram_client = telegram_client

# Replace the legacy core startup/shutdown callbacks after all runtime and
# provider services have been composed. The supervisor keeps the historical
# task globals populated for diagnostics while owning cancellation and shutdown.
runtime_supervisor = install_runtime_supervisor(app, core)

install_webhook_security(core, secret_broker)
previous_context_service = getattr(app.state, "context_compaction_service", None)
if previous_context_service is not None:
    core.hub.unsubscribe(previous_context_service.observe)
core.hub.subscribe(context_service.observe)
app.state.context_compaction_service = context_service

EXTRACTED_ROUTE_COUNTS = {
    "definitions": replace_routes(
        app,
        build_definitions_router(definition_registry_service, project_service),
        paths=set(),
        key="definitions",
    ),
    "configuration": replace_routes(
        app,
        build_configuration_router(configuration_service, project_service, resource_catalog_service),
        paths=set(),
        key="configuration",
    ),
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

core.executive_service = install_executive_integrated(
    app,
    core,
    model_gateway=model_gateway_service,
    executive_roles=executive_role_definition_service,
    organizational_memory=organizational_memory_service,
)

api_authorization_service = install_api_authorization(
    app,
    authority_role_service,
)
app.state.api_authorization_service = api_authorization_service


def main() -> None:
    core.main()
