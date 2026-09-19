from __future__ import annotations

from codex_web.agent_routing_definitions import (
    AGENT_ROUTING_POLICY_ID,
    AGENT_ROUTING_POLICY_KIND,
    AGENT_ROUTING_POLICY_SCHEMA_VERSION,
    AgentRoutingPolicyDefinition,
    AgentRoutingRoleDefault,
    validate_agent_routing_policy,
)
from codex_web.definitions import (
    DefinitionContext,
    DefinitionDraftCreate,
    DefinitionReference,
    reference_for,
)
from codex_web.services.definitions import (
    DefinitionKindSchema,
    DefinitionRegistryService,
)


class AgentRoutingDefinitionService:
    def __init__(self, registry: DefinitionRegistryService) -> None:
        self.registry = registry

    def bootstrap(self) -> None:
        self.registry.bootstrap(
            [
                DefinitionDraftCreate(
                    definition_id=AGENT_ROUTING_POLICY_ID,
                    kind=AGENT_ROUTING_POLICY_KIND,
                    definition_schema_version=AGENT_ROUTING_POLICY_SCHEMA_VERSION,
                    payload={"roles": []},
                    actor="bootstrap",
                    reason="bootstrap provider-neutral agent routing role defaults",
                )
            ]
        )

    def resolve(
        self,
        *,
        role_id: str | None,
        organization_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[AgentRoutingRoleDefault | None, DefinitionReference]:
        context = DefinitionContext(
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        record = self.registry.resolve(
            definition_id=AGENT_ROUTING_POLICY_ID,
            kind=AGENT_ROUTING_POLICY_KIND,
            context=context,
        )
        policy = AgentRoutingPolicyDefinition.model_validate(record.payload)
        selected = policy.role_map.get(role_id or "")
        return selected, reference_for(record)


def install_agent_routing_definitions(
    registry: DefinitionRegistryService,
) -> AgentRoutingDefinitionService:
    if not any(
        item["kind"] == AGENT_ROUTING_POLICY_KIND
        and item["schema_version"] == AGENT_ROUTING_POLICY_SCHEMA_VERSION
        for item in registry.schemas.metadata()
    ):
        registry.register_schema(
            DefinitionKindSchema(
                kind=AGENT_ROUTING_POLICY_KIND,
                schema_version=AGENT_ROUTING_POLICY_SCHEMA_VERSION,
                validate=validate_agent_routing_policy,
            )
        )
    service = AgentRoutingDefinitionService(registry)
    service.bootstrap()
    return service
