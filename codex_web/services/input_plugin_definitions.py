from __future__ import annotations

from collections.abc import Callable
from typing import Any

from codex_web.definitions import (
    DefinitionContext,
    DefinitionDraftCreate,
    DefinitionReference,
    reference_for,
)
from codex_web.identity import AuthenticationActor
from codex_web.input_plugin_definitions import (
    INPUT_PIPELINE_DEFINITION_ID,
    INPUT_PIPELINE_DEFINITION_KIND,
    INPUT_PIPELINE_SCHEMA_VERSION,
    InputPipelineDefinition,
    input_pipeline_seed_payload,
    validate_input_pipeline_definition,
)
from codex_web.input_plugins import (
    GatedValidator,
    InputPluginPipeline,
    InputPluginRegistration,
    NormalizeWhitespaceInputPlugin,
)
from codex_web.model_gateway import ModelInvocationRequest
from codex_web.services.definitions import (
    DefinitionKindSchema,
    DefinitionRegistryService,
)


class InputPluginDefinitionError(RuntimeError):
    pass


class InputPluginCatalog:
    """Code-owned plugin implementations keyed by exact identity/version."""

    def __init__(self) -> None:
        self._plugins: dict[tuple[str, str], Any] = {}

    def register(self, plugin: Any) -> None:
        plugin_id = str(getattr(plugin, "id", "") or "").strip()
        version = str(getattr(plugin, "version", "") or "").strip()
        transform = getattr(plugin, "transform", None)
        transport = str(getattr(plugin, "transport", "") or "").strip()
        if not plugin_id or not version or not transport or not callable(transform):
            raise InputPluginDefinitionError(
                "input plugin implementation requires id, version, transport and transform"
            )
        key = (plugin_id, version)
        existing = self._plugins.get(key)
        if existing is not None and existing is not plugin:
            raise InputPluginDefinitionError(
                f"input plugin implementation already registered: {plugin_id}@{version}"
            )
        self._plugins[key] = plugin

    def get(self, plugin_id: str, version: str) -> Any:
        try:
            return self._plugins[(plugin_id, version)]
        except KeyError as exc:
            raise InputPluginDefinitionError(
                f"input plugin implementation unavailable: {plugin_id}@{version}"
            ) from exc

    def metadata(self) -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "plugin_id": plugin_id,
                "plugin_version": version,
                "transport": str(plugin.transport),
            }
            for (plugin_id, version), plugin in sorted(self._plugins.items())
        )


class InputPipelineDefinitionService:
    """Resolve scoped pipeline definitions into code-owned plugin registrations."""

    def __init__(
        self,
        registry: DefinitionRegistryService,
        catalog: InputPluginCatalog,
        *,
        gated_validator: GatedValidator | None = None,
    ) -> None:
        self.registry = registry
        self.catalog = catalog
        self.gated_validator = gated_validator

    def bootstrap(self) -> None:
        self.registry.bootstrap(
            [
                DefinitionDraftCreate(
                    definition_id=INPUT_PIPELINE_DEFINITION_ID,
                    kind=INPUT_PIPELINE_DEFINITION_KIND,
                    definition_schema_version=INPUT_PIPELINE_SCHEMA_VERSION,
                    payload=input_pipeline_seed_payload(),
                    actor="bootstrap",
                    reason=(
                        "bootstrap empty input pipeline; operators explicitly "
                        "publish scoped plugin registrations"
                    ),
                )
            ]
        )

    def record(
        self,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ):
        return self.registry.resolve(
            definition_id=INPUT_PIPELINE_DEFINITION_ID,
            kind=INPUT_PIPELINE_DEFINITION_KIND,
            context=DefinitionContext(
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
            ),
        )

    def definition(
        self,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> InputPipelineDefinition:
        return InputPipelineDefinition.model_validate(
            self.record(
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
            ).payload
        )

    def reference(
        self,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> DefinitionReference:
        return reference_for(
            self.record(
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
            )
        )

    def pipeline_for(
        self,
        request: ModelInvocationRequest,
        actor: AuthenticationActor,
    ) -> InputPluginPipeline:
        record = self.record(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        definition = InputPipelineDefinition.model_validate(record.payload)
        definition_ref = reference_for(record)
        registrations: list[InputPluginRegistration] = []
        for item in definition.registrations:
            if not item.enabled:
                continue
            if not item.conditions.matches(
                purpose=request.purpose,
                model_class=request.model_class,
            ):
                continue
            plugin = self.catalog.get(item.plugin_id, item.plugin_version)
            registrations.append(
                InputPluginRegistration(
                    plugin=plugin,
                    phase=item.phase,
                    order=item.order,
                    failure_policy=item.failure_policy,
                    max_patch_bytes=item.max_patch_bytes,
                    max_added_characters=item.max_added_characters,
                    definition_ref=definition_ref,
                    settings=dict(item.settings),
                )
            )
        return InputPluginPipeline(
            registrations,
            gated_validator=self.gated_validator,
        )


def default_input_plugin_catalog(
    *,
    skill_root: str | None = None,
) -> InputPluginCatalog:
    catalog = InputPluginCatalog()
    catalog.register(NormalizeWhitespaceInputPlugin())
    if skill_root is not None:
        from codex_web.input_plugin_skills import register_skill_plugins_from_root

        register_skill_plugins_from_root(catalog, skill_root)
    return catalog


def install_input_plugin_definitions(
    registry: DefinitionRegistryService,
    *,
    catalog: InputPluginCatalog | None = None,
    gated_validator: GatedValidator | None = None,
) -> InputPipelineDefinitionService:
    if not any(
        item["kind"] == INPUT_PIPELINE_DEFINITION_KIND
        and item["schema_version"] == INPUT_PIPELINE_SCHEMA_VERSION
        for item in registry.schemas.metadata()
    ):
        registry.register_schema(
            DefinitionKindSchema(
                kind=INPUT_PIPELINE_DEFINITION_KIND,
                schema_version=INPUT_PIPELINE_SCHEMA_VERSION,
                validate=validate_input_pipeline_definition,
            )
        )
    service = InputPipelineDefinitionService(
        registry,
        catalog or default_input_plugin_catalog(),
        gated_validator=gated_validator,
    )
    service.bootstrap()
    return service
