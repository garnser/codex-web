from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from codex_web.api.action_intents import build_action_intents_router
from codex_web.api.agent_profiles import build_agent_profiles_router
from codex_web.api.agent_teams import build_agent_teams_router
from codex_web.api.agent_team_execution import build_agent_team_execution_router
from codex_web.api.skills import build_skills_router
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
from codex_web.api.automations import build_automations_router
from codex_web.api.automation_execution import build_automation_execution_router
from codex_web.api.automation_outcomes import build_automation_outcomes_router
from codex_web.api.bots import build_bots_router
from codex_web.api.configuration import build_configuration_router
from codex_web.api.conversation_channels import build_conversation_channels_router
from codex_web.api.capacity import build_capacity_router
from codex_web.api.canonical_materialization import (
    build_canonical_materialization_router,
)
from codex_web.api.crypto_keys import build_crypto_keys_router
from codex_web.api.context import build_context_router
from codex_web.api.control_plane_broker import build_control_plane_broker_router
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
from codex_web.api.execution_profiles import build_execution_profiles_router
from codex_web.api.execution_workspaces import build_execution_workspaces_router
from codex_web.api.execution_workers import build_execution_workers_router
from codex_web.api.integrations import build_integrations_router
from codex_web.api.incidents import build_incidents_router
from codex_web.api.legacy_project_migration import build_legacy_project_migration_router
from codex_web.api.goals import build_goals_router
from codex_web.api.home import build_home_router
from codex_web.api.metrics import build_metrics_router
from codex_web.api.goal_decompositions import build_goal_decompositions_router
from codex_web.api.input_plugins import build_input_plugins_router
from codex_web.api.model_gateway import build_model_gateway_router
from codex_web.api.orchestration import build_orchestration_router
from codex_web.api.organizational_memory import build_organizational_memory_router
from codex_web.api.identity import build_identity_router, install_identity_middleware
from codex_web.api.projects import build_projects_router
from codex_web.api.project_ui_state import build_project_ui_state_router
from codex_web.api.project_bootstrap import build_project_bootstrap_router
from codex_web.api.operational_compaction import (
    build_operational_compaction_router,
)
from codex_web.api.project_readiness import build_project_readiness_router
from codex_web.api.reconciliation_gates import build_reconciliation_gates_router
from codex_web.api.provider_capacity import build_provider_capacity_router
from codex_web.api.resources import build_resources_router
from codex_web.api.recovery import build_recovery_router
from codex_web.api.stale_active_turns import (
    build_stale_active_turn_router,
)
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
from codex_web.reconciliation_gates import ReconcilerDeclaration, ReconcilerStartupClass
from codex_web.configuration import ConfigurationContext
from codex_web.events import EventHub
from codex_web.executive_integration import install_executive_integrated
from codex_web.extension_packages import LocalExtensionPackageCatalog
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.integrations.slack_client import SlackClient
from codex_web.integrations.telegram_client import TelegramClient
from codex_web.integrations.webhook_security import (
    install_webhook_security,
    verify_gitlab_token,
)
from codex_web.devhealth import (
    build_context as build_devhealth_context,
    render_html as render_devhealth_html,
)
from codex_web.devstatus import (
    build_context as build_devstatus_context,
    render_html as render_devstatus_html,
)
from codex_web.models import GitLabProjectRoutingSettings
from codex_web.identity import TenantScope
from codex_web.model_providers import AnthropicModelProviderAdapter, OpenAIModelProviderAdapter
from codex_web.key_backends import LocalFileKeyBackend
from codex_web.execution_workspace_backend import LocalGitWorkspaceBackend
from codex_web.local_execution_backend import BubblewrapExecutionBackend
from codex_web.execution_workers import ExecutionRuntimeBinding, WorkerCapability
from codex_web.paths import (
    ACTIVE_TURNS_FILE,
    ARTIFACT_CONTENT_DIR,
    BOT_DELIVERY_TARGETS_FILE,
    BOT_REPLY_TARGETS_FILE,
    BOTS_BINDINGS_FILE,
    BOTS_CONNECTIONS_FILE,
    BOTS_EVENTS_FILE,
    EXECUTION_WORKSPACE_DIR,
    GITLAB_SEMANTIC_EVENTS_FILE,
    EXTENSION_PACKAGE_DIR,
    KEY_MATERIAL_DIR,
    DATA_DIR,
    PROJECTS_FILE,
    SECRET_MATERIAL_DIR,
    STATE_DB_FILE,
    STATIC_DIR,
    THREAD_INDEX_FILE,
    THREAD_SETTINGS_FILE,
    TURN_QUEUE_FILE,
    WORK_ITEM_EVENTS_FILE,
    WORK_ITEM_STATES_FILE,
)
from codex_web.runtime import core
from codex_web.runtime.bots import install_bot_runtime
from codex_web.runtime.codex import install_codex_runtime
from codex_web.runtime.execution import install_turn_execution_service
from codex_web.runtime.process import run_server, sd_notify
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.agent_teams import AgentTeamService
from codex_web.services.agent_team_execution import AgentTeamExecutionService
from codex_web.services.skills import SkillService
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
from codex_web.services.autonomy_dependencies import AutonomyRuntimeDependencies
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.autonomy_control_center import AutonomyControlCenterService
from codex_web.services.autonomy_policy import AutonomyPolicyService
from codex_web.services.autonomy_audit import AutonomyAuditService
from codex_web.services.automation_definitions import (
    AutomationEventTriggerService,
    AutomationScheduleMaterializer,
    install_automation_definitions,
)
from codex_web.services.automation_runs import (
    AutomationRunService,
    AutomationTriggerAdmissionBridge,
)
from codex_web.services.automation_execution import AutomationExecutionService
from codex_web.services.automation_outcomes import AutomationOutcomeReconciliationService
from codex_web.services.watchdog_dispatch import install_watchdog_dispatch_policy
from codex_web.services.agent_channel_preferences import install_agent_channel_preference_service
from codex_web.services.bot_binding_selection import install_bot_binding_selection_service
from codex_web.services.bot_bindings import install_bot_binding_lifecycle_service
from codex_web.services.bot_channels import install_bot_channel_discovery_service
from codex_web.services.bot_connections import install_bot_connection_service
from codex_web.services.bot_details import install_bot_detail_service
from codex_web.services.bot_presentation import install_bot_presentation_service
from codex_web.services.bot_runtime_telemetry import install_bot_runtime_telemetry
from codex_web.services.bot_targets import install_bot_target_service
from codex_web.services.bot_webhook_security import install_bot_webhook_security_service
from codex_web.services.bot_delivery import install_bot_delivery_service
from codex_web.services.bot_event_dispatch import (
    BotEventDispatchCompatibilityFacade,
    BotEventDispatchService,
)
from codex_web.services.bot_routing import install_bot_routing_service
from codex_web.services.bots import BotService
from codex_web.services.conversation_channels import (
    ConversationChannelRegistry,
    ConversationChannelService,
)
from codex_web.services.conversation_channel_adapters import (
    SlackConversationChannel,
    TelegramConversationChannel,
    TeamsConversationChannel,
)
from codex_web.services.configuration import ConfigurationService
from codex_web.services.capacity import CapacityService
from codex_web.services.canonical_materialization import (
    CanonicalMaterializationService,
)
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
from codex_web.services.codex_cli_agent_runtime import CodexCliAgentRuntimeAdapter
from codex_web.services.claude_agent_runtime import ClaudeAgentRuntimeAdapter
from codex_web.services.codex_worker_configuration import CODEX_WORKER_ACCESS_TOKEN_CONFIG, install_codex_worker_configuration
from codex_web.services.anthropic_worker_configuration import ANTHROPIC_WORKER_API_KEY_CONFIG, install_anthropic_worker_configuration
from codex_web.services.agent_model_egress import (
    AgentRuntimeModelEgressEndpoint,
    model_egress_endpoints_from_base_urls,
)
from codex_web.services.codex_worker_session import AssignmentBoundCodexSessionManager
from codex_web.services.cli_worker_session import AssignmentBoundCliSessionManager
from codex_web.services.code_hosts import CodeHostRegistry, CodeHostService
from codex_web.services.claude_worker_session import AssignmentBoundClaudeSessionManager
from codex_web.services.context import ContextCompactionService
from codex_web.services.control_plane_broker import (
    ControlPlaneBrokerService,
    DeferredControlPlaneBrokerFactory,
)
from codex_web.services.crypto_keys import CryptoKeyService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
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
from codex_web.services.work_item_business_data_source import (
    WORK_ITEM_BUSINESS_DATA_SOURCE_INSTANCE,
    WORK_ITEM_BUSINESS_DATA_SOURCE_TYPE,
    WorkItemBusinessDataSource,
)
from codex_web.services.decisions import DecisionService
from codex_web.services.decision_deliberation import DecisionDeliberationService
from codex_web.services.decision_work import DecisionWorkService
from codex_web.services.entitlements import EntitlementService
from codex_web.services.evaluations import EvaluationService
from codex_web.services.extensions import ExtensionService
from codex_web.services.extension_runtime import ExtensionRuntimeRegistry
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.services.local_artifact_content import LocalArtifactContentStore
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
from codex_web.services.legacy_project_migration import LegacyProjectMigrationService
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.gitlab import (
    install_gitlab_compatibility,
    install_gitlab_service,
)
from codex_web.services.gitlab_event_presentation import (
    GitLabEventPresentationService,
)
from codex_web.services.gitlab_dependencies import (
    GitLabOperationalDependencies,
    GitLabRoutingDependencies,
    GitLabWorkItemRuntimeDependencies,
)
from codex_web.services.gitlab_sync_health import GitLabSyncHealth
from codex_web.services.gitlab_code_host import GitLabCodeHostProvider
from codex_web.services.github_code_host import GitHubCodeHostProvider
from codex_web.services.goals import GoalService
from codex_web.services.home_overview import HomeOverviewService
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
from codex_web.services.project_ui_state import ProjectUiStateService
from codex_web.services.project_bootstrap import ProjectBootstrapService
from codex_web.services.operational_compaction import (
    OperationalCompactionService,
)
from codex_web.services.fresh_project_bootstrap import FreshProjectBootstrapService
from codex_web.services.project_readiness import ProjectReadinessService
from codex_web.services.reconciliation_gates import ReconciliationGateService
from codex_web.services.project_runtime import ProjectRuntimeService
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
from codex_web.services.runtime_diagnostics import (
    RuntimeDiagnosticsService,
    RuntimeHealthService,
    StaticAssetVersionService,
)
from codex_web.services.operator_ui import OperatorUiService
from codex_web.services.scheduler import SchedulerService
from codex_web.services.secrets import SecretBroker
from codex_web.services.security_boundary import SecurityBoundaryService
from codex_web.services.runtime_supervisor import install_runtime_supervisor
from codex_web.services.stale_active_turns import (
    ActiveTurnRecoveryStore,
    StaleActiveTurnRecoveryService,
)
from codex_web.services.runtime_policy import RuntimePolicy
from codex_web.services.native_recovery import (
    DeferredRecoveryScheduler,
    NativeRecoveryService,
)
from codex_web.services.slack_provider import install_slack_provider_service
from codex_web.services.task_source_action_provider import TaskSourceActionProvider
from codex_web.services.task_source_sync_jobs import GitLabSyncJobService
from codex_web.services.thread_recovery import install_thread_recovery_service
from codex_web.services.thread_execution_settings import install_thread_execution_settings_service
from codex_web.services.thread_resume import ThreadResumeService
from codex_web.services.thread_naming import ThreadNamingService
from codex_web.services.thread_bot_collaboration import ThreadBotCollaborationService
from codex_web.services.thread_compatibility import install_thread_compatibility_facade
from codex_web.services.thread_bootstrap_bindings import (
    ThreadBootstrapBindingService,
)
from codex_web.services.threads import ThreadService
from codex_web.services.turn_queue_policy import install_turn_queue_policy
from codex_web.services.turn_execution_binding import TurnExecutionBindingService
from codex_web.services.turns import TurnService
from codex_web.services.work_item_runs import WorkItemRunProjectionService
from codex_web.services.work_item_execution import WorkItemExecutionLifecycleService
from codex_web.services.work_item_state import install_work_item_state_machine
from codex_web.services.work_item_continuity import (
    DeferredWorkItemContinuityService,
    WorkItemContinuityService,
    build_work_item_continuity_compatibility_service,
)
from codex_web.services.work_item_dispatch_prompt import (
    WorkItemDispatchPromptPolicy,
)
from codex_web.services.work_item_timing import install_work_item_timing_policy
from codex_web.services.work_item_wakeups import install_work_item_wakeup_queue_policy
from codex_web.services.work_item_watchdog_candidates import install_work_item_watchdog_candidate_policy
from codex_web.services.work_item_watchdog_prompts import install_work_item_watchdog_prompt_policy
from codex_web.services.work_item_contracts import install_work_item_contract_service
from codex_web.services.work_item_dependencies import (
    DEFAULT_RELEASE_OWNER,
    DEFAULT_VALIDATION_OWNER,
    HANDOFF_COORDINATION_CHANNEL,
    NON_IMPLEMENTATION_OWNERS,
    OWNER_QUEUE_AGENTS,
    GitLabWorkItemDependencies,
    WorkItemRuntimeDependencies,
    default_leading_owner_cue,
    gitlab_group_path,
    gitlab_label_names,
    gitlab_mr_refs_from_payload,
    gitlab_owner_agents,
    gitlab_project_issue_ref,
    gitlab_token_for_project,
    gitlab_url,
)
from codex_web.services.workflow_claims import WorkflowClaimPolicy
from codex_web.services.work_items import (
    WorkItemService,
    install_work_item_compatibility,
)
from codex_web.services.work_graph import WorkGraphService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.storage.agent_teams import AgentTeamStore
from codex_web.storage.agent_providers import AgentProviderStore
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
from codex_web.storage.approval_requests import ApprovalRequestStore
from codex_web.storage.attention import AttentionStore
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.autonomy_audit import AutonomyAuditStore
from codex_web.storage.automation_runs import AutomationRunStore
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.auxiliary_state import install_auxiliary_state
from codex_web.storage.entitlements import EntitlementStore
from codex_web.storage.evaluations import EvaluationStore
from codex_web.storage.extensions import ExtensionStateStore
from codex_web.storage.crypto_keys import CryptoKeyStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.executive_activations import ExecutiveActivationStore
from codex_web.storage.goals import GoalStore
from codex_web.storage.metrics import MetricStore
from codex_web.storage.goal_decompositions import GoalDecompositionStore
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from codex_web.storage.task_source_sync_jobs import GitLabSyncJobStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.incidents import IncidentStore
from codex_web.storage.legacy_project_migration import LegacyProjectMigrationStore
from codex_web.storage.project_bootstrap import ProjectBootstrapStore
from codex_web.storage.project_readiness import ProjectReadinessStore
from codex_web.storage.model_gateway import ModelGatewayStore
from codex_web.storage.organizational_memory import OrganizationalMemoryStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.security_events import SecurityEventStore
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.storage.control_plane_broker import ControlPlaneBrokerAuditStore
from codex_web.storage.capacity import CapacityStore
from codex_web.storage.canonical_materialization import (
    CanonicalMaterializationStore,
)
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.configuration_state import (
    install_configuration_state,
    normalize_string_list,
)
from codex_web.storage.conversation_channels import ConversationChannelStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.decisions import DecisionStore
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.business_context import BusinessContextStore
from codex_web.storage.business_data_sources import BusinessDataSourceStore
from codex_web.storage.business_kpis import BusinessKPIStore
from codex_web.storage.bot_state import (
    BotStateRepositories,
    IndexedBotBindingRepository,
)
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
from codex_web.storage.work_item_list_index import WorkItemListIndex
from codex_web.storage.work_item_source_identity_index import (
    WorkItemSourceIdentityIndex,
)
from codex_web.storage.thread_index import install_thread_index_repository
from codex_web.storage.turn_queue import TurnQueueRepository
from codex_web.secret_backends import LocalFileSecretBackend


# Application and EventHub ownership lives in the composed application layer.
# The compatibility module receives output-only aliases for historical
# `import server` callers; startup no longer depends on a legacy-owned app.
app = FastAPI(title="Codex Web Local")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
event_hub = EventHub()
core.app = app
core.hub = event_hub

# Verified import-server compatibility data. Canonical ownership remains in
# codex_web.paths; the compatibility namespace only mirrors these references.
core.DATA_DIR = DATA_DIR
core.WORK_ITEM_STATES_FILE = WORK_ITEM_STATES_FILE
core.WORK_ITEM_EVENTS_FILE = WORK_ITEM_EVENTS_FILE
core.BOTS_EVENTS_FILE = BOTS_EVENTS_FILE
core.GITLAB_SEMANTIC_EVENTS_FILE = GITLAB_SEMANTIC_EVENTS_FILE
core.GitLabProjectRoutingSettings = GitLabProjectRoutingSettings

runtime_policy = RuntimePolicy(DATA_DIR)
app.state.runtime_policy = runtime_policy

# Compatibility names are output-only while callers migrate to the policy.
core._autonomy_enabled = runtime_policy.autonomy_enabled
core._owner_work_watchdog_interval = (
    runtime_policy.owner_work_watchdog_interval
)
core._release_gate_watchdog_interval = (
    runtime_policy.release_gate_watchdog_interval
)
core._work_item_sla_watchdog_interval = (
    runtime_policy.work_item_sla_watchdog_interval
)
core._orchestrator_watchdog_interval = (
    runtime_policy.orchestrator_watchdog_interval
)
core._split_brain_watchdog_interval = (
    runtime_policy.split_brain_watchdog_interval
)
core._actionable_owner_continuity_delay_seconds = (
    runtime_policy.actionable_owner_continuity_delay
)
core._handoff_continuity_delay_seconds = (
    runtime_policy.handoff_continuity_delay
)
core._native_recovery_schedule_cooldown_seconds = (
    runtime_policy.native_recovery_cooldown
)

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
execution_preflight_store = ExecutionPreflightStore(state_store)
app.state.execution_preflight_store = execution_preflight_store
project_repository = ProjectRepository(
    PROJECTS_FILE,
    store=state_store,
)
runtime_state = RuntimeStateRepositories(
    state_store,
    thread_settings_file=THREAD_SETTINGS_FILE,
    active_turns_file=ACTIVE_TURNS_FILE,
    work_item_states_file=WORK_ITEM_STATES_FILE,
)
app.state.runtime_state_repositories = runtime_state
work_item_list_index = WorkItemListIndex(state_store)
work_item_source_identity_index = WorkItemSourceIdentityIndex(state_store)
_initial_work_item_states = runtime_state.work_item_states.load()
work_item_list_index.rebuild(_initial_work_item_states)
work_item_source_identity_index.rebuild(_initial_work_item_states)
app.state.work_item_list_index = work_item_list_index
app.state.work_item_source_identity_index = work_item_source_identity_index

def _save_work_item_states(values):
    runtime_state.work_item_states.save(values)
    work_item_list_index.rebuild(values)
    work_item_source_identity_index.rebuild(values)

def _get_work_item_state_by_source_identity(identity):
    ref = work_item_source_identity_index.ref_for_identity(identity)
    if not ref:
        return None
    return runtime_state.work_item_states.get(ref)

def _put_work_item_state_record(state):
    previous = runtime_state.work_item_states.get(state.ref)
    runtime_state.work_item_states.put(state.ref, state)
    try:
        work_item_source_identity_index.upsert(state)
        work_item_list_index.upsert(state)
    except Exception:
        if previous is None:
            runtime_state.work_item_states.delete(state.ref)
            work_item_source_identity_index.remove(state.ref)
            work_item_list_index.remove(state.ref)
        else:
            runtime_state.work_item_states.put(previous.ref, previous)
            work_item_source_identity_index.upsert(previous)
            work_item_list_index.upsert(previous)
        raise

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
app.state.sqlite_state_store = state_store
configuration_state = install_configuration_state(app, core)

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
    loop.create_task(event_hub.publish(event))

definition_registry_store = DefinitionRegistryStore(state_store)
definition_registry_service = DefinitionRegistryService(
    definition_registry_store,
    notifier=_definition_change_notifier,
)
skill_service = SkillService(definition_registry_service)
app.state.skill_service = skill_service

automation_definition_service = install_automation_definitions(
    definition_registry_service
)
automation_event_trigger_service = AutomationEventTriggerService(
    definition_registry_service,
    canonical_event_bus,
)
automation_schedule_materializer = AutomationScheduleMaterializer(
    automation_definition_service,
    scheduler_service,
)
automation_run_store = AutomationRunStore(state_store)
automation_run_service = AutomationRunService(
    automation_run_store,
    automation_definition_service,
)
automation_trigger_admission_bridge = AutomationTriggerAdmissionBridge(
    automation_run_service,
    automation_event_trigger_service,
    canonical_event_bus,
)
automation_trigger_unsubscribe = automation_trigger_admission_bridge.install()
app.state.automation_definition_service = automation_definition_service
app.state.automation_event_trigger_service = automation_event_trigger_service
app.state.automation_schedule_materializer = automation_schedule_materializer
app.state.automation_run_store = automation_run_store
app.state.automation_run_service = automation_run_service
app.state.automation_trigger_admission_bridge = automation_trigger_admission_bridge
app.state.automation_trigger_unsubscribe = automation_trigger_unsubscribe
execution_role_definition_service = install_execution_role_definitions(
    definition_registry_service
)
execution_profile_definition_service = install_execution_profile_definitions(
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
app.state.execution_profile_definition_service = execution_profile_definition_service
app.state.executive_role_definition_service = executive_role_definition_service
app.include_router(
    build_execution_profiles_router(execution_profile_definition_service)
)
app.state.agent_routing_definition_service = agent_routing_definition_service
app.state.input_pipeline_definition_service = input_pipeline_definition_service
app.include_router(build_input_plugins_router(input_pipeline_definition_service))
core._execution_role_definition_service = execution_role_definition_service

def _work_item_definition_usage(reference):
    items = []
    try:
        states = runtime_state.work_item_states.load()
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
app.include_router(
    build_automations_router(
        automation_definition_service,
        automation_run_service,
        automation_trigger_admission_bridge,
        automation_schedule_materializer,
        projects=project_service,
    )
)
project_runtime_service = ProjectRuntimeService(project_service)
app.state.project_runtime_service = project_runtime_service
# Compatibility names now resolve to the extracted project runtime owner.
core._project = project_runtime_service.get
core._project_for_cwd = project_runtime_service.find_by_cwd
core._project_params = project_runtime_service.params
core._sandbox_policy = project_runtime_service.sandbox_policy

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
    identity=identity_service,
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

control_plane_broker_factory = DeferredControlPlaneBrokerFactory()
execution_worker_store = ExecutionWorkerStore(state_store)

def _execution_assignment_notifier(assignment, event_type):
    if assignment.status.value in {"succeeded", "failed", "cancelled", "lost"}:
        outcome_service = getattr(
            app.state,
            "automation_outcome_reconciliation_service",
            None,
        )
        if outcome_service is not None:
            try:
                outcome_actor = identity_service.bootstrap_service_actor(
                    identity_id="service-automation-outcome-reconciler",
                    name="Automation Outcome Reconciler",
                    scope=TenantScope(
                        organization_id=assignment.organization_id,
                        workspace_id=assignment.workspace_id,
                    ),
                    service_scopes=("automation:execute",),
                )
                outcome_runs = outcome_service.reconcile_for_execution(
                    assignment.execution_id,
                    actor=outcome_actor,
                )
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    for automation_run in outcome_runs:
                        if automation_run.status.value in {"succeeded", "failed"}:
                            loop.create_task(
                                outcome_service.sync_attention(
                                    automation_run,
                                    actor=outcome_actor,
                                )
                            )
            except Exception:
                # Outcome projection is observational and must not break the
                # canonical assignment transition that triggered it.
                pass

    if assignment.work_item_ref is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(
        event_hub.publish(
            {
                "type": "work_item.run.updated",
                "projectId": assignment.project_id,
                "workItemRef": assignment.work_item_ref,
                "executionId": assignment.execution_id,
                "assignmentId": assignment.id,
                "status": assignment.status.value,
                "eventType": event_type,
                "workspace_id": assignment.workspace_id,
                "work_item_ref": assignment.work_item_ref,
                "execution_id": assignment.execution_id,
            }
        )
    )
execution_worker_service = ExecutionWorkerService(
    execution_worker_store,
    identity=identity_service,
    workspaces=execution_workspace_service,
    assignment_notifier=_execution_assignment_notifier,
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
    supported_execution_contract_versions=(
        "1.0",
        "thread-turn/1.0",
        "thread-bootstrap/1.0",
    ),
    actor=identity_service.local_trusted_actor(),
)
app.include_router(
    build_execution_workers_router(
        execution_worker_service,
        control_plane_broker_factory=control_plane_broker_factory,
        local_isolation_status=lambda: local_execution_backend.probe(),
    )
)
app.state.execution_worker_store = execution_worker_store
app.state.execution_worker_service = execution_worker_service
automation_outcome_reconciliation_service = AutomationOutcomeReconciliationService(
    automation_run_service,
    execution_worker_service,
    attention=attention_service,
)
app.state.automation_outcome_reconciliation_service = (
    automation_outcome_reconciliation_service
)
app.include_router(
    build_automation_outcomes_router(
        automation_outcome_reconciliation_service
    )
)
app.state.control_plane_broker_factory = control_plane_broker_factory
app.state.local_execution_worker = local_execution_worker
app.state.local_execution_backend = local_execution_backend
app.state.local_execution_backend_status = local_execution_backend_status

codex_execution_runtime_binding = ExecutionRuntimeBinding(
    provider_id="openai",
    runtime_id="codex",
    capability_revision=1,
)
codex_cli_execution_runtime_binding = ExecutionRuntimeBinding(
    provider_id="openai",
    runtime_id="codex-cli",
    capability_revision=1,
    sandbox_profiles=("read-only", "workspace-write", "danger-full-access"),
)
claude_execution_runtime_binding = ExecutionRuntimeBinding(
    provider_id="anthropic",
    runtime_id="claude-code",
    capability_revision=1,
)

project_bootstrap_store = ProjectBootstrapStore(state_store)
project_readiness_store = ProjectReadinessStore(state_store)


def _project_readiness_environment(project, actor):
    del actor
    supported = project.sandbox in {
        "workspace-write",
        "read-only",
        "danger-full-access",
    }
    return {
        "available": supported,
        "code": (
            "sandbox_profile_supported"
            if supported
            else "sandbox_profile_unsupported"
        ),
        "reason": (
            "Project sandbox is supported by the canonical execution contract."
            if supported
            else "Project sandbox is not supported by the execution contract."
        ),
        "remediation": (
            None
            if supported
            else "Select a supported canonical sandbox/profile."
        ),
    }


project_readiness_service = ProjectReadinessService(
    projects=project_service,
    resources=resource_catalog_service,
    secrets=secret_broker,
    workers=execution_worker_service,
    bootstrap=project_bootstrap_store,
    store=project_readiness_store,
    load_work_items=runtime_state.work_item_states.load,
    environment_probe=_project_readiness_environment,
    configuration=configuration_service,
    runtime_binding=codex_execution_runtime_binding,
)
app.state.project_bootstrap_store = project_bootstrap_store
app.state.project_readiness_store = project_readiness_store
app.state.project_readiness_service = project_readiness_service
app.include_router(
    build_project_readiness_router(project_readiness_service)
)


def _reconciliation_readiness(project_id, actor):
    # First validate that the requesting/runtime actor belongs to the Project
    # scope. Readiness itself contains administrative worker/secret checks, so
    # evaluate it through a narrowly scoped internal control-plane principal.
    project_service.get(project_id, actor.tenant)
    control_actor = identity_service.bootstrap_service_actor(
        identity_id="reconciliation-gate-runtime",
        name="Reconciliation Gate Runtime",
        scope=actor.tenant,
        service_scopes=("identity:admin",),
    )
    return project_readiness_service.evaluate(
        project_id,
        actor=control_actor,
        record=False,
    ).model_dump(mode="json")


reconciliation_gate_service = ReconciliationGateService(
    state_store,
    readiness=_reconciliation_readiness,
    identity=identity_service,
)
for declaration in (
    ReconcilerDeclaration(
        service_id="bot-runtime",
        startup_class=ReconcilerStartupClass.PRE_READINESS_BOUNDED,
        description="Bounded live bot/webhook runtime; safe before Project readiness.",
    ),
    ReconcilerDeclaration(
        service_id="queue-recovery",
        startup_class=ReconcilerStartupClass.PRE_READINESS_BOUNDED,
        description="Bounded local queue recovery; execution binding remains readiness-gated.",
    ),
    ReconcilerDeclaration(
        service_id="event-transport",
        startup_class=ReconcilerStartupClass.ALWAYS_SAFE,
        description="Canonical event transport lifecycle.",
    ),
    ReconcilerDeclaration(
        service_id="scheduler",
        startup_class=ReconcilerStartupClass.PRE_READINESS_BOUNDED,
        description="Durable scheduler lifecycle; individual actions retain authority gates.",
    ),
):
    reconciliation_gate_service.register(declaration)

reconciliation_gate_service.register(
    ReconcilerDeclaration(
        service_id="slack-backfill",
        startup_class=ReconcilerStartupClass.OPERATOR_APPROVAL_REQUIRED,
        readiness_required=True,
        initial_approval_required=True,
        maintenance_incompatible=True,
        readiness_check="bootstrap:status",
        description=(
            "Slack missed-message historical polling; webhook ingestion is "
            "independent and remains active while this gate is blocked."
        ),
    )
)
app.state.reconciliation_gate_service = reconciliation_gate_service
app.include_router(
    build_reconciliation_gates_router(reconciliation_gate_service)
)

turn_execution_binding_service = TurnExecutionBindingService(
    configuration_service,
    project_service,
    resource_catalog_service,
    execution_workspace_service,
    execution_worker_service,
    control_actor=identity_service.local_trusted_actor(),
    runtime_binding=codex_execution_runtime_binding,
    secrets=secret_broker,
    execution_profiles=execution_profile_definition_service,
    control_plane_available=lambda: (
        control_plane_broker_factory.service is not None
    ),
    project_readiness=lambda project_id, actor: (
        project_readiness_service.evaluate(
            project_id,
            actor=actor,
            record=False,
        ).model_dump(mode="json")
    ),
    skill_worker_requirements=lambda refs, project: (
        skill_service.requirements_for_refs_scoped(
            refs,
            organization_id=project.organization_id,
            workspace_id=project.workspace_id,
        )[1]
    ),
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
            work_state = runtime_state.work_item_states.get(work_item_ref)
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

def _agent_profile_role_ref(role_id, actor):
    role, definition_ref = agent_routing_definition_service.resolve(
        role_id=role_id,
        organization_id=actor.organization_id,
        workspace_id=actor.workspace_id,
    )
    if role is None:
        raise ValueError(f"unknown agent routing role: {role_id}")
    return definition_ref


def _agent_profile_assignment_history(actor):
    return [
        item
        for item in execution_worker_store.load().assignments
        if item.organization_id == actor.organization_id
        and item.workspace_id == actor.workspace_id
    ]


agent_profile_store = AgentProfileStore(state_store)
agent_profile_service = AgentProfileService(
    agent_profile_store,
    definitions=definition_registry_service,
    authority=authority_role_service,
    execution_profiles=execution_profile_definition_service,
    role_resolver=_agent_profile_role_ref,
    assignment_history=_agent_profile_assignment_history,
    skill_reference_validator=lambda ref, actor: (
        skill_service.validate_reference(ref, actor=actor)
    ),
)
skill_service.bind_profiles(agent_profile_service)
app.state.agent_profile_store = agent_profile_store
app.state.agent_profile_service = agent_profile_service
app.include_router(build_agent_profiles_router(agent_profile_service))
app.include_router(build_skills_router(skill_service))

agent_team_store = AgentTeamStore(state_store)
agent_team_service = AgentTeamService(
    agent_team_store,
    profiles=agent_profile_service,
    definitions=definition_registry_service,
    attention=attention_service,
    runtime_usage_store=agent_runtime_usage_store,
    assignment_loader=lambda: execution_worker_store.load().assignments,
)
app.state.agent_team_store = agent_team_store
app.state.agent_team_service = agent_team_service
app.include_router(build_agent_teams_router(agent_team_service))


def _agent_profile_definition_usage(reference):
    items = []
    for profile in agent_profile_store.load().revisions:
        refs = [
            profile.instructions_ref,
            profile.role_definition_ref,
            profile.authority_definition_ref,
            *profile.skill_refs,
        ]
        if not any(
            item is not None
            and item.record_id == reference.record_id
            for item in refs
        ):
            continue
        items.append(
            {
                "object_type": "agent_profile",
                "object_id": profile.profile_id,
                "revision": profile.revision,
                "lifecycle": profile.lifecycle.value,
                "organization_id": profile.organization_id,
                "workspace_id": profile.workspace_id,
            }
        )
    return items


definition_registry_service.register_usage_provider(
    _agent_profile_definition_usage
)

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
    profiles=agent_profile_service,
    skills=skill_service,
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
    control_plane_broker_factory=control_plane_broker_factory,
)
app.state.assignment_bound_codex_session_manager = assignment_bound_codex_session_manager

assignment_bound_codex_cli_session_manager = AssignmentBoundCliSessionManager(
    local_execution_worker_runtime,
    runtime_binding=codex_cli_execution_runtime_binding,
)
app.state.assignment_bound_codex_cli_session_manager = (
    assignment_bound_codex_cli_session_manager
)

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
    control_plane_broker_factory=control_plane_broker_factory,
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
    execution_workers=execution_worker_service,
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

def _resource_ids_for_project(
    project_id: str,
    alias_value: str | None = None,
    provider: str | None = None,
) -> list[str]:
    project = project_service.get(project_id)
    return resource_catalog_service.resource_ids_for_project(
        project,
        alias_value=alias_value,
        provider=provider,
    )

core._resource_ids_for_project = _resource_ids_for_project
approval_compatibility_actor = identity_service.local_trusted_actor()
codex_approval_requester = identity_service.bootstrap_service_actor(
    identity_id="service-codex-approval-requester",
    name="Codex approval requester",
    scope=approval_compatibility_actor.tenant,
    service_scopes=("approvals:request",),
)
def _assignment_runtime_adapter(binding, session):
    key = (binding.provider_id, binding.runtime_id)
    if key == ("openai", "codex"):
        return CodexAgentRuntimeAdapter(session)
    if key == ("openai", "codex-cli"):
        return codex_cli_agent_adapter
    if key == ("anthropic", "claude-code"):
        return ClaudeAgentRuntimeAdapter(session)
    raise RuntimeError(
        f"unsupported assignment-bound agent runtime: {binding.provider_id}/{binding.runtime_id}"
    )


assignment_session_managers = {
    ("openai", "codex"): assignment_bound_codex_session_manager,
    ("openai", "codex-cli"): assignment_bound_codex_cli_session_manager,
    ("anthropic", "claude-code"): assignment_bound_claude_session_manager,
}

gitlab_client = GitLabClient()
work_item_dependencies = WorkItemRuntimeDependencies(
    data_dir=DATA_DIR,
    events_file=WORK_ITEM_EVENTS_FILE,
    load_states=runtime_state.work_item_states.load,
    save_states=_save_work_item_states,
    load_projects=project_repository.load,
    resource_ids_for_project=_resource_ids_for_project,
    leading_owner_cue_in_action=default_leading_owner_cue,
    default_validation_owner=DEFAULT_VALIDATION_OWNER,
    default_release_owner=DEFAULT_RELEASE_OWNER,
    non_implementation_owners=NON_IMPLEMENTATION_OWNERS,
    get_state=runtime_state.work_item_states.get,
    get_state_by_source_identity=_get_work_item_state_by_source_identity,
    save_state=_put_work_item_state_record,
)
gitlab_work_item_dependencies = GitLabWorkItemDependencies(
    api_base_url=os.environ.get(
        "CODEX_WEB_GITLAB_API_BASE",
        "https://dev.veridataops.com/gitlab/api/v4",
    ),
    token_for_project=lambda project_id: gitlab_token_for_project(
        project_id,
        project_lookup=project_runtime_service.get,
    ),
    group_path=gitlab_group_path,
    load_routing_settings=configuration_state.gitlab_routing.load,
    project_issue_ref=gitlab_project_issue_ref,
    label_names=gitlab_label_names,
    owner_agents=gitlab_owner_agents,
    url=gitlab_url,
    mr_refs_from_payload=gitlab_mr_refs_from_payload,
)
app.state.work_item_dependencies = work_item_dependencies
app.state.gitlab_work_item_dependencies = gitlab_work_item_dependencies
work_item_state_machine = install_work_item_state_machine(
    app,
    core,
    gitlab_client,
    store=state_store,
    dependencies=work_item_dependencies,
)
work_item_dispatch_prompt_policy = WorkItemDispatchPromptPolicy(
    coerce_owner=work_item_state_machine._coerce_owner,
    coordination_channel=HANDOFF_COORDINATION_CHANNEL,
)
work_item_contract_service = install_work_item_contract_service(
    app,
    None,
    execution_role_definition_service,
    execution_profile_definition_service,
    base_formatter=work_item_dispatch_prompt_policy.render,
    split_brain_findings=(
        work_item_state_machine._work_item_split_brain_findings
    ),
    save_state=work_item_state_machine._save_work_item_state,
    append_event=work_item_state_machine._append_work_item_event,
)
# Telemetry has no dependency on bot repositories/runtime tasks, so compose it
# before work-item services that need an explicit event sink.
bot_runtime_telemetry = install_bot_runtime_telemetry(
    app,
    core,
    events_file=BOTS_EVENTS_FILE,
)
watchdog_dispatch_policy = install_watchdog_dispatch_policy(app, core)

work_item_continuity_service = DeferredWorkItemContinuityService()
app.state.work_item_continuity_service = work_item_continuity_service

def _mirror_gitlab_sync_health(snapshot):
    core.GITLAB_SYNC_CONSECUTIVE_FAILURES = snapshot[
        "consecutive_failures"
    ]
    core.GITLAB_SYNC_LAST_ERROR = snapshot["last_error"]
    core.GITLAB_SYNC_LAST_ERROR_AT = snapshot["last_error_at"]
    core.GITLAB_SYNC_LAST_SUCCESS_AT = snapshot["last_success_at"]


gitlab_sync_health = GitLabSyncHealth(
    on_change=_mirror_gitlab_sync_health,
)
_mirror_gitlab_sync_health(gitlab_sync_health.snapshot())
app.state.gitlab_sync_health = gitlab_sync_health

work_item_recovery_scheduler = DeferredRecoveryScheduler()
app.state.work_item_recovery_scheduler = work_item_recovery_scheduler

work_item_service = WorkItemService(
    None,
    gitlab_client,
    work_item_state_machine,
    continuity=work_item_continuity_service,
    recovery=work_item_recovery_scheduler,
    sync_health=gitlab_sync_health,
    event_sink=bot_runtime_telemetry.append,
    publish_event=event_hub.publish,
    truncate_text=lambda value, limit: str(value)[:limit],
    work_item_dependencies=work_item_dependencies,
    gitlab_dependencies=gitlab_work_item_dependencies,
    identity_service=identity_service,
    work_item_list_index=work_item_list_index,
    secret_broker=secret_broker,
)
business_data_source_registry.register(
    WORK_ITEM_BUSINESS_DATA_SOURCE_TYPE,
    lambda record, _actor: WorkItemBusinessDataSource(
        source_instance=record.source_instance,
        project_id=record.scope,
        load_states=work_item_dependencies.load_states,
        load_projects=work_item_dependencies.load_projects,
    )
    if record.source_instance == WORK_ITEM_BUSINESS_DATA_SOURCE_INSTANCE
    else None,
)
work_item_compatibility_service = install_work_item_compatibility(
    app,
    core,
    work_item_service,
)
app.state.work_item_service = work_item_service
work_item_execution_lifecycle_service = WorkItemExecutionLifecycleService(
    None,
    work_item_state_machine,
    dependencies=work_item_dependencies,
)
app.state.work_item_execution_lifecycle_service = (
    work_item_execution_lifecycle_service
)
app.state.task_source_registry = work_item_service.task_source_registry
app.state.task_source_writeback_service = (
    work_item_service.task_source_writeback
)

gitlab_sync_job_store = GitLabSyncJobStore(state_store)
gitlab_sync_job_service = GitLabSyncJobService(
    gitlab_sync_job_store,
    work_item_service,
)
app.state.gitlab_sync_job_store = gitlab_sync_job_store
app.state.gitlab_sync_job_service = gitlab_sync_job_service

control_plane_broker_audit_store = ControlPlaneBrokerAuditStore(state_store)
control_plane_broker_service = ControlPlaneBrokerService(
    identity=identity_service,
    authority=authority_role_service,
    work_items=work_item_service,
    audit=control_plane_broker_audit_store,
)
control_plane_broker_factory.configure(control_plane_broker_service)
app.state.control_plane_broker_audit_store = control_plane_broker_audit_store
app.state.control_plane_broker_service = control_plane_broker_service
app.include_router(build_control_plane_broker_router(control_plane_broker_service))

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
# Legacy code still needing project/runtime state consumes the extracted
# repositories. Canonical keyed mutations avoid whole-registry rewrites.
# Legacy JSON remains a compatibility checkpoint and is refreshed by bulk
# compatibility saves rather than every keyed hot-path mutation.
core._load_projects = project_repository.load
core._save_projects = project_repository.save
core._load_thread_settings = runtime_state.thread_settings.load
core._save_thread_settings = runtime_state.thread_settings.save
core._get_thread_setting_record = runtime_state.thread_settings.get
core._put_thread_setting_record = runtime_state.thread_settings.put
core._delete_thread_setting_record = runtime_state.thread_settings.delete
core._load_active_turns = runtime_state.active_turns.load
core._save_active_turns = runtime_state.active_turns.save
core._get_active_turn_record = runtime_state.active_turns.get
core._put_active_turn_record = runtime_state.active_turns.put
core._delete_active_turn_record = runtime_state.active_turns.delete
core._load_work_item_states = runtime_state.work_item_states.load
core._save_work_item_states = _save_work_item_states
core._get_work_item_state_record = runtime_state.work_item_states.get
core._put_work_item_state_record = _put_work_item_state_record
core._delete_work_item_state_record = runtime_state.work_item_states.delete

bot_state = BotStateRepositories(
    state_store,
    connections_file=BOTS_CONNECTIONS_FILE,
    bindings_file=BOTS_BINDINGS_FILE,
    reply_targets_file=BOT_REPLY_TARGETS_FILE,
    delivery_targets_file=BOT_DELIVERY_TARGETS_FILE,
)
app.state.bot_state_repositories = bot_state
bot_binding_repository = IndexedBotBindingRepository(
    bot_state.bindings
)
app.state.bot_binding_repository = bot_binding_repository
# Transitional aliases for unextracted bot services. The authoritative mutable
# state lives canonically in BotStateRepositories.
core._load_bot_connections = bot_state.connections.load
core._save_bot_connections = bot_state.connections.save
core._load_bot_bindings = bot_binding_repository.load
core._save_bot_bindings = bot_binding_repository.save
assert bot_state.reply_targets is not None
assert bot_state.delivery_targets is not None
core._load_bot_reply_targets = bot_state.reply_targets.load
core._save_bot_reply_targets = bot_state.reply_targets.save
core._get_bot_reply_target_record = bot_state.reply_targets.get
core._put_bot_reply_target_record = bot_state.reply_targets.put
core._load_bot_delivery_targets = bot_state.delivery_targets.load
core._save_bot_delivery_targets = bot_state.delivery_targets.save
core._get_bot_delivery_target_record = bot_state.delivery_targets.get
core._put_bot_delivery_target_record = bot_state.delivery_targets.put

turn_queue_repository = TurnQueueRepository(
    state_store,
    TURN_QUEUE_FILE,
)
app.state.turn_queue_repository = turn_queue_repository
core._load_turn_queues = turn_queue_repository.load
core._save_turn_queues = turn_queue_repository.save
core._thread_queue_record = turn_queue_repository.get
core._update_thread_queue_record = turn_queue_repository.update
core._put_thread_queue_record = turn_queue_repository.put
core._delete_thread_queue_record = turn_queue_repository.delete

auxiliary_state = install_auxiliary_state(app, core)
bot_presentation_service = install_bot_presentation_service(app, core)
bot_detail_service = install_bot_detail_service(
    app,
    core,
    load_details=auxiliary_state.bot_details.load,
    save_details=auxiliary_state.bot_details.save,
)
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
    sandbox_profiles=("read-only", "workspace-write", "danger-full-access"),
    network_profiles=("brokered-model-egress",),
)
app.state.codex_agent_runtime_adapter = agent_runtime_registry.get("openai", "codex")
provider_capacity_service.register_probe(
    "openai",
    "codex",
    app.state.codex_agent_runtime_adapter.capacity_snapshot,
)
agent_runtime_telemetry_service.subscribe(app.state.codex_agent_runtime_adapter)

codex_cli_agent_adapter = CodexCliAgentRuntimeAdapter()
agent_runtime_registry.register(
    codex_cli_agent_adapter,
    capability_revision=1,
    sandbox_profiles=("read-only", "workspace-write", "danger-full-access"),
    network_profiles=("direct-provider-egress",),
)
app.state.codex_cli_agent_runtime_adapter = agent_runtime_registry.get(
    "openai",
    "codex-cli",
)
agent_runtime_telemetry_service.subscribe(
    app.state.codex_cli_agent_runtime_adapter
)

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
# Bot connection/binding lookup is needed by thread settings and collaboration.
bot_connection_service = install_bot_connection_service(
    app,
    core,
    projects=project_runtime_service,
    load_connections=bot_state.connections.load,
    save_connections=bot_state.connections.save,
    load_bindings=bot_binding_repository.load,
    save_bindings=bot_binding_repository.save,
    binding_prefix=bot_presentation_service.binding_prefix,
    secret_broker=secret_broker,
    identity_service=identity_service,
)
bot_binding_selection_service = install_bot_binding_selection_service(
    app,
    core,
    load_bindings=bot_binding_repository.load,
    binding_report_name=bot_presentation_service.binding_report_name,
    binding_prefix=bot_presentation_service.binding_prefix,
    lookup_by_id=bot_binding_repository.by_id,
    indexed_for_connection=bot_binding_repository.for_connection,
    indexed_for_thread=bot_binding_repository.for_thread,
    indexed_for_project=bot_binding_repository.for_project,
    indexed_for_project_all=bot_binding_repository.for_project_all,
    indexed_masters=bot_binding_repository.masters,
)

bot_target_service = install_bot_target_service(
    app,
    core,
    load_reply_targets=bot_state.reply_targets.load,
    save_reply_targets=bot_state.reply_targets.save,
    load_delivery_targets=bot_state.delivery_targets.load,
    save_delivery_targets=bot_state.delivery_targets.save,
    load_active_turns=runtime_state.active_turns.load,
    bindings_for_project=bot_binding_selection_service.for_project,
    put_reply_target=bot_state.reply_targets.put,
    put_delivery_target=bot_state.delivery_targets.put,
    get_reply_target=bot_state.reply_targets.get,
    get_delivery_target=bot_state.delivery_targets.get,
    get_active_turn=runtime_state.active_turns.get,
    page_reply_targets=bot_state.reply_targets.page,
    page_delivery_targets=bot_state.delivery_targets.page,
)

def _gitlab_routing_enabled_for_project(project_id: str) -> bool:
    settings = configuration_state.gitlab_routing.load()
    if not settings.enabled:
        return False
    project_settings = settings.projects.get(project_id)
    return bool(project_settings and project_settings.enabled)


thread_execution_settings_service = install_thread_execution_settings_service(
    app,
    core,
    load_settings=runtime_state.thread_settings.load,
    save_settings=runtime_state.thread_settings.save,
    bindings=bot_binding_selection_service,
    load_bindings=bot_binding_repository.load,
    save_bindings=bot_binding_repository.save,
    gitlab_routing_enabled_for_project=(
        _gitlab_routing_enabled_for_project
    ),
    binding_report_name=bot_presentation_service.binding_report_name,
    binding_prefix=bot_presentation_service.binding_prefix,
)

canonical_materialization_store = CanonicalMaterializationStore(
    state_store
)
canonical_materialization_service = CanonicalMaterializationService(
    projects=project_service,
    resources=resource_catalog_service,
    work_items=runtime_state.work_item_states,
    secrets=secret_broker,
    load_gitlab_routing=configuration_state.gitlab_routing.load,
    legacy_gitlab_token=gitlab_work_item_dependencies.token_for_project,
    gitlab_api_base=gitlab_work_item_dependencies.api_base_url,
    store=canonical_materialization_store,
)
app.state.canonical_materialization_store = canonical_materialization_store
app.state.canonical_materialization_service = canonical_materialization_service
app.include_router(
    build_canonical_materialization_router(
        canonical_materialization_service
    )
)

legacy_project_migration_store = LegacyProjectMigrationStore(state_store)
legacy_project_migration_service = LegacyProjectMigrationService(
    projects=project_service,
    resources=resource_catalog_service,
    thread_settings=thread_execution_settings_service,
    load_threads=thread_index_repository.load,
    load_bindings=bot_binding_repository.load,
    store=legacy_project_migration_store,
)
app.state.legacy_project_migration_store = legacy_project_migration_store
app.state.legacy_project_migration_service = legacy_project_migration_service
app.include_router(
    build_legacy_project_migration_router(legacy_project_migration_service)
)


def _bootstrap_task_source_health(project_id, manifest, actor):
    source = manifest.task_source
    if source is None:
        return {"available": True, "code": "task_source_not_configured"}
    if source.type.casefold() != "gitlab":
        return {
            "available": False,
            "code": "task_source_health_probe_unsupported",
        }
    snapshot = gitlab_sync_health.snapshot()
    failures = int(snapshot.get("consecutive_failures") or 0)
    return {
        "available": failures == 0,
        "code": (
            "gitlab_sync_healthy"
            if failures == 0
            else "gitlab_sync_degraded"
        ),
    }


def _bootstrap_environment_health(project_id, manifest, actor):
    isolation = local_execution_backend.probe()
    return {
        "available": bool(isolation.ready),
        "code": (
            "local_execution_environment_ready"
            if isolation.ready
            else "local_execution_environment_unavailable"
        ),
        "reason": (
            "Local execution isolation is ready."
            if isolation.ready
            else (
                isolation.reason
                or "Local execution isolation is unavailable."
            )
        ),
    }


def _bootstrap_authorization_check(actor):
    if actor.principal_kind.value == "service":
        IdentityService.require_admin(actor)
        return
    current = identity_service.actor_for_identity(
        actor.identity_id,
        scope=actor.tenant,
    )
    IdentityService.require_admin(current)


project_bootstrap_service = ProjectBootstrapService(
    projects=project_service,
    resources=resource_catalog_service,
    secrets=secret_broker,
    workers=execution_worker_service,
    state_store=state_store,
    canonical_materialization=canonical_materialization_service,
    legacy_migration=legacy_project_migration_service,
    store=project_bootstrap_store,
    task_source_health=_bootstrap_task_source_health,
    environment_health=_bootstrap_environment_health,
    authorization_check=_bootstrap_authorization_check,
)
app.state.project_bootstrap_store = project_bootstrap_store
app.state.project_bootstrap_service = project_bootstrap_service
fresh_project_bootstrap_service = FreshProjectBootstrapService(
    materialization=canonical_materialization_service,
    readiness=project_readiness_service,
)
app.state.fresh_project_bootstrap_service = fresh_project_bootstrap_service
project_bootstrap_service.readiness_probe = (
    lambda project_id, actor: project_readiness_service.evaluate(
        project_id,
        actor=actor,
        record=True,
    ).model_dump(mode="json")
)
app.include_router(build_project_bootstrap_router(project_bootstrap_service))

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
    bindings_for_thread=bot_binding_selection_service.for_thread,
    skill_context_resolver=lambda refs, project, objective: (
        skill_service.context_for_refs_scoped(
            refs,
            organization_id=project.organization_id,
            workspace_id=project.workspace_id,
            objective=objective,
        )
    ),
    work_item_context_resolver=(
        work_item_execution_lifecycle_service.continuation_delta
    ),
    work_item_context_recorder=(
        work_item_execution_lifecycle_service.record_continuation_delivery
    ),
    work_item_outcome_recorder=(
        work_item_execution_lifecycle_service.record_continuation_outcome
    ),
)
 
def _codex_cli_thread_event(event):
    thread_id = codex_cli_agent_adapter.logical_session_id_for(
        event.provider_native_session_id
    )
    if thread_id:
        turn_execution_service.record_agent_runtime_event(thread_id, event)

codex_cli_agent_adapter.subscribe_events(_codex_cli_thread_event)

async def _existing_thread_runtime_request(method, params):
    values = dict(params or {})
    thread_id = str(values.get("threadId") or "").strip()
    if not thread_id:
        raise RuntimeError(f"{method} requires threadId")
    return await turn_execution_service.request_for_thread(
        thread_id,
        method,
        values,
    )


async def _thread_recovery_runtime_request(method, params):
    values = dict(params or {})
    thread_id = str(values.get("threadId") or "").strip()
    if method != "thread/start" and thread_id:
        return await turn_execution_service.request_for_thread(
            thread_id,
            method,
            values,
        )
    return await codex_runtime.request(method, values)


thread_naming_service = ThreadNamingService(
    _existing_thread_runtime_request,
    thread_index_repository,
    bot_binding_repository.load,
    event_sink=bot_runtime_telemetry.append,
)
thread_resume_service = ThreadResumeService(
    _existing_thread_runtime_request,
    thread_index_repository,
    bot_binding_selection_service,
    event_sink=bot_runtime_telemetry.append,
    truncate_text=lambda value, limit: str(value)[:limit],
)
app.state.thread_naming_service = thread_naming_service
app.state.thread_resume_service = thread_resume_service

approval_service = ApprovalService(
    core,
    runtime_transport=codex_runtime,
    assignment_sessions=(
        assignment_bound_codex_session_manager,
        assignment_bound_claude_session_manager,
    ),
    canonical=approval_request_service,
    canonical_requester=codex_approval_requester,
    compatibility_actor=approval_compatibility_actor,
    approval_summary=bot_presentation_service.approval_summary,
    approval_result=ApprovalService.native_result,
    request_id_value=ApprovalService.normalize_request_id,
    load_approval_messages=auxiliary_state.approval_messages.load,
    save_approval_messages=auxiliary_state.approval_messages.save,
    load_active_turns=runtime_state.active_turns.load,
)
app.state.approval_service = approval_service

# Historical helper names are compatibility aliases to extracted owners.
core._set_thread_name = thread_naming_service.set_name
core._canonical_bot_thread_names = thread_naming_service.canonical_bot_names
core._restore_bot_thread_name = thread_naming_service.restore
core._restore_bot_thread_names = thread_naming_service.restore_all
core._is_codex_timeout_error = thread_resume_service.is_timeout_error
core._is_stale_thread_error = thread_resume_service.is_stale_thread_error
core._thread_read_timeout_response = thread_resume_service.read_timeout_response
core._web_thread_resume_handoff_timeout = thread_resume_service.handoff_timeout
core._thread_resume_retry_delay = thread_resume_service.retry_delay
core._web_thread_resume_task = thread_resume_service.schedule
core.WEB_THREAD_RESUME_TASKS = thread_resume_service.tasks

thread_recovery_service = install_thread_recovery_service(
    app,
    core,
    projects=project_runtime_service,
    settings=thread_execution_settings_service,
    naming=thread_naming_service,
    thread_index=thread_index_repository,
    runtime_request=_thread_recovery_runtime_request,
    terminal_failures=turn_execution_service.terminal_failures,
)

def _publish_binding_change(event: dict[str, object]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(event_hub.publish(event))


bot_binding_lifecycle_service = install_bot_binding_lifecycle_service(
    app,
    core,
    load_bindings=bot_binding_repository.load,
    save_bindings=bot_binding_repository.save,
    connections=bot_connection_service,
    selection=bot_binding_selection_service,
    targets=bot_target_service,
    presentation=bot_presentation_service,
    projects=project_runtime_service,
    runtime_request=codex_runtime.request,
    set_thread_name=thread_naming_service.set_name,
    on_change=_publish_binding_change,
)

thread_bot_collaboration_service = ThreadBotCollaborationService(
    project_runtime_service,
    _existing_thread_runtime_request,
    load_bindings=bot_binding_repository.load,
    save_bindings=bot_binding_repository.save,
    load_connections=bot_state.connections.load,
    upsert_binding=bot_binding_lifecycle_service.upsert,
)
app.state.thread_bot_collaboration_service = thread_bot_collaboration_service
core._project_scoped_bindings_for_thread = (
    thread_bot_collaboration_service.project_scoped_bindings_for_thread
)
core._set_thread_primary = thread_bot_collaboration_service.set_primary
core._set_thread_primary_channel = (
    thread_bot_collaboration_service.set_primary_channel
)

turn_queue_policy = install_turn_queue_policy(
    app,
    core,
    load_queues=turn_queue_repository.load,
)

context_service = ContextCompactionService(
    request_for_thread=turn_execution_service.request_for_thread,
    pending_approvals=approval_service.pending,
    approval_thread_id=approval_service.thread_id,
    queue_depth=turn_queue_policy.depth,
    thread_is_active=turn_execution_service.thread_is_active,
    raise_if_thread_replaced=(
        thread_recovery_service.raise_if_thread_replaced
    ),
    publish_event=event_hub.publish,
)

def _binding_public(binding):
    item = binding.model_dump()
    item["prefix"] = bot_presentation_service.binding_prefix(binding)
    item["report_name"] = (
        bot_presentation_service.binding_report_name(binding)
    )
    item["active"] = turn_execution_service.thread_is_active(
        binding.thread_id
    )
    item["queueDepth"] = turn_queue_policy.depth(binding.thread_id)
    if binding.provider == "slack":
        item["slack_icon"] = (
            bot_presentation_service.slack_reply_icon(binding)
        )
        item["slack_username"] = (
            bot_presentation_service.slack_reply_username(binding)
        )
    return item


thread_service = ThreadService(
    runtime_transport=codex_runtime,
    runtime_request_for_thread=turn_execution_service.request_for_thread,
    event_sink=bot_runtime_telemetry.append,
    binding_service=turn_execution_binding_service,
    session_manager=assignment_bound_codex_session_manager,
    bootstrap_bindings=thread_bootstrap_binding_service,
    control_actor=identity_service.local_trusted_actor(),
    agent_sessions=agent_session_service,
    agent_profiles=agent_profile_service,
    routing_service=agent_routing_service,
    session_managers=assignment_session_managers,
    runtime_adapter_factory=_assignment_runtime_adapter,
    project_runtime=project_runtime_service,
    settings=thread_execution_settings_service,
    recovery=thread_recovery_service,
    resume_runtime=thread_resume_service,
    naming=thread_naming_service,
    collaboration=thread_bot_collaboration_service,
    thread_index=thread_index_repository,
    active_turn_loader=runtime_state.active_turns.load,
    active_turn_getter=runtime_state.active_turns.get,
)
execution_preflight_service = ExecutionPreflightService(
    execution_preflight_store,
)
app.state.execution_preflight_service = execution_preflight_service

turn_service = TurnService(
    projects=project_runtime_service,
    settings=thread_execution_settings_service,
    recovery=thread_recovery_service,
    resume_runtime=thread_resume_service,
    bindings=bot_binding_selection_service,
    queue_policy=turn_queue_policy,
    execution=turn_execution_service,
    event_sink=bot_runtime_telemetry.append,
    truncate_text=lambda value, limit: str(value)[:limit],
    binding_public=_binding_public,
    preflight=execution_preflight_service,
    agent_profiles=agent_profile_service,
)
app.state.thread_service = thread_service
app.state.turn_service = turn_service

agent_team_execution_service = AgentTeamExecutionService(
    agent_team_service,
    threads=thread_service,
    turns=turn_service,
)
app.state.agent_team_execution_service = agent_team_execution_service
app.include_router(
    build_agent_team_execution_router(agent_team_execution_service)
)

automation_execution_service = AutomationExecutionService(
    automation_run_service,
    identity=identity_service,
    threads=thread_service,
    turns=turn_service,
    teams=agent_team_execution_service,
    events=canonical_event_store,
    work_items=work_item_service,
    action_intents=action_intent_service,
    action_providers=action_provider_registry,
)
app.state.automation_execution_service = automation_execution_service
app.include_router(
    build_automation_execution_router(automation_execution_service)
)

async def _launch_admitted_automation(run):
    await automation_execution_service.launch(
        run.id,
        organization_id=run.organization_id,
        workspace_id=run.workspace_id,
    )

automation_trigger_admission_bridge.set_launch_handler(
    _launch_admitted_automation
)

# Preserve the small historical direct-import surface through dynamic
# compatibility proxies. Production routers continue to use thread_service and
# turn_service above and do not depend on the legacy host.
core._default_thread_message_limit = thread_service.default_message_limit
core._coerce_thread_message_limit = thread_service.coerce_message_limit
core._trim_thread_messages = thread_service.trim_messages
install_thread_compatibility_facade(
    app,
    core,
    canonical_settings=thread_execution_settings_service,
)

async def _resume_provider_capacity_wait(wait):
    if wait.thread_id:
        turn_execution_service.schedule_queue_drain(wait.thread_id)
        return

    if await work_item_service.resume_provider_capacity_wait(
        wait,
        turn_execution_service,
    ):
        return

    recovery = getattr(app.state, "native_recovery_service", None)
    if recovery is not None:
        recovery.schedule(reason="provider-capacity-resumed")


provider_capacity_service.register_resume_handler(
    _resume_provider_capacity_wait
)

# Bot provider runtime is composed from explicit domain owners. Compatibility
# compatibility aliases are output-only and are not read by these
# services after construction.
slack_client = SlackClient()
telegram_client = TelegramClient()
app.state.slack_client = slack_client
app.state.telegram_client = telegram_client
bot_webhook_security_service = install_bot_webhook_security_service(
    app,
    core,
    connections=bot_connection_service,
    secret_broker=secret_broker,
)
bot_channel_discovery_service = install_bot_channel_discovery_service(
    app,
    core,
    connections=bot_connection_service,
    bindings=bot_binding_selection_service,
    projects=project_runtime_service,
    slack_client=slack_client,
    secret_broker=secret_broker,
)
project_ui_state_service = ProjectUiStateService(
    projects=project_service,
    resources=resource_catalog_service,
    threads=thread_service,
    bindings=bot_binding_selection_service,
    settings=thread_execution_settings_service,
    channels=bot_channel_discovery_service,
    execution_profiles=execution_profile_definition_service,
    binding_public=_binding_public,
    readiness=lambda project_id, actor: (
        project_readiness_service.evaluate(
            project_id,
            actor=actor,
            record=False,
        ).model_dump(mode="json")
    ),
)
app.state.project_ui_state_service = project_ui_state_service
agent_channel_preference_service = (
    install_agent_channel_preference_service(
        app,
        core,
        load_settings=configuration_state.agent_channel_presence.load,
        normalize_strings=normalize_string_list,
        known_channels=bot_channel_discovery_service.known,
        clone_binding=bot_binding_lifecycle_service.clone_to_conversation,
        load_bindings=bot_binding_repository.load,
        binding_prefix=bot_presentation_service.binding_prefix,
        same_logical_binding=thread_recovery_service.same_logical_binding,
    )
)

bot_event_dispatch_service = BotEventDispatchService(
    projects=project_runtime_service,
    settings=thread_execution_settings_service,
    targets=bot_target_service,
    bindings=bot_binding_lifecycle_service,
    execution=turn_execution_service,
    queue_policy=turn_queue_policy,
    recovery=thread_recovery_service,
    resume=thread_resume_service,
    telemetry=bot_runtime_telemetry,
    publish_event=event_hub.publish,
    binding_name=thread_recovery_service.logical_binding_name,
)
bot_event_dispatch_compatibility = (
    BotEventDispatchCompatibilityFacade(core)
)
app.state.bot_event_dispatch_compatibility = (
    bot_event_dispatch_compatibility
)

canonical_work_item_continuity_service = WorkItemContinuityService(
    policy=runtime_policy,
    get_state=work_item_state_machine._work_item_state,
    coerce_owner=work_item_state_machine._coerce_owner,
    binding_for_agent=agent_channel_preference_service.binding_for_agent,
    replace_nonperforming_thread=(
        bot_event_dispatch_service.replace_nonperforming_thread
    ),
    dispatch_event=bot_event_dispatch_service.dispatch,
    dispatch_text=work_item_contract_service.dispatch_text,
    append_event=bot_runtime_telemetry.append,
    truncate_text=lambda value, limit: str(value)[:limit],
    thread_is_active=turn_execution_service.thread_is_active,
    thread_queue_depth=turn_queue_policy.depth,
    thread_recently_active=bot_event_dispatch_service.thread_recently_active,
    watchdog_dispatch_allowed=watchdog_dispatch_policy.allowed,
    record_watchdog_dispatch=watchdog_dispatch_policy.record,
    coordination_channel=HANDOFF_COORDINATION_CHANNEL,
)
work_item_continuity_service.bind(
    canonical_work_item_continuity_service
)
app.state.canonical_work_item_continuity_service = (
    canonical_work_item_continuity_service
)

# Historical direct callers resolve through this explicit compatibility edge.
core._binding_for_agent = agent_channel_preference_service.binding_for_agent
core._replace_nonperforming_thread_if_needed = (
    bot_event_dispatch_service.replace_nonperforming_thread
)
core._dispatch_event_to_binding = (
    bot_event_dispatch_compatibility.dispatch
)
core._work_item_dispatch_text = work_item_contract_service.dispatch_text
core._work_item_execution_contract = (
    work_item_contract_service.contract_for_state
)
core._thread_recently_active = bot_event_dispatch_service.thread_recently_active
core._watchdog_dispatch_allowed = watchdog_dispatch_policy.allowed
core._record_watchdog_dispatch = watchdog_dispatch_policy.record
core.HANDOFF_COORDINATION_CHANNEL = HANDOFF_COORDINATION_CHANNEL
work_item_continuity_compatibility = (
    build_work_item_continuity_compatibility_service(core)
)
app.state.work_item_continuity_compatibility = (
    work_item_continuity_compatibility
)
core._schedule_structured_handoff_dispatch = (
    work_item_continuity_compatibility.schedule_structured_handoff_dispatch
)
core._schedule_handoff_continuity_check = (
    work_item_continuity_compatibility.schedule_handoff_continuity_check
)
core._schedule_actionable_owner_dispatch = (
    work_item_continuity_compatibility.schedule_actionable_owner_dispatch
)
core._schedule_actionable_owner_continuity_check = (
    work_item_continuity_compatibility.schedule_actionable_owner_continuity_check
)
core._dispatch_structured_handoff_to_recipient = (
    work_item_continuity_compatibility.dispatch_structured_handoff
)
core._run_handoff_continuity_check = (
    work_item_continuity_compatibility.run_handoff_continuity_check
)
core._actionable_owner_dispatch_stage = (
    work_item_continuity_compatibility.actionable_owner_stage
)
core._dispatch_actionable_owner_to_responsible_thread = (
    work_item_continuity_compatibility.dispatch_actionable_owner
)
core._run_actionable_owner_continuity_check = (
    work_item_continuity_compatibility.run_actionable_owner_continuity_check
)
work_item_timing_policy = install_work_item_timing_policy(
    app,
    core,
    coerce_owner=work_item_state_machine._coerce_owner,
)
work_item_watchdog_candidate_policy = (
    install_work_item_watchdog_candidate_policy(
        app,
        core,
        load_states=runtime_state.work_item_states.load,
        split_brain_findings=(
            work_item_state_machine._work_item_split_brain_findings
        ),
        handoff_timeout_seconds=(
            work_item_timing_policy.handoff_timeout_seconds
        ),
        coerce_owner=work_item_state_machine._coerce_owner,
        sla_threshold_seconds=(
            work_item_timing_policy.sla_threshold_seconds
        ),
    )
)
work_item_watchdog_prompt_policy = install_work_item_watchdog_prompt_policy(
    app,
    core,
    project_lookup=project_runtime_service.get,
)
async def _autonomy_gitlab_group_issues(
    project_id,
    project_settings,
    *,
    labels=None,
    state="opened",
):
    token = gitlab_work_item_dependencies.token_for_project(project_id)
    group = gitlab_work_item_dependencies.group_path(project_settings)
    if not token or not group:
        return []
    return await gitlab_client.group_issues(
        gitlab_work_item_dependencies.api_base_url,
        group,
        token=token,
        labels=labels,
        state=state,
    )


autonomy_runtime_dependencies = AutonomyRuntimeDependencies(
    load_gitlab_routing_settings=configuration_state.gitlab_routing.load,
    load_work_item_states=runtime_state.work_item_states.load,
    save_work_item_states=runtime_state.work_item_states.save,
    gitlab_token_for_project=(
        gitlab_work_item_dependencies.token_for_project
    ),
    gitlab_group_path=gitlab_work_item_dependencies.group_path,
    gitlab_group_issues=_autonomy_gitlab_group_issues,
    append_bot_event=bot_runtime_telemetry.append,
    append_work_item_event=work_item_state_machine._append_work_item_event,
    work_item_event=work_item_state_machine._work_item_event,
    archive_active_handoff=(
        work_item_state_machine._archive_active_handoff
    ),
    coerce_owner=work_item_state_machine._coerce_owner,
    owner_queue_agents=OWNER_QUEUE_AGENTS,
    handoff_coordination_channel=HANDOFF_COORDINATION_CHANNEL,
    binding_for_agent=agent_channel_preference_service.binding_for_agent,
    orchestrator_binding=bot_binding_selection_service.orchestrator,
    binding_prefix=bot_presentation_service.binding_prefix,
    replace_nonperforming_thread=(
        bot_event_dispatch_service.replace_nonperforming_thread
    ),
    dispatch_event=bot_event_dispatch_service.dispatch,
    release_stale_active_turn=(
        thread_recovery_service.release_stale_active_turn
    ),
    thread_is_active=turn_execution_service.thread_is_active,
    thread_queue_depth=turn_queue_policy.depth,
    thread_recently_active=bot_event_dispatch_service.thread_recently_active,
    watchdog_dispatch_allowed=watchdog_dispatch_policy.allowed,
    record_watchdog_dispatch=watchdog_dispatch_policy.record,
    handoff_timeout_seconds=work_item_timing_policy.handoff_timeout_seconds,
    release_validation_sla_seconds=(
        work_item_timing_policy.release_validation_sla_seconds
    ),
    work_item_sla_threshold_seconds=(
        work_item_timing_policy.sla_threshold_seconds
    ),
    owner_activity_timestamp=(
        work_item_timing_policy.owner_activity_timestamp
    ),
    orchestrator_watchdog_candidates=(
        work_item_watchdog_candidate_policy.orchestrator_candidates
    ),
    split_brain_watchdog_candidates=(
        work_item_watchdog_candidate_policy.split_brain_candidates
    ),
    format_orchestrator_watchdog_prompt=(
        work_item_watchdog_prompt_policy.format_orchestrator_prompt
    ),
    format_split_brain_watchdog_prompt=(
        work_item_watchdog_prompt_policy.format_split_brain_prompt
    ),
    work_item_dispatch_text=work_item_dispatch_prompt_policy.render,
)
app.state.autonomy_runtime_dependencies = autonomy_runtime_dependencies

autonomy_service = install_autonomy_service(
    app,
    core,
    action_execution_service,
    action_intent_service,
    controller=autonomy_controller,
    canonical_events=canonical_event_ingestion,
    runtime=autonomy_runtime_dependencies,
)
native_recovery_service = NativeRecoveryService(
    policy=runtime_policy,
    cycles=(
        autonomy_service.run_owner_work_cycle,
        autonomy_service.run_release_gate_cycle,
        autonomy_service.run_work_item_sla_cycle,
        autonomy_service.run_orchestrator_cycle,
    ),
    append_event=bot_runtime_telemetry.append,
)
app.state.native_recovery_service = native_recovery_service
work_item_recovery_scheduler.bind(native_recovery_service)
core._schedule_native_recovery_cycles = native_recovery_service.schedule
work_item_wakeup_queue_policy = install_work_item_wakeup_queue_policy(app, core)

workflow_claim_policy = WorkflowClaimPolicy(
    load_states=runtime_state.work_item_states.load,
    ensure_defaults=work_item_state_machine._ensure_work_item_lane_defaults,
    coerce_owner=work_item_state_machine._coerce_owner,
    owner_names=OWNER_QUEUE_AGENTS,
)
app.state.workflow_claim_policy = workflow_claim_policy

bot_delivery_service = install_bot_delivery_service(
    app,
    core,
    connections=bot_connection_service,
    bindings=bot_binding_selection_service,
    targets=bot_target_service,
    presentation=bot_presentation_service,
    details=bot_detail_service,
    telemetry=bot_runtime_telemetry,
    collaboration=thread_bot_collaboration_service,
    approvals=approval_service,
    publish_event=event_hub.publish,
    workflow_claim_findings=workflow_claim_policy.findings,
    workflow_correction=workflow_claim_policy.correction,
    slack_client=slack_client,
    telegram_client=telegram_client,
)
bot_routing_service = install_bot_routing_service(
    app,
    core,
    bot_delivery_service,
    connections=bot_connection_service,
    bindings=bot_binding_selection_service,
    binding_lifecycle=bot_binding_lifecycle_service,
    targets=bot_target_service,
    presentation=bot_presentation_service,
    telemetry=bot_runtime_telemetry,
    projects=project_runtime_service,
    settings=thread_execution_settings_service,
    recovery=thread_recovery_service,
    resume=thread_resume_service,
    queue_policy=turn_queue_policy,
    execution=turn_execution_service,
    publish_event=event_hub.publish,
)

gitlab_routing_dependencies = GitLabRoutingDependencies(
    load_settings=configuration_state.gitlab_routing.load,
    normalize_strings=normalize_string_list,
    binding_for_agent=agent_channel_preference_service.binding_for_agent,
    preferred_agent_conversations=agent_channel_preference_service.conversations,
    clone_binding_to_known_channel=(
        agent_channel_preference_service.clone_to_known_channel
    ),
    master_binding=bot_binding_selection_service.master,
)
gitlab_work_item_runtime_dependencies = (
    GitLabWorkItemRuntimeDependencies(
        split_brain_findings=(
            work_item_state_machine._work_item_split_brain_findings
        ),
        coerce_owner=work_item_state_machine._coerce_owner,
        project_event=work_item_service.project_gitlab_event_compat,
        project_lookup=project_runtime_service.get,
        load_projects=project_repository.load,
    )
)
gitlab_event_presentation = GitLabEventPresentationService(
    delivery=bot_delivery_service,
    targets=bot_target_service,
    telemetry=bot_runtime_telemetry,
    label_names=gitlab_work_item_dependencies.label_names,
    event_url=gitlab_work_item_dependencies.url,
)
app.state.gitlab_event_presentation = gitlab_event_presentation


def _verify_gitlab_webhook(request):
    verify_gitlab_token(
        request,
        os.environ.get("CODEX_WEB_GITLAB_WEBHOOK_SECRET")
        or os.environ.get("GITLAB_WEBHOOK_SECRET"),
    )


gitlab_operational_dependencies = GitLabOperationalDependencies(
    api_base_url=gitlab_work_item_dependencies.api_base_url,
    load_support_state=auxiliary_state.support_servicedesk.load,
    save_support_state=auxiliary_state.support_servicedesk.save,
    load_semantic_events=auxiliary_state.gitlab_semantic_events.load,
    save_semantic_events=auxiliary_state.gitlab_semantic_events.save,
    verify_webhook=_verify_gitlab_webhook,
    append_event=bot_runtime_telemetry.append,
    publish_event=event_hub.publish,
    truncate_text=lambda value, limit: str(value)[:limit],
    dispatch_event=bot_event_dispatch_service.dispatch,
    format_event_prompt=gitlab_event_presentation.format_prompt,
    send_event_notice=gitlab_event_presentation.send_notice,
    schedule_recovery=native_recovery_service.schedule,
)
gitlab_service = install_gitlab_service(
    app,
    None,
    gitlab_client,
    canonical_events=canonical_event_ingestion,
    autonomy_controller=autonomy_controller,
    routing=gitlab_routing_dependencies,
    work_items=gitlab_work_item_runtime_dependencies,
    operations=gitlab_operational_dependencies,
)
install_gitlab_compatibility(core, gitlab_service)
bot_runtime = install_bot_runtime(
    app,
    core,
    connections=bot_connection_service,
    bindings=bot_binding_selection_service,
    presentation=bot_presentation_service,
    telemetry=bot_runtime_telemetry,
    routing=bot_routing_service,
    delivery=bot_delivery_service,
    publish_event=event_hub.publish,
    slack_client=slack_client,
    telegram_client=telegram_client,
    ownership=replicated_ownership_service,
)

assert bot_state.reply_targets is not None
assert bot_state.delivery_targets is not None
operational_compaction_service = OperationalCompactionService(
    state_store=state_store,
    delivery_targets=bot_state.delivery_targets,
    reply_targets=bot_state.reply_targets,
    turn_queues=turn_queue_repository,
    load_bindings=bot_binding_repository.all,
    event_journal=BOTS_EVENTS_FILE,
    backup_directory=DATA_DIR / "compaction-backups",
    event_journal_status=bot_runtime_telemetry.journal_status,
)
app.state.operational_compaction_service = operational_compaction_service
project_bootstrap_service.operational_state_inspection = (
    operational_compaction_service.inspect_bounded
)
app.include_router(
    build_operational_compaction_router(
        operational_compaction_service,
        bot_runtime=bot_runtime,
    )
)

conversation_channel_store = ConversationChannelStore(state_store)
conversation_channel_registry = ConversationChannelRegistry()
conversation_channel_registry.register_provider(
    "slack",
    SlackConversationChannel,
)
conversation_channel_registry.register_provider(
    "telegram",
    TelegramConversationChannel,
)
conversation_channel_registry.register_provider(
    "teams",
    TeamsConversationChannel,
)
conversation_channel_service = ConversationChannelService(
    conversation_channel_store,
    conversation_channel_registry,
    canonical_event_ingestion,
    bot_routing_service.route_normalized,
)
bot_routing_service.conversation_channels = conversation_channel_service
bot_runtime.conversation_channels = conversation_channel_service
app.state.conversation_channel_store = conversation_channel_store
app.state.conversation_channel_registry = conversation_channel_registry
app.state.conversation_channel_service = conversation_channel_service
app.include_router(
    build_conversation_channels_router(conversation_channel_service)
)

slack_provider_service = install_slack_provider_service(
    app,
    core,
    slack_client=slack_client,
    routing_service=bot_routing_service,
    connections=bot_connection_service,
    bindings=bot_binding_selection_service,
    targets=bot_target_service,
    presentation=bot_presentation_service,
    telemetry=bot_runtime_telemetry,
    webhook_security=bot_webhook_security_service,
    reconciliation_gates=reconciliation_gate_service,
)
bot_service = BotService(
    connections=bot_connection_service,
    bindings=bot_binding_selection_service,
    binding_lifecycle=bot_binding_lifecycle_service,
    channels=bot_channel_discovery_service,
    presentation=bot_presentation_service,
    telemetry=bot_runtime_telemetry,
    runtime=bot_runtime,
    routing_service=bot_routing_service,
    load_gitlab_routing_settings=configuration_state.gitlab_routing.load,
)
app.state.bot_service = bot_service

active_turn_recovery_store = ActiveTurnRecoveryStore(state_store)
stale_active_turn_recovery_service = StaleActiveTurnRecoveryService(
    active_turns=runtime_state.active_turns,
    turn_queues=turn_queue_repository,
    worker_state_loader=execution_worker_store.load,
    store=active_turn_recovery_store,
    schedule_queue_drain=turn_execution_service.schedule_queue_drain,
    append_event=bot_runtime_telemetry.append,
    resume_active_threads=(
        turn_execution_service.resume_active_threads_after_startup
    ),
    backup_directory=DATA_DIR / "active-turn-recovery-backups",
)
thread_recovery_service.stale_active_turn_reconciler = (
    stale_active_turn_recovery_service.schedule_thread
)
app.state.active_turn_recovery_store = active_turn_recovery_store
app.state.stale_active_turn_recovery_service = (
    stale_active_turn_recovery_service
)
app.include_router(
    build_stale_active_turn_router(
        stale_active_turn_recovery_service
    )
)

static_asset_version_service = StaticAssetVersionService(
    STATIC_DIR,
    DATA_DIR.parent,
)
def _execution_readiness_health():
    result = execution_worker_service.execution_readiness(
        required_capabilities=(
            WorkerCapability.GIT,
            WorkerCapability.COMMAND_EXECUTION,
        ),
        execution_contract_version="thread-turn/1.0",
        actor=identity_service.local_trusted_actor(),
    )
    payload = result.model_dump(mode="json")
    isolation = local_execution_backend.probe()
    payload["local_isolation"] = {
        "backend": isolation.backend,
        "ready": isolation.ready,
        "reason": isolation.reason,
        "capabilities": [
            capability.value for capability in isolation.capabilities
        ],
        "container_runtime": isolation.container_runtime,
        "container_profile": isolation.container_profile,
        "remediation": isolation.remediation,
    }
    return payload


runtime_health_service = RuntimeHealthService(
    codex=codex_runtime,
    bot_runtime=bot_runtime,
    telemetry=bot_runtime_telemetry,
    load_bindings=bot_binding_repository.load,
    terminal_failures=turn_execution_service.terminal_failures,
    terminal_recovery_tasks=turn_execution_service.terminal_recovery_tasks,
    terminal_failure_window_seconds=(
        turn_execution_service.terminal_failure_window_seconds
    ),
    load_queues=turn_queue_repository.load,
    slack_provider_health=slack_provider_service.health,
    gitlab_sync_status=gitlab_sync_health.snapshot,
    execution_readiness=_execution_readiness_health,
    count_active_turns=runtime_state.active_turns.count,
    state_store_status=state_store.status,
    event_sink=bot_runtime_telemetry.append,
)
app.state.static_asset_version_service = static_asset_version_service
app.state.runtime_health_service = runtime_health_service

# Replace the legacy core startup/shutdown callbacks after all runtime and
# provider services have been composed. The supervisor coordinates explicit
# runtime owners and receives the extracted health evaluator directly.
def _compact_turn_queues() -> None:
    queues = turn_queue_repository.load()
    compacted = {
        thread_id: items
        for thread_id, items in queues.items()
        if items
    }
    if len(compacted) != len(queues):
        turn_queue_repository.save(compacted)


def _flush_compatibility_state() -> None:
    repositories = (
        runtime_state.thread_settings,
        runtime_state.active_turns,
        runtime_state.work_item_states,
        turn_queue_repository,
        bot_state.reply_targets,
        bot_state.delivery_targets,
    )
    for repository in repositories:
        if repository is not None:
            repository.flush_legacy_mirror()


core._flush_compatibility_state = _flush_compatibility_state

runtime_supervisor = install_runtime_supervisor(
    app,
    core,
    policy=runtime_policy,
    autonomy=autonomy_service,
    gitlab=gitlab_service,
    native_recovery=native_recovery_service,
    continuity=work_item_continuity_service,
    codex=codex_runtime,
    bot_runtime=bot_runtime,
    runtime_health=runtime_health_service,
    stale_turn_recovery=stale_active_turn_recovery_service,
    event_sink=bot_runtime_telemetry.append,
    truncate_text=lambda value, limit: str(value)[:limit],
    sd_notify=sd_notify,
    daemon_health=runtime_health_service.health,
    load_projects=project_repository.load,
    compact_turn_queues=_compact_turn_queues,
    dedupe_bot_integrations=bot_connection_service.dedupe_integrations,
    restore_thread_names=thread_naming_service.restore_all,
    resume_active_threads=turn_execution_service.resume_active_threads_after_startup,
    load_turn_queues=turn_queue_repository.load,
    thread_is_active=turn_execution_service.thread_is_active,
    release_stale_active_turn=thread_recovery_service.release_stale_active_turn,
    schedule_queue_drain=turn_execution_service.schedule_queue_drain,
    flush_compatibility_state=_flush_compatibility_state,
)

def _compatibility_state_metrics() -> dict[str, object]:
    repositories = (
        runtime_state.thread_settings,
        runtime_state.active_turns,
        runtime_state.work_item_states,
        turn_queue_repository,
        bot_state.reply_targets,
        bot_state.delivery_targets,
    )
    return {
        repository.namespace: repository.compatibility_metrics()
        for repository in repositories
        if repository is not None
    }


runtime_service = RuntimeService(
    codex=codex_runtime,
    static_version=static_asset_version_service.version,
    runtime_health=runtime_health_service.health,
    load_active_turns=runtime_state.active_turns.load,
    load_turn_queues=turn_queue_repository.load,
    active_turn_stale_seconds=(
        thread_recovery_service.active_turn_stale_seconds
    ),
    resume_active_threads_after_startup=(
        turn_execution_service.resume_active_threads_after_startup
    ),
    schedule_queue_drain=turn_execution_service.schedule_queue_drain,
    load_work_item_states=runtime_state.work_item_states.load,
    work_item_split_brain_findings=(
        work_item_state_machine._work_item_split_brain_findings
    ),
    recent_events=bot_runtime_telemetry.recent,
    supervisor_status=runtime_supervisor.task_status,
    event_sink=bot_runtime_telemetry.append,
    state_store=state_store,
    coordination_backend=coordination_backend,
    replicated_ownership=replicated_ownership_service,
    canonical_event_bus=canonical_event_bus,
    event_transport_runtime=event_transport_runtime,
    event_transport=event_transport,
    deployment_mode=deployment_mode,
    instance_id=instance_id,
    compatibility_state_metrics=_compatibility_state_metrics,
    task_source_writeback_status=(
        work_item_service.task_source_writeback.status
    ),
    native_recovery_status=native_recovery_service.status,
    continuity_background_status=(
        canonical_work_item_continuity_service.status
    ),
)
app.state.runtime_service = runtime_service
core.healthz = runtime_service.healthz
core.recovery_resume = runtime_service.recovery_resume

def _diagnostic_queued_turn_public(queued):
    preview = queued.message.replace("\n", " ")
    if len(preview) > 180:
        preview = f"{preview[:180]}..."
    return {
        "id": queued.id,
        "threadId": queued.thread_id,
        "projectId": queued.project_id,
        "source": queued.source,
        "attempts": queued.attempts,
        "createdAt": queued.created_at,
        "messagePreview": preview,
        "replyTarget": (
            queued.reply_target.model_dump()
            if queued.reply_target
            else None
        ),
    }


def _diagnostic_binding_public(binding):
    item = binding.model_dump()
    item["prefix"] = bot_presentation_service.binding_prefix(binding)
    item["report_name"] = bot_presentation_service.binding_report_name(
        binding
    )
    item["active"] = turn_execution_service.thread_is_active(
        binding.thread_id
    )
    item["queueDepth"] = turn_queue_policy.depth(binding.thread_id)
    if binding.provider == "slack":
        item["slack_icon"] = bot_presentation_service.slack_reply_icon(
            binding
        )
        item["slack_username"] = (
            bot_presentation_service.slack_reply_username(binding)
        )
    return item


runtime_diagnostics_service = RuntimeDiagnosticsService(
    version=static_asset_version_service.version,
    health=runtime_health_service.health,
    codex=codex_runtime,
    bot_runtime=bot_runtime,
    telemetry=bot_runtime_telemetry,
    runtime_policy=runtime_policy,
    supervisor=runtime_supervisor,
    thread_message_limit=thread_service.default_message_limit,
    slack_provider_health=slack_provider_service.health,
    project_lookup=project_runtime_service.get,
    load_projects=project_repository.load,
    load_thread_index=thread_index_repository.load,
    load_active_turns=runtime_state.active_turns.load,
    load_queues=turn_queue_repository.load,
    queued_turn_public=_diagnostic_queued_turn_public,
    queue_tasks=turn_execution_service.queue_drain_tasks,
    load_connections=bot_state.connections.load,
    connection_public=bot_connection_service.public,
    load_bindings=bot_binding_repository.load,
    binding_public=_diagnostic_binding_public,
    load_agent_presence=configuration_state.agent_channel_presence.load,
    agent_presence_public=lambda settings: settings.model_dump(),
    load_reply_targets=bot_state.reply_targets.load,
    load_delivery_targets=bot_state.delivery_targets.load,
    load_work_item_states=runtime_state.work_item_states.load,
    count_reply_targets=bot_state.reply_targets.count,
    count_delivery_targets=bot_state.delivery_targets.count,
    page_reply_targets_raw=bot_state.reply_targets.raw_page,
    page_delivery_targets_raw=bot_state.delivery_targets.raw_page,
    work_item_public=work_item_state_machine._work_item_state_public,
    recent_events=bot_runtime_telemetry.recent,
    bot_routing_metrics=bot_target_service.metrics,
    bot_binding_index_status=bot_binding_repository.index_status,
)
operator_ui_service = OperatorUiService(
    static_dir=STATIC_DIR,
    version=static_asset_version_service.version,
    health=runtime_health_service.health,
    load_turn_queues=turn_queue_repository.load,
    load_active_turns=runtime_state.active_turns.load,
    load_work_item_states=runtime_state.work_item_states.load,
    event_hub=event_hub,
)
home_overview_service = HomeOverviewService(
    projects=project_service,
    work_items=work_item_service,
    attention=attention_service,
    approvals=approval_request_service,
    incidents=incident_service,
    agent_sessions=agent_session_service,
    goals=goal_service,
    schedules=scheduler_service,
)
app.state.home_overview_service = home_overview_service
app.state.runtime_diagnostics_service = runtime_diagnostics_service
app.state.operator_ui_service = operator_ui_service

# Output-only compatibility aliases. Implementations live in extracted
# services; the compatibility namespace serves historical direct callers.
core._static_version = static_asset_version_service.version

def _compat_daemon_health():
    compatibility_health = RuntimeHealthService(
        codex=getattr(core, "codex", codex_runtime),
        bot_runtime=getattr(core, "bot_runtime", bot_runtime),
        telemetry=bot_runtime_telemetry,
        load_bindings=bot_binding_repository.load,
        terminal_failures=getattr(
            core,
            "THREAD_TERMINAL_FAILURES",
            turn_execution_service.terminal_failures,
        ),
        terminal_recovery_tasks=getattr(
            core,
            "TERMINAL_RECOVERY_TASKS",
            turn_execution_service.terminal_recovery_tasks,
        ),
        terminal_failure_window_seconds=(
            turn_execution_service.terminal_failure_window_seconds
        ),
        load_queues=turn_queue_repository.load,
        slack_provider_health=slack_provider_service.health,
        gitlab_sync_status=gitlab_sync_health.snapshot,
    )
    # Historical tests may replace the public runtime-status dictionary.
    compatibility_health.telemetry.status = getattr(
        core,
        "BOT_RUNTIME_STATUS",
        bot_runtime_telemetry.status,
    )
    # Legacy devhealth callers expect an immediate point-in-time
    # evaluation. This compatibility path is explicitly diagnostic and is not
    # used by the lightweight HTTP health/status endpoints.
    return compatibility_health.refresh_sync()


async def _compat_healthz():
    health = core._daemon_health()
    if not health["ok"]:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail=health)
    return health


core._daemon_health = _compat_daemon_health
core.healthz = _compat_healthz
core._diagnostic_snapshot = runtime_diagnostics_service.snapshot
core._preview_bot_route = bot_routing_service.preview
core._devhealth_work_item_stats = operator_ui_service.work_item_stats
core._recent_bot_events = bot_runtime_telemetry.recent
core._thread_recent_activity_age_seconds = (
    bot_runtime_telemetry.thread_recent_activity_age_seconds
)
core._thread_recent_event_count = (
    bot_runtime_telemetry.thread_recent_event_count
)

# Verified historical operator-UI compatibility. These wrappers deliberately
# resolve names from the compatibility namespace at call time so direct tests
# can patch them without changing the canonical UI/router implementation.
core.build_devstatus_context = build_devstatus_context
core.render_devstatus_html = render_devstatus_html
core.build_devhealth_context = build_devhealth_context
core.render_devhealth_html = render_devhealth_html


async def _compat_devstatus():
    return HTMLResponse(
        core.render_devstatus_html(
            core.build_devstatus_context()
        )
    )


async def _compat_devhealth(request):
    force_refresh = request.query_params.get("refresh") in {
        "1",
        "true",
        "yes",
    }
    queues = core._load_turn_queues()
    context = core.build_devhealth_context(
        core._daemon_health(),
        active_turns=len(core._load_active_turns()),
        queued_turns=sum(len(items) for items in queues.values()),
        status_context=core.build_devstatus_context(
            force_refresh=force_refresh
        ),
        work_item_stats=core._devhealth_work_item_stats(),
        refresh_url="/devhealth?refresh=1",
    )
    return HTMLResponse(core.render_devhealth_html(context))


core.devstatus = _compat_devstatus
core.devhealth = _compat_devhealth

install_webhook_security(core, secret_broker)
previous_context_service = getattr(app.state, "context_compaction_service", None)
if previous_context_service is not None:
    event_hub.unsubscribe(previous_context_service.observe)
event_hub.subscribe(context_service.observe)
app.state.context_compaction_service = context_service

def _include_domain_router(router) -> int:
    route_count = len(router.routes)
    app.include_router(router)
    return route_count


work_item_run_service = WorkItemRunProjectionService(
    execution_worker_store,
    runtime_usage=agent_runtime_usage_store,
    artifact_evidence=artifact_evidence_store,
    action_intents=action_intent_store,
    approvals=approval_request_store,
    attention=attention_store,
    execution_workspaces=execution_workspace_state_store,
)
app.state.work_item_run_service = work_item_run_service


EXTRACTED_ROUTE_COUNTS = {
    "definitions": _include_domain_router(
        build_definitions_router(
            definition_registry_service,
            project_service,
        )
    ),
    "configuration": _include_domain_router(
        build_configuration_router(
            configuration_service,
            project_service,
            resource_catalog_service,
        )
    ),
    "projects": _include_domain_router(
        build_projects_router(
            project_service,
            fresh_bootstrap=fresh_project_bootstrap_service,
        )
    ),
    "project-ui": _include_domain_router(
        build_project_ui_state_router(project_ui_state_service)
    ),
    "threads": _include_domain_router(
        build_threads_router(thread_service)
    ),
    "turns": _include_domain_router(
        build_turns_router(turn_service)
    ),
    "context": _include_domain_router(
        build_context_router(context_service)
    ),
    "runtime": _include_domain_router(
        build_runtime_router(runtime_service)
    ),
    "approvals": _include_domain_router(
        build_approvals_router(approval_service)
    ),
    "bots": _include_domain_router(
        build_bots_router(bot_service)
    ),
    "slack": _include_domain_router(
        build_slack_router(slack_provider_service)
    ),
    "telegram": _include_domain_router(
        build_telegram_router(
            bot_routing_service,
            connections=bot_connection_service,
            webhook_security=bot_webhook_security_service,
            conversation_channels=conversation_channel_service,
        )
    ),
    "work-items": _include_domain_router(
        build_work_items_router(
            work_item_service,
            gitlab_sync_jobs=gitlab_sync_job_service,
            agent_teams=agent_team_service,
            runs=work_item_run_service,
        )
    ),
    "ui": _include_domain_router(
        build_ui_router(operator_ui_service)
    ),
    "home": _include_domain_router(
        build_home_router(home_overview_service)
    ),
    "system": _include_domain_router(
        build_system_router(
            static_asset_version_service,
            runtime_health_service,
            runtime_diagnostics_service,
            bot_routing_service,
        )
    ),
    "integrations": _include_domain_router(
        build_integrations_router(
            None,
            gitlab_service,
            load_agent_presence=(
                configuration_state.agent_channel_presence.load
            ),
            save_agent_presence=(
                configuration_state.agent_channel_presence.save
            ),
            load_gitlab_routing=(
                configuration_state.gitlab_routing.load
            ),
            save_gitlab_routing=(
                configuration_state.gitlab_routing.save
            ),
            event_sink=bot_runtime_telemetry.append,
            publish_event=event_hub.publish,
            truncate_text=lambda value, limit: str(value)[:limit],
        )
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
    run_server()
