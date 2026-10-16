"""Interpret coordination owner references in the existing execution-role catalog.

This is an immutable read snapshot, not an identity alias or an execution grant.
Historical Work Item values and authenticated principals remain unchanged.
"""
from dataclasses import dataclass

from codex_web.definitions import DefinitionReference, DefinitionScope
from codex_web.execution_role_models import ExecutionRoleCatalogDefinition


class OwnerRoutingError(ValueError):
    pass


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
        return {'configured_owner': configured, 'canonical_owner': canonical,
            'alias_applied': configured != canonical and reason is None,
            'status': 'blocked' if reason else 'unavailable' if configured is None else 'resolved', 'reason': reason,
            'organization_id': self.organization_id, 'workspace_id': self.workspace_id,
            'project_id': self.project_id, 'definition': self.definition.model_dump(mode='json'),
            'grants_execution_authority': False}


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
            rejected[source] = 'coordination_owner_disabled'
        elif mappings.get(target) != {target}:
            # No transitive alias, role swap or cycle may select a recipient.
            rejected[source] = 'coordination_alias_target_remapped'
        else:
            aliases[source] = target
    return OwnerRoutingSnapshot(organization_id=organization_id, workspace_id=workspace_id,
        project_id=project_id, definition=reference_for(record), aliases=tuple(sorted(aliases.items())),
        rejected=tuple(sorted(rejected.items())))
