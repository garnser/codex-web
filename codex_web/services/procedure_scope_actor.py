"""Canonical scope-only metadata principal for procedure reference checks."""
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.services.identity import IdentityService
from codex_web.services.projects import ProjectNotFoundError, ProjectService
from codex_web.storage.projects import ProjectRepository
from codex_web.workspaces import WorkspaceMapper


class ProcedureDefinitionScopeActor:
    """Resolve tenant metadata; this does not authenticate or grant service scopes."""

    def __init__(self, identity, projects):
        self.identity = identity
        self.projects = projects

    def can_observe_reads(self, identity) -> bool:
        return (type(self) is ProcedureDefinitionScopeActor
                and self.identity is identity
                and type(identity) is IdentityService
                and getattr(self.__call__, '__self__', None) is self
                and getattr(self.__call__, '__func__', None) is _CANONICAL_SCOPE_CALL
                and getattr(self.can_observe_reads, '__self__', None) is self
                and getattr(self.can_observe_reads, '__func__', None) is _CANONICAL_SCOPE_CHECK
                and _CANONICAL_IDENTITY_CHECK(identity)
                and type(self.projects) is ProjectService
                and all(getattr(getattr(self.projects, name, None), '__self__', None) is self.projects
                        and getattr(getattr(self.projects, name, None), '__func__', None) is method
                        for name, method in _CANONICAL_PROJECT_LOOKUPS.items())
                and type(self.projects.repository) is ProjectRepository
                and all(getattr(getattr(self.projects.repository, name, None), '__self__', None) is self.projects.repository
                        and getattr(getattr(self.projects.repository, name, None), '__func__', None) is method
                        for name, method in _CANONICAL_PROJECT_REPOSITORY_READS.items())
                and type(self.projects.repository.workspace_mapper) is WorkspaceMapper
                and all(getattr(getattr(self.projects.repository.workspace_mapper, name, None), '__self__', None)
                        is self.projects.repository.workspace_mapper
                        and getattr(getattr(self.projects.repository.workspace_mapper, name, None), '__func__', None) is method
                        for name, method in _CANONICAL_WORKSPACE_PATH_READS.items())
                and self.projects.repository.workspace_mapper._relative_to is _CANONICAL_WORKSPACE_RELATIVE)

    def __call__(self, record, *, _identity=None):
        if _identity is not None:
            if not _CANONICAL_SCOPE_CHECK(self, self.identity):
                raise ValueError('Canonical procedure scope metadata reader is unavailable')
            self.identity.require_read_observation(_identity)
        if record.scope_type.value == 'project':
            try:
                project = self.projects.get(record.scope_id)
            except ProjectNotFoundError:
                return None
            workspace_id = project.workspace_id
            organization_id = project.organization_id
        elif record.scope_type.value == 'workspace':
            workspace_id = record.scope_id
            organization_id = None
        else:
            return None
        state = self.identity.state(**({'_read_observation': _identity} if _identity is not None else {}))
        workspace = next((w for w in state.workspaces if w.id == workspace_id
                          and (organization_id is None or w.organization_id == organization_id)), None)
        if workspace is None:
            return None
        return AuthenticationActor(
            identity_id='procedure-contract-metadata', principal_kind=PrincipalKind.SERVICE,
            organization_id=workspace.organization_id, workspace_id=workspace.id,
            assurance=AuthenticationAssurance.SERVICE_TOKEN, service_scopes=(),
        )


_CANONICAL_SCOPE_CALL = ProcedureDefinitionScopeActor.__call__
_CANONICAL_SCOPE_CHECK = ProcedureDefinitionScopeActor.can_observe_reads
_CANONICAL_PROJECT_LOOKUPS = {name: getattr(ProjectService, name) for name in ('get', 'list')}
_CANONICAL_PROJECT_REPOSITORY_READS = {name: getattr(ProjectRepository, name) for name in (
    'load', '_legacy_or_default_payload', '_validate_portable_paths', '_runtime_project',
    '_storage_payload', '_write_payload')}
_CANONICAL_WORKSPACE_PATH_READS = {name: getattr(WorkspaceMapper, name) for name in (
    'runtime_path', 'storage_path', 'default_project_path', '_under_runtime_root')}
_CANONICAL_WORKSPACE_RELATIVE = WorkspaceMapper._relative_to

_CANONICAL_IDENTITY_CHECK = IdentityService.can_observe_reads
