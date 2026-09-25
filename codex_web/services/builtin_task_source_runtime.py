from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from codex_web.identity import AuthenticationActor, TenantScope
from codex_web.models import (
    JiraTaskSourceSettings,
    ServiceNowTaskSourceSettings,
    TaskSourceConfiguration,
    WorkItemState,
)
from codex_web.secrets import SecretStatus
from codex_web.services.identity import IdentityService
from codex_web.integrations.jira_client import JiraClient
from codex_web.integrations.servicenow_client import ServiceNowClient
from codex_web.integrations.github_client import GitHubClient
from codex_web.services.jira_task_source import JiraTaskSource
from codex_web.services.secrets import SecretBroker
from codex_web.services.servicenow_task_source import (
    ServiceNowFieldMapping,
    ServiceNowTaskSource,
)
from codex_web.services.github_task_source import GitHubTaskSource
from codex_web.services.task_source_runtime import (
    TaskSourceRegistry,
    TaskSourceResolutionError,
)
from codex_web.services.task_sources import (
    TaskSource,
    TaskSourceCanonicalProjection,
    TaskSourceCapabilities,
    TaskSourceCapability,
    TaskSourceCreateRequest,
    TaskSourceEvent,
    TaskSourcePage,
    TaskSourceReconciliationCursor,
    TaskSourceSnapshot,
)


TaskSourceBuilder = Callable[[str], TaskSource]


class SecretBoundTaskSource:
    """TaskSource facade that materializes credentials only per provider call.

    The facade retains canonical SecretReference identity, not credential
    material. A short-lived provider adapter is built inside SecretBroker.use*
    for each operation that can reach an external provider.
    """

    def __init__(
        self,
        *,
        source_type: str,
        source_instance: str,
        credential_secret_id: str,
        actor: AuthenticationActor,
        secret_broker: SecretBroker,
        builder: TaskSourceBuilder,
        projection_source: TaskSource,
    ) -> None:
        self.source_type = str(source_type).strip().casefold()
        self.source_instance = str(source_instance).strip().rstrip("/")
        self.credential_secret_id = str(credential_secret_id).strip()
        self._actor = actor
        self._secret_broker = secret_broker
        self._builder = builder
        self._projection_source = projection_source
        self.contract_version = getattr(projection_source, "contract_version", "1.0")
        self.capabilities = projection_source.capabilities
        if not isinstance(self.capabilities, TaskSourceCapabilities):
            raise TaskSourceResolutionError("resolved provider declared invalid capabilities")
        if projection_source.source_type.casefold() != self.source_type:
            raise TaskSourceResolutionError("provider source_type does not match binding")
        if projection_source.source_instance.rstrip("/") != self.source_instance:
            raise TaskSourceResolutionError("provider source_instance does not match binding")

    def _context(self, operation: str) -> dict[str, str]:
        return {
            "task_source_type": self.source_type,
            "task_source_instance": self.source_instance,
            "task_source_operation": operation,
        }

    async def _use(
        self,
        operation: str,
        consumer: Callable[[TaskSource], Awaitable[Any]],
    ) -> Any:
        async def invoke(secret: str) -> Any:
            source = self._builder(secret)
            if source.source_type.casefold() != self.source_type:
                raise TaskSourceResolutionError("provider source_type changed during secret use")
            if source.source_instance.rstrip("/") != self.source_instance:
                raise TaskSourceResolutionError("provider source_instance changed during secret use")
            return await consumer(source)

        return await self._secret_broker.use_async(
            self.credential_secret_id,
            actor=self._actor,
            operation=f"task-source:{operation}",
            consumer=lambda secret: invoke(secret),
            context=self._context(operation),
        )

    async def create(
        self,
        request: TaskSourceCreateRequest,
        *,
        scope: str,
    ) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.CREATE)
        return await self._use(
            "create",
            lambda source: source.create(request, scope=scope),
        )

    async def discover(self, *, scope: str) -> list[TaskSourceSnapshot]:
        self.capabilities.require(TaskSourceCapability.DISCOVERY)
        return await self._use(
            "discover",
            lambda source: source.discover(scope=scope),
        )

    async def discover_page(
        self,
        *,
        scope: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> TaskSourcePage:
        self.capabilities.require(TaskSourceCapability.PAGED_DISCOVERY)
        return await self._use(
            "discover_page",
            lambda source: source.discover_page(
                scope=scope,
                cursor=cursor,
                limit=limit,
            ),
        )

    async def reconcile_since(
        self,
        *,
        scope: str,
        cursor: TaskSourceReconciliationCursor | None = None,
        limit: int = 100,
    ) -> TaskSourcePage:
        self.capabilities.require(TaskSourceCapability.INCREMENTAL_RECONCILIATION)
        return await self._use(
            "reconcile_since",
            lambda source: source.reconcile_since(
                scope=scope,
                cursor=cursor,
                limit=limit,
            ),
        )

    async def read(self, identity) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.READ)
        return await self._use("read", lambda source: source.read(identity))

    async def normalize_event(self, payload: object) -> TaskSourceEvent | None:
        self.capabilities.require(TaskSourceCapability.EVENTS)
        # Event normalization is pure adapter logic and needs no credential.
        return await self._projection_source.normalize_event(payload)

    def project(
        self,
        snapshot: TaskSourceSnapshot,
        *,
        current_stage=None,
    ) -> TaskSourceCanonicalProjection:
        return self._projection_source.project(
            snapshot,
            current_stage=current_stage,
        )

    async def write_owner(self, identity, owner: str | None) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.OWNER_WRITE)
        return await self._use(
            "write_owner",
            lambda source: source.write_owner(identity, owner),
        )

    async def write_state(self, identity, state: str) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.STATE_WRITE)
        return await self._use(
            "write_state",
            lambda source: source.write_state(identity, state),
        )

    async def add_comment(self, identity, body: str) -> None:
        self.capabilities.require(TaskSourceCapability.COMMENTS)
        await self._use(
            "add_comment",
            lambda source: source.add_comment(identity, body),
        )

    async def attach_artifact(self, identity, url: str) -> None:
        self.capabilities.require(TaskSourceCapability.ARTIFACT_LINKS)
        await self._use(
            "attach_artifact",
            lambda source: source.attach_artifact(identity, url),
        )

    async def available_transitions(self, identity) -> tuple[str, ...]:
        self.capabilities.require(TaskSourceCapability.WORKFLOW_TRANSITIONS)
        return await self._use(
            "available_transitions",
            lambda source: source.available_transitions(identity),
        )

    async def transition(self, identity, transition: str) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.WORKFLOW_TRANSITIONS)
        return await self._use(
            "transition",
            lambda source: source.transition(identity, transition),
        )


class BuiltInTaskSourceRuntime:
    """Registers built-in enterprise TaskSources behind canonical boundaries."""

    service_identity_id = "service-task-source-runtime"

    def __init__(
        self,
        registry: TaskSourceRegistry,
        host: Any | None,
        identity: IdentityService,
        secrets: SecretBroker,
        *,
        load_projects: Callable[[], list[Any]] | None = None,
        jira_client: JiraClient | None = None,
        servicenow_client: ServiceNowClient | None = None,
        github_client: GitHubClient | None = None,
    ) -> None:
        if load_projects is None:
            if host is None:
                raise TypeError(
                    "BuiltInTaskSourceRuntime requires a project loader"
                )
            load_projects = host._load_projects
        self.registry = registry
        self.load_projects = load_projects
        self.identity = identity
        self.secrets = secrets
        self.jira_client = jira_client
        self.servicenow_client = servicenow_client
        self.github_client = github_client

    def _actor(self, scope: TenantScope) -> AuthenticationActor:
        return self.identity.bootstrap_service_actor(
            identity_id=self.service_identity_id,
            name="Task source runtime",
            scope=scope,
            service_scopes=("secret:use",),
        )

    def _validate_secret(
        self,
        configuration: TaskSourceConfiguration,
        actor: AuthenticationActor,
    ) -> None:
        secret_id = configuration.credential_secret_id
        if not secret_id:
            raise TaskSourceResolutionError(
                f"{configuration.source_type} task source has no credential reference"
            )
        try:
            reference = self.secrets.metadata(
                secret_id,
                actor=actor,
                require_use=True,
            )
        except Exception as exc:
            raise TaskSourceResolutionError(
                f"{configuration.source_type} task-source credential is unavailable"
            ) from exc
        if reference.status() != SecretStatus.ACTIVE:
            raise TaskSourceResolutionError(
                f"{configuration.source_type} task-source credential is {reference.status().value}"
            )

    def _jira(
        self,
        configuration: TaskSourceConfiguration,
        *,
        actor: AuthenticationActor,
    ) -> SecretBoundTaskSource:
        settings = configuration.provider_settings
        if not isinstance(settings, JiraTaskSourceSettings):
            raise TaskSourceResolutionError("Jira task source settings are invalid")
        self._validate_secret(configuration, actor)

        def builder(secret: str) -> TaskSource:
            return JiraTaskSource(
                configuration.source_instance,
                secret,
                username=settings.username,
                client=self.jira_client,
            )

        projection = JiraTaskSource(
            configuration.source_instance,
            "__credential_not_loaded__",
            username=settings.username,
            client=self.jira_client,
        )
        return SecretBoundTaskSource(
            source_type="jira",
            source_instance=configuration.source_instance,
            credential_secret_id=configuration.credential_secret_id or "",
            actor=actor,
            secret_broker=self.secrets,
            builder=builder,
            projection_source=projection,
        )

    def _servicenow(
        self,
        configuration: TaskSourceConfiguration,
        *,
        actor: AuthenticationActor,
    ) -> SecretBoundTaskSource:
        settings = configuration.provider_settings
        if not isinstance(settings, ServiceNowTaskSourceSettings):
            raise TaskSourceResolutionError("ServiceNow task source settings are invalid")
        self._validate_secret(configuration, actor)
        field_mapping = ServiceNowFieldMapping(**settings.fields.model_dump())

        def builder(secret: str) -> TaskSource:
            return ServiceNowTaskSource(
                configuration.source_instance,
                secret,
                table=settings.table,
                fields=field_mapping,
                canonical_state_values=settings.canonical_state_values,
                client=self.servicenow_client,
            )

        projection = ServiceNowTaskSource(
            configuration.source_instance,
            "__credential_not_loaded__",
            table=settings.table,
            fields=field_mapping,
            canonical_state_values=settings.canonical_state_values,
            client=self.servicenow_client,
        )
        return SecretBoundTaskSource(
            source_type="servicenow",
            source_instance=configuration.source_instance,
            credential_secret_id=configuration.credential_secret_id or "",
            actor=actor,
            secret_broker=self.secrets,
            builder=builder,
            projection_source=projection,
        )

    def _github(self, configuration: TaskSourceConfiguration, *, actor: AuthenticationActor) -> SecretBoundTaskSource:
        self._validate_secret(configuration, actor)
        def builder(secret: str) -> TaskSource:
            return GitHubTaskSource(configuration.source_instance, secret, client=self.github_client)
        projection = GitHubTaskSource(configuration.source_instance, "__credential_not_loaded__", client=self.github_client)
        return SecretBoundTaskSource(source_type="github", source_instance=configuration.source_instance,
            credential_secret_id=configuration.credential_secret_id or "", actor=actor,
            secret_broker=self.secrets, builder=builder, projection_source=projection)

    def source_for_configuration(
        self,
        configuration: TaskSourceConfiguration,
        *,
        scope: TenantScope,
    ) -> TaskSource | None:
        actor = self._actor(scope)
        source_type = configuration.source_type.casefold()
        if source_type == "jira":
            return self._jira(configuration, actor=actor)
        if source_type == "servicenow":
            return self._servicenow(configuration, actor=actor)
        if source_type == "github":
            return self._github(configuration, actor=actor)
        return None

    def _project(self, project_id: str, scope: TenantScope) -> Any:
        project = next(
            (
                item
                for item in self.load_projects()
                if getattr(item, "id", None) == project_id
                and getattr(item, "organization_id", None) == scope.organization_id
                and getattr(item, "workspace_id", None) == scope.workspace_id
            ),
            None,
        )
        if project is None:
            raise TaskSourceResolutionError("Project not found in task-source tenant")
        return project

    def source_for_project(
        self,
        configuration: TaskSourceConfiguration,
        project_id: str,
        scope: TenantScope,
    ) -> TaskSource | None:
        project = self._project(project_id, scope)
        if project.authoritative_task_source != configuration:
            raise TaskSourceResolutionError(
                "Project task-source binding changed during resolution"
            )
        return self.source_for_configuration(configuration, scope=scope)

    def source_for_state(self, state: WorkItemState) -> TaskSource | None:
        project_id = state.project_id
        if not project_id:
            return None
        scope = TenantScope(
            organization_id=state.organization_id,
            workspace_id=state.workspace_id,
        )
        project = self._project(project_id, scope)
        configuration = project.authoritative_task_source
        if configuration is None:
            return None
        identity = state.source_identity
        if identity is None:
            return None
        if identity.source_type.casefold() != configuration.source_type.casefold():
            raise TaskSourceResolutionError(
                "Work-item source type does not match project authoritative source"
            )
        if identity.source_instance.rstrip("/") != configuration.source_instance.rstrip("/"):
            raise TaskSourceResolutionError(
                "Work-item source instance does not match project authoritative source"
            )
        return self.source_for_configuration(configuration, scope=scope)

    def install(self) -> "BuiltInTaskSourceRuntime":
        for source_type in ("jira", "servicenow", "github"):
            self.registry.register(source_type, self.source_for_state)
            self.registry.register_project(source_type, self.source_for_project)
        return self


def install_builtin_task_source_runtime(
    registry: TaskSourceRegistry,
    host: Any | None,
    identity: IdentityService,
    secrets: SecretBroker,
    *,
    load_projects: Callable[[], list[Any]] | None = None,
    jira_client: JiraClient | None = None,
    servicenow_client: ServiceNowClient | None = None,
    github_client: GitHubClient | None = None,
) -> BuiltInTaskSourceRuntime:
    return BuiltInTaskSourceRuntime(
        registry,
        host,
        identity,
        secrets,
        load_projects=load_projects,
        jira_client=jira_client,
        servicenow_client=servicenow_client,
        github_client=github_client,
    ).install()
