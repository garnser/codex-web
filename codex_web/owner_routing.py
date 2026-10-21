"""Interpret coordination owner references in the existing execution-role catalog.

This is an immutable read snapshot, not an identity alias or an execution grant.
Historical Work Item values and authenticated principals remain unchanged.
"""
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from codex_web.definitions import DefinitionReference, DefinitionScope
from codex_web.execution_role_models import ExecutionRoleCatalogDefinition


class OwnerRoutingError(ValueError):
    pass


class OwnerRoutingBinding(BaseModel):
    """Original recipient intent/provenance, never an identity or grant."""
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    schema_version: Literal['1.0', '1.1'] = '1.0'
    organization_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    configured_owner: str = Field(min_length=1, max_length=128)
    canonical_owner: str = Field(min_length=1, max_length=128, pattern=r'^[a-z0-9][a-z0-9-]*$')
    definition: DefinitionReference

    @model_validator(mode='after')
    def valid_reference(self):
        if (self.definition.kind != 'execution-role-catalog'
                or self.definition.definition_id != 'execution-roles.default'
                or self.definition.definition_schema_version != '1.0'
                or normalized_owner(self.configured_owner) != self.configured_owner
                or (self.schema_version == '1.0' and self.configured_owner == self.canonical_owner)):
            raise ValueError('invalid coordination owner binding')
        return self

    def for_work_item(self, state):
        if (state.organization_id, state.workspace_id, state.project_id) != (
                self.organization_id, self.workspace_id, self.project_id):
            raise OwnerRoutingError('owner_routing_scope_mismatch')
        return self

    def audit_metadata(self) -> dict[str, str]:
        return {'routing_schema_version': self.schema_version,
            'configured_owner': self.configured_owner, 'canonical_owner': self.canonical_owner,
            'organization_id': self.organization_id, 'workspace_id': self.workspace_id,
            'project_id': self.project_id, 'definition_id': self.definition.definition_id,
            'definition_kind': self.definition.kind, 'definition_record_id': self.definition.record_id,
            'definition_revision': str(self.definition.revision), 'definition_checksum': self.definition.checksum,
            'definition_schema_version': self.definition.definition_schema_version}


class OwnerRoutingRecipient(BaseModel):
    """Current canonical execution custody, separate from conversation provenance."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    thread_id: str = Field(min_length=1, max_length=256)
    bootstrap_id: str = Field(min_length=1, max_length=256)
    assignment_id: str = Field(min_length=1, max_length=256)
    execution_id: str = Field(min_length=1, max_length=256)
    worker_id: str = Field(min_length=1, max_length=256)
    fence: int = Field(ge=1)
    authority_checksum: str = Field(pattern=r'^[a-f0-9]{64}$')
    work_item_owner_checksum: str = Field(pattern=r'^[a-f0-9]{64}$')


class OwnerRoutingPin(BaseModel):
    """Server-produced recipient intent retained with the original queued turn.

    Actor, exact AgentProfile and resource scope stay in QueuedTurn's existing
    fields. This pin adds no credentials, permissions or native outcome claims.
    """
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal['1.0', '1.1'] = '1.0'
    binding: OwnerRoutingBinding
    work_item_ref: str = Field(min_length=1, max_length=500)
    stage: str = Field(min_length=1, max_length=128)
    retry_attempt: int = Field(ge=0)
    handoff_requested_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    bot_binding_id: str = Field(min_length=1, max_length=256)
    original_thread_id: str = Field(min_length=1, max_length=256)

    recipient: OwnerRoutingRecipient | None = None

    @model_validator(mode='after')
    def versioned_recipient(self):
        if (self.schema_version == '1.1') != (self.recipient is not None):
            raise ValueError('recipient witness requires explicit owner-routing 1.1')
        if self.schema_version == '1.0' and self.binding.schema_version != '1.0':
            raise ValueError('legacy pin requires legacy binding')
        return self

    @model_serializer(mode='wrap')
    def preserve_legacy_shape(self, handler):
        result = handler(self)
        if self.schema_version == '1.0':
            result.pop('recipient', None)
        return result


class OwnerRoutingBlocker(BaseModel):
    """One durable configuration denial on the existing canonical Work Item."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal['1.0'] = '1.0'
    code: str = Field(min_length=1, max_length=128, pattern=r'^[a-z0-9_]+$')
    configured_owner: str | None = Field(default=None, max_length=128)
    definition: DefinitionReference | None = None
    recorded_at: float = Field(ge=0, allow_inf_nan=False)


def normalized_owner(value: str | None) -> str | None:
    result = (value or '').strip().lower()
    return None if result in {'', 'none', 'null'} else result


@dataclass(frozen=True)
class OwnerRoutingSnapshot:
    organization_id: str
    workspace_id: str
    project_id: str
    definition: DefinitionReference
    aliases: tuple[tuple[str, str], ...]
    rejected: tuple[tuple[str, str], ...]
    coordination_owners: tuple[str, ...]
    implementation_owners: tuple[str, ...]
    known_owners: tuple[str, ...]

    def for_work_item(self, state):
        if (state.organization_id, state.workspace_id, state.project_id) != (
                self.organization_id, self.workspace_id, self.project_id):
            raise OwnerRoutingError('owner_routing_scope_mismatch')
        claimed = {normalized_owner(getattr(state, name, None)) for name in
                   ('implementation_owner', 'validation_owner', 'release_owner')}
        if claimed.intersection(dict(self.aliases)):
            raise OwnerRoutingError('owner_alias_cannot_change_claimed_lane')
        return self

    def owner(self, value: str | None) -> str | None:
        configured = normalized_owner(value)
        if configured in dict(self.rejected):
            raise OwnerRoutingError(dict(self.rejected)[configured])
        return dict(self.aliases).get(configured, configured)

    def observation(self, value: str | None) -> dict:
        configured = normalized_owner(value)
        reason = dict(self.rejected).get(configured)
        canonical = None if reason else self.owner(configured)
        status = ('blocked' if reason else 'unavailable' if configured is None else
                  'aliased' if configured != canonical else 'canonical' if configured in self.known_owners else 'unmapped')
        return {'schema_version': '1.0', 'configured_owner': configured, 'canonical_owner': canonical,
            'alias_applied': configured != canonical and reason is None,
            'status': status, 'reason': reason,
            'organization_id': self.organization_id, 'workspace_id': self.workspace_id,
            'project_id': self.project_id, 'definition': self.definition.model_dump(mode='json'),
            'grants_execution_authority': False}

    def audit_metadata(self, value: str | None) -> dict[str, str]:
        """Use the existing WorkItemEvent flat string metadata contract."""
        return {'routing_schema_version': '1.0', 'configured_owner': normalized_owner(value) or '', 'canonical_owner': self.owner(value) or '',
            'organization_id': self.organization_id, 'workspace_id': self.workspace_id, 'project_id': self.project_id,
            'definition_id': self.definition.definition_id, 'definition_kind': self.definition.kind,
            'definition_record_id': self.definition.record_id, 'definition_revision': str(self.definition.revision),
            'definition_checksum': self.definition.checksum,
            'definition_schema_version': self.definition.definition_schema_version}

    def binding(self, value):
        observation = self.observation(value)
        if observation['status'] == 'blocked':
            raise OwnerRoutingError(observation['reason'])
        if not observation['alias_applied']:
            return None
        return OwnerRoutingBinding(organization_id=self.organization_id, workspace_id=self.workspace_id,
            project_id=self.project_id, configured_owner=observation['configured_owner'],
            canonical_owner=observation['canonical_owner'], definition=self.definition)


def owner_routing_snapshot(record, *, organization_id: str, workspace_id: str, project_id: str):
    """Only a direct Project mapping to a genuine active coordination role aliases.

    Implementation/validation/release owner mappings retain their existing role
    selection meaning. They never rename an owner or implementation claim.
    """
    from codex_web.definitions import reference_for
    catalog = ExecutionRoleCatalogDefinition.model_validate(record.payload)
    if len(catalog.owner_to_execution_role) > 1000:
        raise OwnerRoutingError('owner_mapping_limit')
    mappings = {}
    for raw, target in catalog.owner_to_execution_role.items():
        source = normalized_owner(raw)
        if source is None or len(source) > 128:
            raise OwnerRoutingError('invalid_owner_mapping')
        mappings.setdefault(source, set()).add(target)
    aliases, rejected = {}, {}
    roles = catalog.role_map
    canonical_names = {name for role in roles for name in (role, role.replace('-', ' '))}
    for source, targets in mappings.items():
        if len(targets) != 1:
            rejected[source] = 'ambiguous_owner_mapping'
            continue
        target = next(iter(targets))
        role = roles[target]
        if role.lane != 'coordination' or source == target:
            continue
        if source in canonical_names:
            rejected[source] = 'canonical_owner_remap'
        elif record.scope_type != DefinitionScope.PROJECT or record.scope_id != project_id:
            rejected[source] = 'owner_alias_requires_project_definition'
        elif role.lifecycle != 'active':
            rejected[source] = 'coordination_owner_inactive'
        elif mappings.get(target) != {target}:
            # No transitive alias, role swap or cycle may select a recipient.
            rejected[source] = 'coordination_alias_target_remapped'
        else:
            aliases[source] = target
    return OwnerRoutingSnapshot(organization_id=organization_id, workspace_id=workspace_id,
        project_id=project_id, definition=reference_for(record), aliases=tuple(sorted(aliases.items())),
        rejected=tuple(sorted(rejected.items())), coordination_owners=tuple(sorted(
            role.id for role in catalog.roles if role.lane == 'coordination')),
        implementation_owners=tuple(sorted({name for role in catalog.roles
            if role.lifecycle == 'active' and role.lane not in {'coordination', 'validation', 'release'}
            for name in (role.id, role.id.replace('-', ' '))})), known_owners=tuple(sorted(canonical_names)))
