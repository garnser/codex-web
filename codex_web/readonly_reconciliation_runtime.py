"""Explicit one-shot READY recovery composition; never import the application.

This module does not start a server, bootstrap definitions/identities, run an
agent, claim an action, or replace an installed release. Its caller owns the
operator lock, authenticated credential issuance/revocation and audit journal.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from codex_web.action_intents import ActionIntentReconcileRequest
from codex_web.services.code_host_action_contract import CODE_HOST_PULL_REQUEST_READY_ACTION_ID
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.execution_workers import ExecutionWorkerStore


@dataclass(frozen=True)
class ReconciliationTarget:
    intent: object
    assignment: object
    worker: object


def validate_readonly_reconciliation_target(
    store, *, intent_id: str, expected_assignment_id: str, expected_fence: int,
) -> ReconciliationTarget:
    """Read-only pre-credential guard; all current authority is checked again.

    Work-item advancement and procedural execution require the running server's
    full execution host. They are deliberately outside this emergency tool.
    """
    collections = (ActionIntentStore.records_namespace, ExecutionWorkerStore.worker_namespace,
        ExecutionWorkerStore.assignment_namespace, ExecutionWorkerStore.event_namespace,
        ExecutionWorkerStore.enrollment_namespace)
    if (not all(store.record_collections_exist(collections).values())
            or store.record_get(ActionIntentStore.records_namespace, ActionIntentStore._meta_key)
                not in ({'schema_version':'1.3'}, {'schema_version':'1.4'})
            or store.get(ExecutionWorkerStore.namespace) is not None):
        raise ValueError('canonical normalized records are required; migrate through the running service')
    state = ActionIntentStore(store).load(intent_id=intent_id)
    intent = next((item for item in state.intents if item.id == intent_id), None)
    if (intent is None or intent.action_id != CODE_HOST_PULL_REQUEST_READY_ACTION_ID
            or intent.status not in {'uncertain', 'requires_reconciliation'}
            or intent.attempt < 1 or intent.execution_started_at is None
            or intent.work_item_ref is not None or intent.work_item_success is not None
            or intent.expected_evidence
            or intent.readiness_origin is None or intent.rollback_checkpoint is not None):
        raise ValueError('unsupported read-only reconciliation target')
    origin = intent.readiness_origin
    workers = ExecutionWorkerStore(store)
    assignment = workers.assignment(expected_assignment_id)
    now = time.time()
    if (origin.assignment_id != expected_assignment_id or origin.fence != expected_fence
            or assignment is None or assignment.fence != expected_fence
            or assignment.execution_id != intent.execution_id
            or assignment.assigned_worker_id != origin.worker_id
            or assignment.execution_profile_definition != origin.execution_profile_definition
            or assignment.created_by != intent.requested_by
            or (assignment.organization_id, assignment.workspace_id, assignment.project_id)
                != (intent.organization_id, intent.workspace_id, intent.project_id)
            or assignment.status not in {'claimed', 'running'}
            or assignment.procedure_checkpoint is not None
            or assignment.lease is None or assignment.lease.expires_at <= now
            or assignment.lease.fence != expected_fence
            or assignment.lease.worker_id != assignment.assigned_worker_id
            or assignment.deadline_at is not None and assignment.deadline_at <= now):
        raise ValueError('originating read-only reconciliation custody is unavailable')
    worker = workers.worker(origin.worker_id)
    if (worker is None or worker.revoked_at is not None or worker.lifecycle not in {'active', 'draining'}
            or (worker.organization_id, worker.workspace_id)
                != (intent.organization_id, intent.workspace_id)):
        raise ValueError('originating worker is unavailable')
    if not any(receipt.intent_id == intent.id and receipt.result is not None
               and receipt.result.status == 'succeeded' for receipt in state.receipts):
        raise ValueError('completed provider result is required for this recovery tool')
    return ReconciliationTarget(intent, assignment, worker)


class ReadOnlyGitLabTransport(httpx.AsyncBaseTransport):
    """Defense in depth: this process cannot send a provider mutation."""

    def __init__(self, api_base: str, *, transport=None):
        self.base = httpx.URL(api_base)
        self.inner = transport if transport is not None else httpx.AsyncHTTPTransport()
        self.requests = 0

    async def handle_async_request(self, request):
        if (request.method not in {'GET', 'HEAD'} or self.requests >= 16
                or (request.url.scheme, request.url.host, request.url.port)
                    != (self.base.scheme, self.base.host, self.base.port)
                or not request.url.path.startswith(self.base.path.rstrip('/') + '/')):
            raise RuntimeError('read-only provider request refused')
        self.requests += 1
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        # GitLabClient owns short-lived AsyncClients sharing this transport.
        # The enclosing one-shot runtime, rather than each GET, owns its pool.
        pass

    async def close_owned(self):
        await self.inner.aclose()


class _NoExecutionTransport:
    """Real adapter metadata is inspectable; native execution is unavailable."""

    def __getattr__(self, name):
        raise RuntimeError('native execution is unavailable in read-only recovery')


class _NoWorkItemHost:
    def __getattr__(self, name):
        raise RuntimeError('work-item operations require the running execution host')


@dataclass
class ReadOnlyReconciliationRuntime:
    intents: object
    identity: object
    worker_actor: object
    target: ReconciliationTarget
    transport: ReadOnlyGitLabTransport
    authenticate: object = field(repr=False)
    used: bool = field(default=False, init=False)

    async def reconcile_once(self):
        if self.used:
            raise ValueError('read-only recovery runtime has already been used')
        self.used = True
        try:
            actor = self.authenticate()
            target = self.target
            if (actor.identity_id != target.worker.service_identity_id
                    or (actor.organization_id, actor.workspace_id)
                        != (target.intent.organization_id, target.intent.workspace_id)):
                raise ValueError('authenticated worker changed before reconciliation')
            self.intents._require_worker(actor)
            validate_readonly_reconciliation_target(self.intents.store.store,
                intent_id=target.intent.id, expected_assignment_id=target.assignment.id,
                expected_fence=target.assignment.fence)
            return await self.intents.reconcile(target.intent.id,
                ActionIntentReconcileRequest(retry_if_idempotent=False), actor=actor)
        finally:
            await self.transport.close_owned()


def build_readonly_reconciliation_runtime(
    state_store, *, intent_id: str, expected_assignment_id: str, expected_fence: int,
    worker_token: str, secret_material_dir: Path, projects_file: Path,
    gitlab_api_base: str, gitlab_provider_instance: str, gitlab_web_base: str | None = None,
):
    """Compose canonical services without bootstrap, migration or background work."""
    target = validate_readonly_reconciliation_target(state_store,
        intent_id=intent_id, expected_assignment_id=expected_assignment_id,
        expected_fence=expected_fence)
    # Imports remain local so the pre-credential guard has a small dependency
    # boundary and importing this helper never composes a live application.
    from codex_web.services.identity import IdentityService
    from codex_web.storage.identity_state import IdentityStateStore
    identity = IdentityService(IdentityStateStore(state_store))
    actor = identity.authenticate_service_token(worker_token, touch=False)
    if (actor.identity_id != target.worker.service_identity_id
            or (actor.organization_id, actor.workspace_id)
                != (target.intent.organization_id, target.intent.workspace_id)):
        raise ValueError('authenticated worker differs from originating custody')
    from codex_web.services.action_intents import ActionIntentService
    from codex_web.services.action_providers import ActionExecutionService, ActionProviderRegistry
    from codex_web.services.agent_profiles import AgentProfileService
    from codex_web.services.agent_providers import AgentProviderService
    from codex_web.services.agent_runtime import AgentRuntimeRegistry
    from codex_web.services.approval_requests import ApprovalRequestService
    from codex_web.services.artifact_evidence import ArtifactEvidenceService
    from codex_web.services.attention import AttentionService
    from codex_web.services.authority_roles import AuthorityRoleService
    from codex_web.services.capacity import CapacityService
    from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
    from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
    from codex_web.services.data_governance import DataGovernanceService
    from codex_web.services.definitions import DefinitionKindSchema, DefinitionRegistryService
    from codex_web.services.entitlements import EntitlementService
    from codex_web.services.execution_profile_definitions import ExecutionProfileDefinitionService
    from codex_web.services.execution_workers import ExecutionWorkerService
    from codex_web.services.gitlab_action_provider import GitLabActionProvider
    from codex_web.services.organizational_memory import OrganizationalMemoryService
    from codex_web.services.procedure_discovery import (
        ProcedureDiscoveryService, ProcedureExecutionCapabilityService, ProcedureSupportingContextService)
    from codex_web.services.procedure_execution import ProcedureExecutionService
    from codex_web.services.projects import ProjectService
    from codex_web.services.resources import ResourceCatalogService
    from codex_web.services.secrets import SecretBroker
    from codex_web.services.security_boundary import SecurityBoundaryService
    from codex_web.services.skill_scanners import SkillScannerRegistry
    from codex_web.services.skill_security import SkillSecurityService
    from codex_web.services.skills import SkillService
    from codex_web.storage.action_providers import ActionProviderStateStore
    from codex_web.storage.agent_profiles import AgentProfileStore
    from codex_web.storage.agent_providers import AgentProviderStore
    from codex_web.storage.approval_requests import ApprovalRequestStore
    from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
    from codex_web.storage.attention import AttentionStore
    from codex_web.storage.canonical_events import CanonicalEventStore
    from codex_web.storage.capacity import CapacityStore
    from codex_web.storage.data_governance import DataGovernanceStore
    from codex_web.storage.definition_registry import DefinitionRegistryStore
    from codex_web.storage.entitlements import EntitlementStore
    from codex_web.storage.extensions import ExtensionStateStore
    from codex_web.storage.model_gateway import ModelGatewayStore
    from codex_web.storage.organizational_memory import OrganizationalMemoryStore
    from codex_web.storage.projects import ProjectRepository
    from codex_web.storage.resource_catalog import ResourceCatalogStore
    from codex_web.storage.secret_state import SecretStateStore
    from codex_web.storage.security_events import SecurityEventStore
    from codex_web.storage.skill_catalog import SkillSourceStore
    from codex_web.storage.skill_security import SkillSecurityStore
    from codex_web.secret_backends import LocalFileSecretBackend
    from codex_web.integrations.gitlab_client import GitLabClient
    from codex_web.agent_runtime import AgentRuntimeAccountingMode
    from codex_web.authority import (
        AUTHORITY_ROLE_CATALOG_KIND, AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
        validate_authority_role_catalog)
    from codex_web.services.authority_publication import assess_authority_catalog_publication
    from codex_web.execution_profiles import (
        EXECUTION_PROFILE_CATALOG_KIND, EXECUTION_PROFILE_CATALOG_SCHEMA_VERSION,
        ADMINISTRATIVE_EXECUTION_PROFILE_SCHEMA_VERSION, COORDINATION_EXECUTION_PROFILE_SCHEMA_VERSION,
        validate_execution_profile_catalog, validate_administrative_execution_profile_catalog,
        validate_coordination_execution_profile_catalog)

    # These registrations describe engine schemas only. In particular, do not
    # call the ordinary install_* helpers: those also seed persisted catalogs.
    definitions = DefinitionRegistryService(DefinitionRegistryStore(state_store))
    definitions.register_schema(DefinitionKindSchema(kind=AUTHORITY_ROLE_CATALOG_KIND,
        schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
        validate=validate_authority_role_catalog, assess_publish=assess_authority_catalog_publication))
    for version, validator in (
            (EXECUTION_PROFILE_CATALOG_SCHEMA_VERSION, validate_execution_profile_catalog),
            (ADMINISTRATIVE_EXECUTION_PROFILE_SCHEMA_VERSION, validate_administrative_execution_profile_catalog),
            (COORDINATION_EXECUTION_PROFILE_SCHEMA_VERSION, validate_coordination_execution_profile_catalog)):
        definitions.register_schema(DefinitionKindSchema(kind=EXECUTION_PROFILE_CATALOG_KIND,
            schema_version=version, validate=validator))
    resources = ResourceCatalogService(ResourceCatalogStore(state_store))
    authority = AuthorityRoleService(definitions, resources)
    profiles = ExecutionProfileDefinitionService(definitions)
    skills = SkillService(definitions)
    security = SkillSecurityService(SkillSecurityStore(state_store), definitions, SkillScannerRegistry())
    security.source_store = SkillSourceStore(state_store)
    skills.bind_security(security)
    agent_profiles = AgentProfileService(AgentProfileStore(state_store), definitions=definitions,
        authority=authority, execution_profiles=profiles,
        skill_reference_validator=lambda ref, actor: skills.validate_reference(ref, actor=actor))
    profiles.agent_profiles = agent_profiles
    skills.bind_profiles(agent_profiles)
    workers = ExecutionWorkerService(ExecutionWorkerStore(state_store), identity=identity)
    governance = DataGovernanceService(DataGovernanceStore(state_store))
    memory = OrganizationalMemoryService(OrganizationalMemoryStore(state_store), governance, authority)
    host = _NoWorkItemHost()
    evidence = ArtifactEvidenceService(ArtifactEvidenceStore(state_store), resources=resources,
        work_item_host=host, governance=governance, organizational_memory=memory)
    memory.artifact_evidence = evidence
    capacity = CapacityService(CapacityStore(state_store), evidence=evidence)
    secret_material_dir = Path(secret_material_dir)
    if (secret_material_dir.is_symlink() or not secret_material_dir.is_dir()
            or secret_material_dir.stat().st_mode & 0o777 != 0o700):
        raise ValueError('an existing private secret backend is required')
    broker = SecretBroker(SecretStateStore(state_store),
        {'local': LocalFileSecretBackend(secret_material_dir)})
    registry = ActionProviderRegistry(ActionProviderStateStore(state_store))
    transport = ReadOnlyGitLabTransport(gitlab_api_base)
    registry.register(GitLabActionProvider(resources, client=GitLabClient(transport=transport),
        provider_instance=gitlab_provider_instance, api_base=gitlab_api_base, web_base=gitlab_web_base))
    execution = ActionExecutionService(registry, resources, secret_broker=broker)
    skills.bind_procedure_contracts(actions=registry, resources=resources,
        scope_actor=lambda record: identity.actor_for_identity(target.intent.requested_by, scope=actor.tenant))
    runtimes = AgentRuntimeRegistry()
    # Register the actual engine adapter and its production registration. Its
    # transport refuses execution; discovery reads static adapter metadata and
    # intersects it with current canonical provider/worker grants as usual.
    runtimes.register(CodexAgentRuntimeAdapter(_NoExecutionTransport()), capability_revision=1,
        sandbox_profiles=('read-only', 'workspace-write', 'danger-full-access'),
        network_profiles=('brokered-model-egress',), accounting_mode=AgentRuntimeAccountingMode.ALLOCATION)
    providers = AgentProviderService(AgentProviderStore(state_store),
        model_gateway=ModelGatewayStore(state_store), extensions=ExtensionStateStore(state_store))
    projects = ProjectService(ProjectRepository(Path(projects_file), store=state_store))
    from codex_web.services.configuration import ConfigurationService
    from codex_web.storage.configuration_registry import ConfigurationRegistryStore
    from codex_web.services.procedure_configuration import (
        PROCEDURE_CONTEXT_CHARACTERS, install_procedure_configuration,
    )
    configuration = ConfigurationService(ConfigurationRegistryStore(state_store))
    install_procedure_configuration(configuration)
    discovery = ProcedureDiscoveryService(skills, resources,
        context_capacity_resolver=lambda: configuration.resolve(PROCEDURE_CONTEXT_CHARACTERS.key),
        supporting_context=ProcedureSupportingContextService(memory), projects=projects,
        execution_capabilities=ProcedureExecutionCapabilityService(workers, runtimes, providers))
    events = CanonicalEventIngestionService(CanonicalEventBus(CanonicalEventStore(state_store)))
    approvals = ApprovalRequestService(ApprovalRequestStore(state_store), identity, events,
        authority_roles=authority)
    attention = AttentionService(AttentionStore(state_store), events, identity=identity)
    intents = ActionIntentService(ActionIntentStore(state_store), execution,
        artifact_evidence=evidence, work_item_host=host,
        security_boundary=SecurityBoundaryService(SecurityEventStore(state_store)),
        entitlements=EntitlementService(EntitlementStore(state_store)), authority=authority,
        identity=identity, capacity=capacity, execution_workers=workers,
        execution_profiles=profiles, procedure_discovery=discovery)
    intents.procedure_execution = ProcedureExecutionService(workers=workers, skills=skills,
        discovery=discovery, intents=intents, evidence=evidence, identity=identity,
        authority=authority, approvals=approvals, attention=attention,
        control_actor=actor, worker_actor=actor)
    intents._require_worker(actor)
    return ReadOnlyReconciliationRuntime(intents, identity, actor, target, transport,
        authenticate=lambda: identity.authenticate_service_token(worker_token, touch=False))
