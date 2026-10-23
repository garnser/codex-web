"""Opt-in, assignment-bound host metadata over the existing broker.

The native worker stays isolated. This adapter accepts no paths, commands or
credential material, and creates no principal, grant, assignment or scheduler.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys

from codex_web.authority import AuthorityLevel
from codex_web.control_plane_broker import ControlPlaneBrokerOperation
from codex_web.definitions import DefinitionScope, reference_for
from codex_web.execution_profiles import ExecutionProfileCatalogDefinition
from codex_web.identity import PrincipalKind
from codex_web.services.identity import AuthorizationError, IdentityError, IdentityService
from codex_web.services.agent_profiles import AgentProfileError
from codex_web.services.definitions import DefinitionConflictError, DefinitionCompatibilityError
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingError
from fastapi import HTTPException

HOST_INSPECTION = ControlPlaneBrokerOperation(
    id='operator.host.inspect', method='GET', path_template='/api/operator/host',
    capability='operator.host.inspect', authority_level=AuthorityLevel.READ,
)


def inspect_host_metadata(project_path: str) -> dict:
    """Fixed metadata only; no shell, caller paths, environment or file contents."""
    disk = shutil.disk_usage(project_path)
    return {
        'process': {'pid': os.getpid(), 'python': platform.python_version(),
                    'implementation': sys.implementation.name},
        'platform': {'system': platform.system(), 'release': platform.release(),
                     'machine': platform.machine()},
        'home_filesystem': {'total_bytes': disk.total, 'used_bytes': disk.used,
                            'free_bytes': disk.free},
    }


class HomeOperatorHostService:
    def __init__(self, *, broker, projects, thread_scope, thread_index, bootstrap_bindings,
                 inspector=inspect_host_metadata):
        self.broker = broker
        self.projects = projects
        self.thread_scope = thread_scope
        self.thread_index = thread_index
        self.bootstrap_bindings = bootstrap_bindings
        self.inspector = inspector
        self.event_input_authorizer = None

    @staticmethod
    def denied(message):
        from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
        raise ControlPlaneBrokerDeniedError(message)

    def authorize(self, assignment, actor):
        try:
            return self._authorize_context(assignment, actor)
        except (IdentityError, AgentProfileError, DefinitionConflictError,
                DefinitionCompatibilityError, ThreadBootstrapBindingError, ValueError, LookupError, HTTPException):
            self.denied('Home operator identity, profile, authority or conversation is unavailable')

    def _authorize_context(self, assignment, actor):
        """Fresh identity and exact explicit declaration, not UI/profile inference."""
        if (actor.principal_kind != PrincipalKind.HUMAN or assignment.created_by != actor.identity_id
                or assignment.project_id != 'home' or assignment.resource_ids or assignment.resource_context or assignment.network.enabled
                or (assignment.organization_id, assignment.workspace_id) !=
                    (actor.organization_id, actor.workspace_id)):
            self.denied('Host inspection requires the exact human-requested Home assignment')
        IdentityService.require_admin(actor)
        if assignment.execution_profile_id != 'orchestration-only':
            self.denied('Host inspection requires isolated scratch orchestration')
        project = self.projects.get('home', actor.tenant)
        operator = project.home_operator
        if not operator or not operator.thread_id or operator.selected_by != actor.identity_id:
            self.denied('Administrator must explicitly select this Home operator conversation')
        self.thread_scope.for_thread(operator.thread_id, actor, 'home')
        row = self.thread_index.get(operator.thread_id)
        if (row is None or row.archived or self.thread_index.replacement_thread_id(operator.thread_id,
                project_id='home', organization_id=actor.organization_id, workspace_id=actor.workspace_id)):
            self.denied('Selected Home operator conversation is unavailable or replaced')
        binding = self.bootstrap_bindings.get_by_thread(operator.thread_id, actor)
        if (binding.assignment_id != assignment.id or binding.execution_id != assignment.execution_id
                or binding.execution_workspace_id != assignment.execution_workspace_id
                or assignment.subject != binding.subject):
            self.denied('Home operator is not bound to this current native assignment')
        profiles = self.broker.execution_profiles
        if profiles is None or profiles.agent_profiles is None or assignment.agent_profile is None:
            self.denied('Host inspection requires a current published Agent Profile')
        profile, ref = profiles.resolve(assignment.execution_profile_id,
            organization_id=actor.organization_id, workspace_id=actor.workspace_id, project_id='home')
        record = profiles.registry.get_record(ref.record_id)
        if (ref != assignment.execution_profile_definition or record.scope_type != DefinitionScope.PROJECT
                or record.scope_id != 'home' or HOST_INSPECTION.id not in profile.control_plane_operations
                or profile.workspace_mode != 'scratch' or profile.repository_access != 'none'
                or profile.host_mutation or profile.network_enabled):
            self.denied('Host inspection is not explicitly declared in the current Home Definition')
        pin = assignment.agent_profile
        if operator.agent_profile_id and (operator.agent_profile_id != pin.profile_id
                or operator.agent_profile_revision != pin.profile_revision):
            self.denied('Selected Home operator Agent Profile changed')
        current, _ = profiles.agent_profiles.resolve_for_execution(pin.profile_id, actor=actor, project_id='home')
        if (current.lifecycle != 'active' or current.record_id != pin.profile_record_id
                or current.revision != pin.profile_revision or current.execution_profile_id != assignment.execution_profile_id
                or current.role_id != pin.role_id or current.authority_role_id != pin.authority_role_id
                or current.authority_definition_ref != pin.authority_definition_ref
                or not pin.authority_role_id or pin.authority_role_id not in self.broker.authority.role_ids_for_actor(actor, project_id='home')
                or reference_for(self.broker.authority.catalog_record(actor=actor, project_id='home')) != pin.authority_definition_ref):
            self.denied('Host inspection Agent Profile or authority binding changed')
        return project, operator

    def authorize_native_input(self, *, assignment, actor, project, source=None, request_checksum=None, input_execution_id=None):
        """Do not let a later non-admin input borrow the original human's worker.

        This is an admission restriction, never a host grant. It also covers
        bootstrap before a native thread can be selected in Home. Ordinary
        undeclared profiles retain their existing admission path.
        """
        event_source = (source.removeprefix('queued:') if isinstance(source, str) else '')
        if event_source.startswith('home-operator-automation:'):
            try:
                if actor is None or assignment is None or self.event_input_authorizer is None:
                    raise ValueError('Home event input authority is unavailable')
                self.event_input_authorizer(source=source, assignment=assignment, actor=actor, project=project,
                    request_checksum=request_checksum, input_execution_id=input_execution_id)
                self._authorize_context(assignment, actor)
                return True
            except (IdentityError, AgentProfileError, DefinitionConflictError, DefinitionCompatibilityError,
                    ThreadBootstrapBindingError, ValueError, LookupError, RuntimeError, HTTPException):
                raise AuthorizationError('Current Home event delegation is unavailable') from None
        if assignment is None or assignment.execution_profile_definition is None:
            return
        profiles = self.broker.execution_profiles
        try:
            if profiles is None:
                raise AuthorizationError('Current execution profile is unavailable')
            record = profiles.registry.get_record(assignment.execution_profile_definition.record_id)
            if reference_for(record) != assignment.execution_profile_definition:
                raise AuthorizationError('Pinned execution profile identity changed')
            profile = ExecutionProfileCatalogDefinition.model_validate(record.payload).profile_map.get(
                assignment.execution_profile_id)
            if profile is None or HOST_INSPECTION.id not in profile.control_plane_operations:
                return
            if (project.id != 'home' or assignment.project_id != 'home'
                    or record.scope_type != DefinitionScope.PROJECT or record.scope_id != 'home'
                    or profile.id != 'orchestration-only' or assignment.resource_ids or assignment.resource_context
                    or actor is None or actor.principal_kind != PrincipalKind.HUMAN or actor.identity_id != assignment.created_by
                    or (actor.organization_id, actor.workspace_id) !=
                        (assignment.organization_id, assignment.workspace_id)):
                raise AuthorizationError('Operator-profile native input requires its exact human administrator')
            if source not in {'web', 'queued:web'}:
                raise AuthorizationError('Home operator input requires canonical human web ingress')
            current_actor = self.broker.identity.actor_for_identity(actor.identity_id, scope=actor.tenant)
            IdentityService.require_admin(current_actor)
            current, reference = profiles.resolve(profile.id, organization_id=actor.organization_id,
                workspace_id=actor.workspace_id, project_id='home')
            if reference != assignment.execution_profile_definition or HOST_INSPECTION.id not in current.control_plane_operations:
                raise AuthorizationError('Home operator execution declaration changed')
            pin = assignment.agent_profile
            if pin is None or profiles.agent_profiles is None:
                raise AuthorizationError('Home operator Agent Profile is unavailable')
            agent, _ = profiles.agent_profiles.resolve_for_execution(pin.profile_id, actor=current_actor, project_id='home')
            if (agent.record_id != pin.profile_record_id or agent.revision != pin.profile_revision
                    or agent.execution_profile_id != profile.id or agent.lifecycle != 'active'):
                raise AuthorizationError('Home operator Agent Profile changed')
            operator = self.projects.get('home', current_actor.tenant).home_operator
            if operator and operator.agent_profile_id and (
                    operator.selected_by != actor.identity_id
                    or operator.agent_profile_id != pin.profile_id
                    or operator.agent_profile_revision != pin.profile_revision):
                raise AuthorizationError('Selected Home operator Agent Profile changed')
            return True
        except AuthorizationError:
            raise
        except (IdentityError, AgentProfileError, DefinitionConflictError, DefinitionCompatibilityError,
                ValueError, LookupError):
            raise AuthorizationError('Current Home operator input authority is unavailable') from None

    def prepare_native_input_guard(self, *, project, actor, execution_id, assignment_id, source,
                                   request_checksum=None, input_execution_id=None):
        """Same-request authority at the existing final native-send boundary.

        No discovery/history or native operation occurs in this callback. The
        exact assignment incarnation remains pinned while current input authority
        is checked again after awaited context preparation and at transport send.
        """
        from codex_web.runtime.execution import ExecutionPrestartRefusal

        def refuse():
            raise ExecutionPrestartRefusal(status_code=403, detail={
                'code': 'native_input_authority_denied',
                'message': 'Current Home operator input authority or custody changed',
            })

        def current():
            try:
                if self.projects.get('home', actor.tenant).home_operator != operator_snapshot:
                    refuse()
                assignment = self.broker.execution_workers.store.assignment(snapshot.id)
                if assignment is None:
                    refuse()
                if any(getattr(assignment, key) != getattr(snapshot, key) for key in (
                        'id', 'fence', 'assigned_worker_id', 'execution_id', 'created_by',
                        'organization_id', 'workspace_id', 'project_id', 'subject',
                        'execution_workspace_id', 'execution_profile_id', 'execution_profile_definition',
                        'agent_profile', 'resource_ids', 'resource_context', 'runtime_binding')):
                    refuse()
                now = self.broker._clock()
                if (assignment.status not in {'claimed', 'running'} or not assignment.lease
                        or assignment.lease.fence != assignment.fence
                        or assignment.lease.worker_id != assignment.assigned_worker_id
                        or assignment.lease.expires_at <= now
                        or assignment.deadline_at is not None and assignment.deadline_at <= now):
                    refuse()
                self.authorize_native_input(assignment=assignment, actor=actor, project=project, source=source,
                    request_checksum=request_checksum, input_execution_id=input_execution_id)
            except (IdentityError, ValueError, LookupError):
                refuse()

        event_source = source.removeprefix('queued:') if isinstance(source, str) else ''
        if event_source.startswith('home-operator-automation:') and (request_checksum is None or input_execution_id is None):
            refuse()
        if not assignment_id:
            return None
        # This lookup is an existing control-plane read, never a claimed ingress
        # identity. The native input actor is independently checked above/below.
        snapshot = self.broker.execution_workers.store.assignment(assignment_id)
        if snapshot is None or snapshot.execution_id != execution_id:
            refuse()
        try:
            if not self.authorize_native_input(assignment=snapshot, actor=actor, project=project, source=source,
                    request_checksum=request_checksum, input_execution_id=input_execution_id):
                return None
        except (IdentityError, ValueError, LookupError):
            refuse()
        operator_snapshot = self.projects.get('home', actor.tenant).home_operator
        return current

    def guard(self, snapshot, worker_actor, resolved, validator):
        if validator is None:
            self.denied('Authenticated current assignment validation is required')
        current = validator()
        if any(getattr(current, key) != getattr(snapshot, key) for key in (
                'id', 'fence', 'execution_id', 'created_by', 'organization_id', 'workspace_id', 'project_id',
                'subject', 'assigned_worker_id', 'execution_workspace_id', 'execution_profile_id',
                'execution_profile_definition', 'agent_profile', 'resource_ids', 'resource_context',
                'runtime_binding', 'sandbox', 'network')):
            self.denied('Host inspection assignment incarnation or scope changed')
        now = self.broker._clock()
        lease = current.lease
        if (current.status not in {'claimed', 'running'} or not lease
                or lease.worker_id != current.assigned_worker_id or lease.fence != current.fence
                or lease.expires_at <= now or current.deadline_at is not None and current.deadline_at <= now):
            self.denied('Host inspection assignment lease or deadline expired')
        workers = self.broker.execution_workers
        if workers is None:
            self.denied('Current worker registration is unavailable')
        registered = workers.store.worker(current.assigned_worker_id)
        if (not registered or registered.lifecycle not in {'active', 'draining'}
                or registered.service_identity_id != worker_actor.identity_id
                or (registered.organization_id, registered.workspace_id) !=
                    (current.organization_id, current.workspace_id)):
            self.denied('Host inspection worker registration changed')
        try:
            workers._require_worker_actor(registered, worker_actor)
        except IdentityError:
            self.denied('Host inspection worker identity or authenticated run scope changed')
        if worker_actor.service_token_id:
            token = next((t for t in self.broker.identity.store.load().service_tokens
                          if t.id == worker_actor.service_token_id), None)
            if (token is None or token.revoked_at is not None or token.expires_at is not None and token.expires_at <= now
                    or token.service_identity_id != worker_actor.identity_id
                    or (token.organization_id, token.workspace_id) != (current.organization_id, current.workspace_id)
                    or set(token.scopes) != set(worker_actor.service_scopes)):
                self.denied('Host inspection worker credential revoked or changed')
        _, requester, _, _ = self.broker._dispatch_scope(current, worker_actor, resolved)
        # _dispatch_scope already rechecked the full Home declaration and
        # canonical authority. Read the current pointer for final-edge comparison.
        project = self.projects.get('home', requester.tenant)
        operator = project.home_operator
        if operator is None or not operator.thread_id:
            self.denied('Home operator selection changed')
        workspaces = self.broker.execution_workspaces
        workspace = workspaces.store.workspace(current.execution_workspace_id) if workspaces else None
        space_lease = workspaces.store.lease(workspace.lease_id) if workspace else None
        if (not workspace or workspace.kind != 'scratch' or workspace.status != 'active'
                or (workspace.organization_id, workspace.workspace_id, workspace.project_id, workspace.execution_id,
                    workspace.subject, workspace.owner_identity_id) !=
                   (current.organization_id, current.workspace_id, 'home', current.execution_id, current.subject, current.created_by)
                or workspace.resource_ids or workspace.resource_context or workspace.writable_repository_ids
                or workspace.read_only_resource_ids or workspace.repository_members or workspace.repository_resource_id
                or not space_lease or space_lease.resource_ids
                or space_lease.execution_workspace_id != workspace.id or space_lease.execution_id != current.execution_id
                or space_lease.subject != current.subject or space_lease.owner_identity_id != current.created_by
                or (space_lease.organization_id, space_lease.workspace_id) != (current.organization_id, current.workspace_id)
                or space_lease.released_at is not None or space_lease.expires_at <= now):
            self.denied('Host inspection scratch workspace or lease changed')
        return current, requester, project, operator

    def inspect(self, assignment, worker_actor, resolved, validator):
        current, requester, project, operator = self.guard(assignment, worker_actor, resolved, validator)
        observed = self.inspector(project.path)
        # A selection, profile, membership or custody revocation during the read
        # suppresses the result. Never silently inspect for a successor request.
        after, _, after_project, after_operator = self.guard(assignment, worker_actor, resolved, validator)
        if operator != after_operator or project.path != after_project.path:
            self.denied('Home operator selection or configured host path changed during inspection')
        return {'observed_at': self.broker._clock(), 'metadata': observed,
                'assignment_id': after.id, 'fence': after.fence,
                'operator_binding': operator.model_dump(mode='json'),
                'execution_profile_definition': after.execution_profile_definition.model_dump(mode='json'),
                'requester_identity_id': requester.identity_id,
                'scope': 'read-only fixed host metadata; no commands, file contents or credentials'}
