from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Callable

from codex_web.execution_workspace_backend import (
    ExecutionWorkspaceBackend,
    ExecutionWorkspaceBackendError,
)
from codex_web.execution_workspaces import (
    ExecutionWorkspace,
    ExecutionWorkspaceAcquire,
    ExecutionWorkspaceEvent,
    ExecutionWorkspaceInspection,
    ExecutionWorkspaceKind,
    ExecutionWorkspaceMember,
    ExecutionWorkspaceLease,
    ExecutionWorkspaceReference,
    ExecutionWorkspaceRelease,
    ExecutionWorkspaceRenew,
    ExecutionWorkspaceStatus,
    IntegrationOutcome,
    LeaseMode,
    WorkspaceIntegrationRecord,
    WorkspaceIntegrationState,
    WorkspaceQuota,
    deterministic_branch_name,
    deterministic_workspace_id,
)
from codex_web.identity import AuthenticationActor, MembershipRole, PrincipalKind, TenantScope
from codex_web.models import Project
from codex_web.resources import Resource, ResourceType
from codex_web.services.identity import AuthorizationError, TenantIsolationError
from codex_web.services.resources import ResourceCatalogService, ResourceNotFoundError
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore


class ExecutionWorkspaceError(RuntimeError):
    pass


class ExecutionWorkspaceNotFoundError(ExecutionWorkspaceError):
    pass


class ExecutionWorkspaceConflictError(ExecutionWorkspaceError):
    pass


class ExecutionWorkspaceQuotaError(ExecutionWorkspaceError):
    pass


class ExecutionWorkspaceLeaseError(ExecutionWorkspaceError):
    pass


class ExecutionWorkspaceService:
    def __init__(
        self,
        store: ExecutionWorkspaceStateStore,
        backend: ExecutionWorkspaceBackend,
        resources: ResourceCatalogService,
        project_lookup: Callable[[str], Project],
        *,
        work_item_host: Any | None = None,
        quota: WorkspaceQuota | None = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.resources = resources
        self.project_lookup = project_lookup
        self.work_item_host = work_item_host
        self.quota = quota or WorkspaceQuota()

    @staticmethod
    def _admin(actor: AuthenticationActor) -> bool:
        if actor.principal_kind == PrincipalKind.SERVICE:
            return "execution-workspace:admin" in actor.service_scopes
        return actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN)

    @staticmethod
    def _scope_matches(workspace: ExecutionWorkspace, scope: TenantScope) -> bool:
        return (
            workspace.organization_id == scope.organization_id
            and workspace.workspace_id == scope.workspace_id
        )

    @staticmethod
    def _lease_active(lease: ExecutionWorkspaceLease, now: float) -> bool:
        return lease.released_at is None and lease.expires_at > now

    @staticmethod
    def _append_event(state, event: ExecutionWorkspaceEvent) -> None:
        state.events.append(event)
        state.events = state.events[-5000:]

    def _project(self, project_id: str, actor: AuthenticationActor) -> Project:
        try:
            project = self.project_lookup(project_id)
        except Exception as exc:
            raise ExecutionWorkspaceNotFoundError("project not found") from exc
        if (
            project.organization_id != actor.organization_id
            or project.workspace_id != actor.workspace_id
        ):
            raise ExecutionWorkspaceNotFoundError("project not found")
        return project

    def _authorized(self, workspace: ExecutionWorkspace, actor: AuthenticationActor) -> None:
        if not self._scope_matches(workspace, actor.tenant):
            raise ExecutionWorkspaceNotFoundError("execution workspace not found")
        if workspace.owner_identity_id != actor.identity_id and not self._admin(actor):
            raise AuthorizationError("execution workspace owner or administrator required")

    @staticmethod
    def _repository_source_path(
        resource: Resource,
        project: Project,
        *,
        allow_project_fallback: bool,
    ) -> Path:
        project_root = Path(project.path).resolve(strict=True)
        aliases = [
            alias
            for alias in resource.aliases
            if alias.namespace.casefold()
            in {"filesystem", "path", "repository-path", "legacy"}
        ]
        for alias in aliases:
            raw = alias.value.strip()
            if not raw or raw.startswith("~"):
                continue
            candidate = Path(raw)
            try:
                if candidate.is_absolute():
                    resolved = candidate.resolve(strict=True)
                else:
                    resolved = (project_root / candidate).resolve(strict=True)
                    if not resolved.is_relative_to(project_root):
                        raise ExecutionWorkspaceConflictError(
                            "relative repository source escapes canonical project root"
                        )
            except FileNotFoundError:
                continue
            if resolved.is_dir():
                return resolved
        if allow_project_fallback:
            return project_root
        raise ExecutionWorkspaceConflictError(
            f"repository resource {resource.id} has no canonical filesystem source alias"
        )

    def _resource_set(
        self,
        request: ExecutionWorkspaceAcquire,
        actor: AuthenticationActor,
    ) -> tuple[list[Resource], str | None, tuple[str, ...], tuple[str, ...]]:
        resources = [
            self.resources.get(resource_id, actor)
            for resource_id in request.resource_ids
        ]
        by_id = {item.id: item for item in resources}
        repository_resource_id = request.repository_resource_id
        writable_repository_ids = tuple(request.writable_repository_ids)
        repositories = [
            item for item in resources
            if item.resource_type == ResourceType.REPOSITORY
        ]
        if repository_resource_id is None and not writable_repository_ids and len(repositories) == 1:
            repository_resource_id = repositories[0].id
            writable_repository_ids = (repository_resource_id,)
        elif repository_resource_id is None and writable_repository_ids:
            repository_resource_id = writable_repository_ids[0]
        elif repository_resource_id is not None and not writable_repository_ids:
            writable_repository_ids = (repository_resource_id,)

        for resource_id in writable_repository_ids:
            resource = by_id.get(resource_id)
            if resource is None:
                raise ResourceNotFoundError(
                    "writable repository is outside requested resource set"
                )
            if resource.resource_type != ResourceType.REPOSITORY:
                raise ExecutionWorkspaceConflictError(
                    "writable repository must reference canonical repository resources"
                )
        if (
            repository_resource_id is not None
            and repository_resource_id not in writable_repository_ids
        ):
            raise ExecutionWorkspaceConflictError(
                "primary repository must be included in writable repository set"
            )

        read_only_repository_ids = tuple(request.read_only_repository_ids)
        for resource_id in read_only_repository_ids:
            resource = by_id.get(resource_id)
            if resource is None:
                raise ResourceNotFoundError(
                    "read-only repository is outside requested resource set"
                )
            if resource.resource_type != ResourceType.REPOSITORY:
                raise ExecutionWorkspaceConflictError(
                    "read-only repository context must reference repository resources"
                )
            if resource_id in writable_repository_ids:
                raise ExecutionWorkspaceConflictError(
                    "writable repository cannot also be read-only context"
                )
        return (
            resources,
            repository_resource_id,
            writable_repository_ids,
            read_only_repository_ids,
        )

    @staticmethod
    def _member_workspace_id(
        workspace_id: str,
        resource_id: str,
        *,
        access_mode: LeaseMode,
    ) -> str:
        suffix = hashlib.sha256(resource_id.encode()).hexdigest()[:12]
        mode = "write" if access_mode == LeaseMode.WRITE else "readonly"
        return f"{workspace_id}-{mode}-{suffix}"

    @staticmethod
    def _writable_sandbox_path(resource_id: str) -> str:
        safe = "".join(
            character if character.isalnum() or character in "-._" else "-"
            for character in resource_id
        ).strip("-._")
        return f"/mnt/codex-repositories/{safe or 'repository'}"

    @staticmethod
    def _readonly_sandbox_path(resource_id: str) -> str:
        safe = "".join(
            character if character.isalnum() or character in "-._" else "-"
            for character in resource_id
        ).strip("-._")
        return f"/mnt/codex-context/{safe or 'repository'}"

    def _existing(self, workspace_id: str, actor: AuthenticationActor) -> ExecutionWorkspace | None:
        item = next(
            (
                workspace
                for workspace in self.store.load().workspaces
                if workspace.id == workspace_id
                and self._scope_matches(workspace, actor.tenant)
            ),
            None,
        )
        if item is not None:
            self._authorized(item, actor)
        return item

    def _workspace_reference(self, workspace: ExecutionWorkspace) -> ExecutionWorkspaceReference:
        lease = next(
            (item for item in self.store.load().leases if item.id == workspace.lease_id),
            None,
        )
        return ExecutionWorkspaceReference(
            workspace_id=workspace.id,
            lease_id=workspace.lease_id,
            kind=workspace.kind,
            status=workspace.status,
            resource_ids=workspace.resource_ids,
            repository_members=workspace.repository_members,
            path=workspace.path,
            branch_name=workspace.branch_name,
            base_revision=workspace.base_revision,
            head_revision=workspace.head_revision,
            lease_expires_at=lease.expires_at if lease and lease.released_at is None else None,
        )

    def _sync_work_item(self, workspace: ExecutionWorkspace) -> None:
        host = self.work_item_host
        if host is None:
            return
        loader = getattr(host, "_load_work_item_states", None)
        saver = getattr(host, "_save_work_item_states", None)
        if not callable(loader) or not callable(saver):
            return
        if workspace.work_item_ref is None:
            return
        states = loader()
        state = states.get(workspace.work_item_ref)
        if state is None:
            return
        if (
            state.organization_id != workspace.organization_id
            or state.workspace_id != workspace.workspace_id
        ):
            return
        state.execution.workspace = self._workspace_reference(workspace)
        state.updated_at = time.time()
        states[state.ref] = state
        saver(states)
        event_factory = getattr(host, "_work_item_event", None)
        append = getattr(host, "_append_work_item_event", None)
        if callable(event_factory) and callable(append):
            append(
                event_factory(
                    state.ref,
                    "execution_workspace_updated",
                    payload={
                        "workspace_id": workspace.id,
                        "lease_id": workspace.lease_id,
                        "status": workspace.status.value,
                        "base_revision": workspace.base_revision,
                        "branch_name": workspace.branch_name,
                    },
                )
            )

    def list(self, actor: AuthenticationActor) -> list[ExecutionWorkspace]:
        return sorted(
            [
                item
                for item in self.store.load().workspaces
                if self._scope_matches(item, actor.tenant)
            ],
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )

    def inspect(
        self,
        actor: AuthenticationActor,
        *,
        now: float | None = None,
    ) -> list[ExecutionWorkspaceInspection]:
        observed_at = time.time() if now is None else now
        state = self.store.load()
        leases = {
            lease.id: lease
            for lease in state.leases
            if (
                lease.organization_id == actor.organization_id
                and lease.workspace_id == actor.workspace_id
            )
        }
        items = []
        for workspace in state.workspaces:
            if not self._scope_matches(workspace, actor.tenant):
                continue
            lease = leases.get(workspace.lease_id)
            lease_active = bool(
                lease is not None
                and lease.released_at is None
                and lease.expires_at > observed_at
            )
            lease_expired = bool(
                lease is not None
                and lease.released_at is None
                and lease.expires_at <= observed_at
            )
            items.append(
                ExecutionWorkspaceInspection(
                    workspace=workspace,
                    lease=lease,
                    lease_active=lease_active,
                    lease_expired=lease_expired,
                    observed_at=observed_at,
                )
            )
        return sorted(
            items,
            key=lambda item: (
                item.workspace.created_at,
                item.workspace.id,
            ),
            reverse=True,
        )

    def get(self, workspace_id: str, actor: AuthenticationActor) -> ExecutionWorkspace:
        item = self._existing(workspace_id, actor)
        if item is None:
            raise ExecutionWorkspaceNotFoundError("execution workspace not found")
        return item

    def _recover_state(self, state, now: float, scope: TenantScope | None):
        abandoned: list[str] = []
        for index, lease in enumerate(state.leases):
            if lease.released_at is not None or lease.expires_at > now:
                continue
            if scope is not None and (
                lease.organization_id != scope.organization_id
                or lease.workspace_id != scope.workspace_id
            ):
                continue
            state.leases[index] = lease.model_copy(
                update={
                    "released_at": now,
                    "release_reason": "lease-expired",
                }
            )
            for windex, workspace in enumerate(state.workspaces):
                if workspace.id != lease.execution_workspace_id:
                    continue
                if workspace.status in {
                    ExecutionWorkspaceStatus.RELEASED,
                    ExecutionWorkspaceStatus.DISCARDED,
                }:
                    break
                updated = workspace.model_copy(
                    update={
                        "status": ExecutionWorkspaceStatus.ABANDONED,
                        "updated_at": now,
                    }
                )
                state.workspaces[windex] = updated
                abandoned.append(updated.id)
                self._append_event(
                    state,
                    ExecutionWorkspaceEvent(
                        workspace_id=updated.id,
                        event_type="lease_expired",
                        details={"lease_id": lease.id},
                    ),
                )
                break
        return state, abandoned

    def _cleanup_scratch_workspace(self, workspace: ExecutionWorkspace) -> None:
        if workspace.path:
            self.backend.cleanup_scratch(Path(workspace.path))

    def _cleanup_git_workspace(
        self,
        workspace: ExecutionWorkspace,
        *,
        discard_mutable_branch: bool,
    ) -> None:
        if workspace.repository_members:
            errors: list[str] = []
            for member in reversed(workspace.repository_members):
                try:
                    self.backend.cleanup_git(
                        Path(member.source_path),
                        Path(member.workspace_path),
                        member.branch_name or "",
                        discard_branch=bool(
                            discard_mutable_branch
                            and member.access_mode == LeaseMode.WRITE
                            and member.branch_name
                        ),
                    )
                except Exception as exc:
                    errors.append(f"{member.resource_id}: {exc}")
            if errors:
                raise ExecutionWorkspaceBackendError(
                    "repository member cleanup failed: " + "; ".join(errors)
                )
            return
        if workspace.path and workspace.branch_name:
            project = self.project_lookup(workspace.project_id)
            self.backend.cleanup_git(
                Path(project.path),
                Path(workspace.path),
                workspace.branch_name,
                discard_branch=discard_mutable_branch,
            )

    def recover_expired(
        self,
        *,
        now: float | None = None,
        scope: TenantScope | None = None,
    ) -> list[ExecutionWorkspace]:
        current = time.time() if now is None else now
        abandoned_ids: list[str] = []

        def apply(state):
            updated, ids = self._recover_state(state, current, scope)
            abandoned_ids.extend(ids)
            return updated

        self.store.update(apply)
        recovered: list[ExecutionWorkspace] = []
        for workspace_id in abandoned_ids:
            state = self.store.load()
            workspace = next(item for item in state.workspaces if item.id == workspace_id)
            if (
                workspace.kind in {
                    ExecutionWorkspaceKind.GIT_WORKTREE,
                    ExecutionWorkspaceKind.SCRATCH,
                }
                and workspace.path
            ):
                try:
                    if workspace.kind == ExecutionWorkspaceKind.SCRATCH:
                        self._cleanup_scratch_workspace(workspace)
                    else:
                        self._cleanup_git_workspace(
                            workspace,
                            discard_mutable_branch=False,
                        )
                    cleaned_at = time.time()

                    def mark_cleaned(current_state):
                        for index, item in enumerate(current_state.workspaces):
                            if item.id == workspace_id:
                                current_state.workspaces[index] = item.model_copy(
                                    update={"cleaned_at": cleaned_at, "updated_at": cleaned_at}
                                )
                                return current_state
                        return current_state

                    self.store.update(mark_cleaned)
                except Exception as exc:
                    message = str(exc)

                    def mark_error(current_state):
                        for index, item in enumerate(current_state.workspaces):
                            if item.id == workspace_id:
                                current_state.workspaces[index] = item.model_copy(
                                    update={"error": message, "updated_at": time.time()}
                                )
                                return current_state
                        return current_state

                    self.store.update(mark_error)
            workspace = next(
                item for item in self.store.load().workspaces if item.id == workspace_id
            )
            self._sync_work_item(workspace)
            recovered.append(workspace)
        return recovered

    def acquire(
        self,
        request: ExecutionWorkspaceAcquire,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionWorkspace:
        project = self._project(request.project_id, actor)
        (
            resources,
            repository_resource_id,
            writable_repository_ids,
            read_only_repository_ids,
        ) = self._resource_set(
            request,
            actor,
        )
        resource_by_id = {item.id: item for item in resources}
        self.recover_expired(scope=actor.tenant)

        workspace_id = deterministic_workspace_id(
            actor.organization_id,
            actor.workspace_id,
            request.work_item_ref,
            request.execution_id,
            subject=request.subject,
        )
        existing = self._existing(workspace_id, actor)
        if existing is not None:
            if (
                existing.resource_ids != request.resource_ids
                or existing.repository_resource_id != repository_resource_id
                or tuple(existing.writable_repository_ids) != writable_repository_ids
            ):
                raise ExecutionWorkspaceConflictError(
                    "execution id is already bound to a different resource set"
                )
            if existing.status in {
                ExecutionWorkspaceStatus.PROVISIONING,
                ExecutionWorkspaceStatus.ACTIVE,
                ExecutionWorkspaceStatus.CONFLICTED,
                ExecutionWorkspaceStatus.INTEGRATED,
            }:
                return existing
            raise ExecutionWorkspaceConflictError(
                "execution id already has a terminal workspace; use a new execution id"
            )

        now = time.time()
        lease_id = f"{workspace_id}-lease"
        kind = (
            ExecutionWorkspaceKind.SCRATCH
            if request.scratch
            else (
                ExecutionWorkspaceKind.GIT_WORKTREE
                if writable_repository_ids
                else ExecutionWorkspaceKind.RESOURCE_LEASE
            )
        )
        branch_name = (
            deterministic_branch_name(request.subject.key, request.execution_id)
            if kind == ExecutionWorkspaceKind.GIT_WORKTREE
            else None
        )
        if len(request.resource_ids) > self.quota.max_resources_per_workspace:
            raise ExecutionWorkspaceQuotaError(
                "execution workspace resource quota exceeded"
            )
        if request.requested_disk_bytes > self.quota.max_requested_disk_bytes:
            raise ExecutionWorkspaceQuotaError(
                "execution workspace requested disk quota exceeded"
            )

        resource_modes = {
            resource_id: request.lease_mode
            for resource_id in request.resource_ids
        }
        for resource_id in writable_repository_ids:
            resource_modes[resource_id] = (
                LeaseMode.WRITE
                if len(writable_repository_ids) > 1
                else request.lease_mode
            )
        for resource_id in read_only_repository_ids:
            resource_modes[resource_id] = LeaseMode.READ
        aggregate_mode = (
            LeaseMode.WRITE
            if any(mode == LeaseMode.WRITE for mode in resource_modes.values())
            else LeaseMode.READ
        )

        source_paths: dict[str, Path] = {}
        if writable_repository_ids:
            for resource_id in writable_repository_ids:
                source_paths[resource_id] = self._repository_source_path(
                    resource_by_id[resource_id],
                    project,
                    allow_project_fallback=(resource_id == repository_resource_id),
                )
            for resource_id in read_only_repository_ids:
                source_paths[resource_id] = self._repository_source_path(
                    resource_by_id[resource_id],
                    project,
                    allow_project_fallback=False,
                )
            normalized_sources = [str(path) for path in source_paths.values()]
            if len(normalized_sources) != len(set(normalized_sources)):
                raise ExecutionWorkspaceConflictError(
                    "repository members must resolve to distinct canonical source paths"
                )

        lease = ExecutionWorkspaceLease(
            id=lease_id,
            execution_workspace_id=workspace_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            subject=request.subject,
            work_item_ref=request.work_item_ref,
            execution_id=request.execution_id,
            owner_identity_id=actor.identity_id,
            resource_ids=request.resource_ids,
            mode=aggregate_mode,
            resource_modes=resource_modes,
            acquired_at=now,
            expires_at=now + request.ttl_seconds,
        )
        workspace = ExecutionWorkspace(
            id=workspace_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            subject=request.subject,
            work_item_ref=request.work_item_ref,
            execution_id=request.execution_id,
            project_id=request.project_id,
            owner_identity_id=actor.identity_id,
            kind=kind,
            resource_ids=request.resource_ids,
            repository_resource_id=repository_resource_id,
            writable_repository_ids=writable_repository_ids,
            lease_id=lease_id,
            branch_name=branch_name,
            base_revision=request.base_revision,
            requested_disk_bytes=request.requested_disk_bytes,
            created_at=now,
            updated_at=now,
        )

        reservation_already_exists: list[bool] = []

        def reserve(state):
            if any(item.id == workspace_id for item in state.workspaces):
                reservation_already_exists.append(True)
                return state
            active_leases = [
                item
                for item in state.leases
                if self._lease_active(item, now)
                and item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
            ]
            if len(active_leases) >= self.quota.max_active_per_tenant:
                raise ExecutionWorkspaceQuotaError(
                    "tenant active execution workspace quota exceeded"
                )
            owner_active = [
                item
                for item in active_leases
                if item.owner_identity_id == actor.identity_id
            ]
            if len(owner_active) >= self.quota.max_active_per_identity:
                raise ExecutionWorkspaceQuotaError(
                    "identity active execution workspace quota exceeded"
                )
            requested = set(request.resource_ids)
            for active in active_leases:
                overlap = requested.intersection(active.resource_ids)
                conflicting = [
                    resource_id
                    for resource_id in overlap
                    if (
                        resource_modes.get(resource_id, aggregate_mode)
                        == LeaseMode.WRITE
                        or active.resource_modes.get(resource_id, active.mode)
                        == LeaseMode.WRITE
                    )
                ]
                if conflicting:
                    raise ExecutionWorkspaceConflictError(
                        "conflicting active resource lease: "
                        + ", ".join(sorted(conflicting))
                    )
            state.leases.append(lease)
            state.workspaces.append(workspace)
            self._append_event(
                state,
                ExecutionWorkspaceEvent(
                    workspace_id=workspace.id,
                    event_type="workspace_reserved",
                    actor_identity_id=actor.identity_id,
                    details={
                        "lease_id": lease.id,
                        "kind": kind.value,
                        "subject_kind": request.subject.kind.value,
                        "subject_ref": request.subject.ref,
                        "expires_at": lease.expires_at,
                        "repository_member_count": (
                            len(writable_repository_ids) + len(read_only_repository_ids)
                        ),
                        "writable_repository_count": len(writable_repository_ids),
                    },
                ),
            )
            return state

        self.store.update(reserve)
        if reservation_already_exists:
            current = self.get(workspace_id, actor)
            if current.status in {
                ExecutionWorkspaceStatus.PROVISIONING,
                ExecutionWorkspaceStatus.ACTIVE,
                ExecutionWorkspaceStatus.CONFLICTED,
                ExecutionWorkspaceStatus.INTEGRATED,
            }:
                return current
            raise ExecutionWorkspaceConflictError(
                "execution id already has a terminal workspace; use a new execution id"
            )

        if kind == ExecutionWorkspaceKind.SCRATCH:
            try:
                scratch_path = self.backend.provision_scratch(workspace_id)
            except Exception as exc:
                failed_at = time.time()
                message = str(exc)

                def fail_scratch(state):
                    for index, item in enumerate(state.leases):
                        if item.id == lease_id:
                            state.leases[index] = item.model_copy(
                                update={
                                    "released_at": failed_at,
                                    "release_reason": "provisioning-failed",
                                }
                            )
                    for index, item in enumerate(state.workspaces):
                        if item.id == workspace_id:
                            state.workspaces[index] = item.model_copy(
                                update={
                                    "status": ExecutionWorkspaceStatus.ERROR,
                                    "error": message,
                                    "updated_at": failed_at,
                                }
                            )
                    self._append_event(
                        state,
                        ExecutionWorkspaceEvent(
                            workspace_id=workspace_id,
                            event_type="workspace_provisioning_failed",
                            actor_identity_id=actor.identity_id,
                            details={"error": message[:500]},
                        ),
                    )
                    return state

                self.store.update(fail_scratch)
                raise ExecutionWorkspaceBackendError(message) from exc

            activated_at = time.time()

            def activate_scratch(state):
                for index, item in enumerate(state.workspaces):
                    if item.id == workspace_id:
                        state.workspaces[index] = item.model_copy(
                            update={
                                "status": ExecutionWorkspaceStatus.ACTIVE,
                                "path": str(scratch_path),
                                "actual_disk_bytes": self.backend.disk_usage(scratch_path),
                                "updated_at": activated_at,
                            }
                        )
                        self._append_event(
                            state,
                            ExecutionWorkspaceEvent(
                                workspace_id=workspace_id,
                                event_type="workspace_activated",
                                actor_identity_id=actor.identity_id,
                                details={"scratch": True},
                            ),
                        )
                        break
                return state

            self.store.update(activate_scratch)
            activated = next(
                item
                for item in self.store.load().workspaces
                if item.id == workspace_id
            )
            self._sync_work_item(activated)
            return activated

        if kind == ExecutionWorkspaceKind.RESOURCE_LEASE:
            activated_at = time.time()

            def activate_resource(state):
                for index, item in enumerate(state.workspaces):
                    if item.id == workspace_id:
                        state.workspaces[index] = item.model_copy(
                            update={
                                "status": ExecutionWorkspaceStatus.ACTIVE,
                                "updated_at": activated_at,
                            }
                        )
                        self._append_event(
                            state,
                            ExecutionWorkspaceEvent(
                                workspace_id=workspace_id,
                                event_type="workspace_activated",
                                actor_identity_id=actor.identity_id,
                            ),
                        )
                        break
                return state

            self.store.update(activate_resource)
            activated = next(
                item
                for item in self.store.load().workspaces
                if item.id == workspace_id
            )
            self._sync_work_item(activated)
            return activated

        provisioned_members: list[
            tuple[Resource, ExecutionWorkspaceMember]
        ] = []
        try:
            for resource_id in writable_repository_ids:
                resource = resource_by_id[resource_id]
                source = source_paths[resource_id]
                primary = resource_id == repository_resource_id
                member_workspace_id = (
                    workspace_id
                    if primary
                    else self._member_workspace_id(
                        workspace_id,
                        resource_id,
                        access_mode=LeaseMode.WRITE,
                    )
                )
                provisioned = self.backend.provision_git(
                    source,
                    member_workspace_id,
                    branch_name or "",
                    request.base_revision if primary else None,
                )
                provisioned_members.append(
                    (
                        resource,
                        ExecutionWorkspaceMember(
                            resource_id=resource_id,
                            access_mode=resource_modes[resource_id],
                            source_path=str(source),
                            workspace_path=str(provisioned.path),
                            sandbox_path=(
                                str(provisioned.path)
                                if primary
                                else self._writable_sandbox_path(resource_id)
                            ),
                            branch_name=provisioned.branch_name,
                            base_revision=provisioned.base_revision,
                            head_revision=provisioned.head_revision,
                            disk_bytes=self.backend.disk_usage(provisioned.path),
                        ),
                    )
                )

            for resource_id in read_only_repository_ids:
                resource = resource_by_id[resource_id]
                source = source_paths[resource_id]
                provisioned = self.backend.provision_git_readonly(
                    source,
                    self._member_workspace_id(
                        workspace_id,
                        resource_id,
                        access_mode=LeaseMode.READ,
                    ),
                    None,
                )
                provisioned_members.append(
                    (
                        resource,
                        ExecutionWorkspaceMember(
                            resource_id=resource_id,
                            access_mode=LeaseMode.READ,
                            source_path=str(source),
                            workspace_path=str(provisioned.path),
                            sandbox_path=self._readonly_sandbox_path(resource_id),
                            branch_name=None,
                            base_revision=provisioned.base_revision,
                            head_revision=provisioned.head_revision,
                            disk_bytes=self.backend.disk_usage(provisioned.path),
                        ),
                    )
                )
        except Exception as exc:
            for _resource, member in reversed(provisioned_members):
                try:
                    self.backend.cleanup_git(
                        Path(member.source_path),
                        Path(member.workspace_path),
                        member.branch_name or "",
                        discard_branch=bool(member.branch_name),
                    )
                except Exception:
                    pass
            failed_at = time.time()
            message = str(exc)

            def fail(state):
                for index, item in enumerate(state.leases):
                    if item.id == lease_id:
                        state.leases[index] = item.model_copy(
                            update={
                                "released_at": failed_at,
                                "release_reason": "provisioning-failed",
                            }
                        )
                for index, item in enumerate(state.workspaces):
                    if item.id == workspace_id:
                        state.workspaces[index] = item.model_copy(
                            update={
                                "status": ExecutionWorkspaceStatus.ERROR,
                                "error": message,
                                "updated_at": failed_at,
                            }
                        )
                self._append_event(
                    state,
                    ExecutionWorkspaceEvent(
                        workspace_id=workspace_id,
                        event_type="workspace_provisioning_failed",
                        actor_identity_id=actor.identity_id,
                        details={"error": message[:500]},
                    ),
                )
                return state

            self.store.update(fail)
            raise ExecutionWorkspaceBackendError(message) from exc

        mutable_member = next(
            member
            for _resource, member in provisioned_members
            if member.resource_id == repository_resource_id
        )
        actual_disk_bytes = sum(
            member.disk_bytes
            for _resource, member in provisioned_members
        )
        activated_at = time.time()

        def activate(state):
            for index, item in enumerate(state.workspaces):
                if item.id == workspace_id:
                    state.workspaces[index] = item.model_copy(
                        update={
                            "status": ExecutionWorkspaceStatus.ACTIVE,
                            "path": mutable_member.workspace_path,
                            "branch_name": mutable_member.branch_name,
                            "base_revision": mutable_member.base_revision,
                            "head_revision": mutable_member.head_revision,
                            "actual_disk_bytes": actual_disk_bytes,
                            "repository_members": tuple(
                                member
                                for _resource, member in provisioned_members
                            ),
                            "updated_at": activated_at,
                        }
                    )
                    self._append_event(
                        state,
                        ExecutionWorkspaceEvent(
                            workspace_id=workspace_id,
                            event_type="workspace_activated",
                            actor_identity_id=actor.identity_id,
                            details={
                                "base_revision": mutable_member.base_revision,
                                "branch_name": mutable_member.branch_name,
                                "repository_member_count": len(provisioned_members),
                                "writable_member_count": len(writable_repository_ids),
                            "read_only_member_count": len(read_only_repository_ids),
                            },
                        ),
                    )
                    break
            return state

        self.store.update(activate)
        activated = next(
            item for item in self.store.load().workspaces
            if item.id == workspace_id
        )
        self._sync_work_item(activated)
        return activated

    def renew(
        self,
        workspace_id: str,
        request: ExecutionWorkspaceRenew,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionWorkspace:
        workspace = self.get(workspace_id, actor)
        self._authorized(workspace, actor)
        now = time.time()

        def apply(state):
            lease_found = False
            for index, lease in enumerate(state.leases):
                if lease.id != workspace.lease_id:
                    continue
                if lease.released_at is not None or lease.expires_at <= now:
                    raise ExecutionWorkspaceLeaseError("execution workspace lease is not active")
                state.leases[index] = lease.model_copy(
                    update={
                        "expires_at": now + request.ttl_seconds,
                        "renewed_at": now,
                    }
                )
                lease_found = True
                break
            if not lease_found:
                raise ExecutionWorkspaceLeaseError("execution workspace lease not found")
            for index, item in enumerate(state.workspaces):
                if item.id == workspace_id:
                    state.workspaces[index] = item.model_copy(update={"updated_at": now})
                    break
            self._append_event(
                state,
                ExecutionWorkspaceEvent(
                    workspace_id=workspace_id,
                    event_type="lease_renewed",
                    actor_identity_id=actor.identity_id,
                    details={"expires_at": now + request.ttl_seconds},
                ),
            )
            return state

        self.store.update(apply)
        updated = self.get(workspace_id, actor)
        self._sync_work_item(updated)
        return updated

    def record_integration(
        self,
        workspace_id: str,
        request: WorkspaceIntegrationRecord,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionWorkspace:
        workspace = self.get(workspace_id, actor)
        self._authorized(workspace, actor)
        now = time.time()
        status = workspace.status
        if request.outcome == IntegrationOutcome.CONFLICT:
            status = ExecutionWorkspaceStatus.CONFLICTED
        elif request.outcome in {
            IntegrationOutcome.MERGED,
            IntegrationOutcome.REBASED,
            IntegrationOutcome.FAST_FORWARDED,
        }:
            status = ExecutionWorkspaceStatus.INTEGRATED
        elif request.outcome == IntegrationOutcome.DISCARDED:
            status = ExecutionWorkspaceStatus.DISCARDED
        integration = WorkspaceIntegrationState(
            strategy=request.strategy,
            outcome=request.outcome,
            target_revision=request.target_revision,
            resulting_revision=request.resulting_revision,
            conflicts=request.conflicts,
            recorded_at=now,
            recorded_by=actor.identity_id,
        )

        def apply(state):
            for index, item in enumerate(state.workspaces):
                if item.id == workspace_id:
                    updates = {
                        "integration": integration,
                        "status": status,
                        "updated_at": now,
                    }
                    if request.resulting_revision:
                        updates["head_revision"] = request.resulting_revision
                        updates["repository_members"] = tuple(
                            member.model_copy(
                                update={"head_revision": request.resulting_revision}
                            )
                            if member.resource_id == item.repository_resource_id
                            else member
                            for member in item.repository_members
                        )
                    state.workspaces[index] = item.model_copy(update=updates)
                    break
            self._append_event(
                state,
                ExecutionWorkspaceEvent(
                    workspace_id=workspace_id,
                    event_type="integration_recorded",
                    actor_identity_id=actor.identity_id,
                    details={
                        "outcome": request.outcome.value,
                        "strategy": request.strategy.value,
                        "conflict_count": len(request.conflicts),
                    },
                ),
            )
            return state

        self.store.update(apply)
        updated = self.get(workspace_id, actor)
        self._sync_work_item(updated)
        return updated

    def release(
        self,
        workspace_id: str,
        request: ExecutionWorkspaceRelease,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionWorkspace:
        workspace = self.get(workspace_id, actor)
        self._authorized(workspace, actor)
        now = time.time()

        def mark_released(state):
            for index, lease in enumerate(state.leases):
                if lease.id == workspace.lease_id and lease.released_at is None:
                    state.leases[index] = lease.model_copy(
                        update={
                            "released_at": now,
                            "release_reason": request.reason or (
                                "discarded" if request.discard else "released"
                            ),
                        }
                    )
            for index, item in enumerate(state.workspaces):
                if item.id == workspace_id:
                    state.workspaces[index] = item.model_copy(
                        update={
                            "status": (
                                ExecutionWorkspaceStatus.DISCARDED
                                if request.discard
                                else ExecutionWorkspaceStatus.RELEASED
                            ),
                            "updated_at": now,
                        }
                    )
                    break
            self._append_event(
                state,
                ExecutionWorkspaceEvent(
                    workspace_id=workspace_id,
                    event_type="workspace_released",
                    actor_identity_id=actor.identity_id,
                    details={"discard": request.discard},
                ),
            )
            return state

        self.store.update(mark_released)

        if workspace.kind == ExecutionWorkspaceKind.GIT_WORKTREE:
            self._cleanup_git_workspace(
                workspace,
                discard_mutable_branch=request.discard,
            )
        elif workspace.kind == ExecutionWorkspaceKind.SCRATCH:
            self._cleanup_scratch_workspace(workspace)
        cleaned_at = time.time()

        def mark_cleaned(state):
            for index, item in enumerate(state.workspaces):
                if item.id == workspace_id:
                    state.workspaces[index] = item.model_copy(
                        update={"cleaned_at": cleaned_at, "updated_at": cleaned_at}
                    )
                    break
            return state

        self.store.update(mark_cleaned)
        updated = self.get(workspace_id, actor)
        self._sync_work_item(updated)
        return updated

    def events(
        self,
        workspace_id: str,
        *,
        actor: AuthenticationActor,
    ) -> list[ExecutionWorkspaceEvent]:
        self.get(workspace_id, actor)
        return [
            item for item in self.store.load().events if item.workspace_id == workspace_id
        ]
