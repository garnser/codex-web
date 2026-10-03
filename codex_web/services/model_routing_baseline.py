from __future__ import annotations

import json
from importlib.resources import files

from codex_web.definitions import DefinitionContext, DefinitionDraftCreate, reference_for
from codex_web.model_routing_baseline import (
    MODEL_ROUTING_BASELINE_ID,
    MODEL_ROUTING_BASELINE_KIND,
    MODEL_ROUTING_BASELINE_SCHEMA_VERSION,
    ModelRoutingBaselineDefinition,
    validate_model_routing_baseline,
)
from codex_web.services.definitions import DefinitionKindSchema, DefinitionRegistryService


class ModelRoutingBaselineService:
    def __init__(self, registry: DefinitionRegistryService) -> None:
        self.registry = registry

    def bootstrap(self) -> None:
        payload = json.loads(
            files("codex_web").joinpath("model_routing_baseline_seed.json").read_text()
        )
        self.registry.bootstrap([
            DefinitionDraftCreate(
                definition_id=MODEL_ROUTING_BASELINE_ID,
                kind=MODEL_ROUTING_BASELINE_KIND,
                definition_schema_version=MODEL_ROUTING_BASELINE_SCHEMA_VERSION,
                payload=payload,
                actor="bootstrap",
                reason="bootstrap dated replaceable model-routing baseline",
            )
        ])

    def resolve(
        self,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ):
        record = self.registry.resolve(
            definition_id=MODEL_ROUTING_BASELINE_ID,
            kind=MODEL_ROUTING_BASELINE_KIND,
            context=DefinitionContext(
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
            ),
        )
        return ModelRoutingBaselineDefinition.model_validate(record.payload), reference_for(record)


def install_model_routing_baseline(
    registry: DefinitionRegistryService,
) -> ModelRoutingBaselineService:
    if not any(
        item["kind"] == MODEL_ROUTING_BASELINE_KIND
        and item["schema_version"] == MODEL_ROUTING_BASELINE_SCHEMA_VERSION
        for item in registry.schemas.metadata()
    ):
        registry.register_schema(DefinitionKindSchema(
            kind=MODEL_ROUTING_BASELINE_KIND,
            schema_version=MODEL_ROUTING_BASELINE_SCHEMA_VERSION,
            validate=validate_model_routing_baseline,
        ))
    service = ModelRoutingBaselineService(registry)
    service.bootstrap()
    return service
