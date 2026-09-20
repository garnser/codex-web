from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Callable

from codex_web.identity import AuthenticationActor
from codex_web.legacy_project_migration import (
    AuthorityDifference,
    LegacyMigrationExecution,
    LegacyMigrationPlan,
    LegacyPathCompatibilityMapping,
    LegacyRepositoryProposal,
    LegacyThreadProposal,
    MigrationApplyStatus,
    MigrationDisposition,
)
from codex_web.models import BotBinding, IndexedThread, Project, ThreadRunSettings
from codex_web.resources import (
    ResourceAlias,
    ResourceCreate,
    ResourceType,
)
from codex_web.services.projects import ProjectService
from codex_web.services.resources import (
    ResourceCatalogService,
    ResourceNotFoundError,
)
from codex_web.services.thread_execution_settings import (
    ThreadExecutionSettingsService,
)
from codex_web.storage.legacy_project_migration import LegacyProjectMigrationStore


class LegacyProjectMigrationError(RuntimeError):
    pass


class LegacyProjectMigrationApprovalRequired(LegacyProjectMigrationError):
    pass


class LegacyProjectMigrationPlanStale(LegacyProjectMigrationError):
    pass


class LegacyProjectMigrationBlocked(LegacyProjectMigrationError):
    pass


class LegacyProjectMigrationService:
    """Deterministic legacy Project/thread -> canonical execution migration."""

    def __init__(
        self,
        *,
        projects: ProjectService,
        resources: ResourceCatalogService,
        thread_settings: ThreadExecutionSettingsService,
        load_threads: Callable[[], list[IndexedThread]],
        load_bindings: Callable[[], list[BotBinding]],
        store: LegacyProjectMigrationStore,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.projects = projects
        self.resources = resources
        self.thread_settings = thread_settings
        self.load_threads = load_threads
        self.load_bindings = load_bindings
        self.store = store
        self.clock = clock

    @staticmethod
    def _safe_root(project: Project) -> Path:
        root = Path(project.path).resolve(strict=True)
        if not root.is_dir():
            raise LegacyProjectMigrationError("project path is not a directory")
        return root

    @staticmethod
    def _git_root(path: Path) -> bool:
        marker = path / ".git"
        return marker.is_dir() or marker.is_file()

    def _discover_repositories(self, project: Project) -> list[Path]:
        root = self._safe_root(project)
        discovered: list[Path] = []
        if self._git_root(root):
            discovered.append(root)

        for current, dirs, _files in os.walk(root, followlinks=False):
            current_path = Path(current)
            # Never traverse Git metadata or symlinked directories.
            dirs[:] = [
                name
                for name in dirs
                if name != ".git" and not (current_path / name).is_symlink()
            ]
            if current_path == root:
                pass
            elif self._git_root(current_path):
                resolved = current_path.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    continue
                discovered.append(resolved)
                dirs[:] = []

        unique = sorted(
            {path.resolve() for path in discovered},
            key=lambda item: (len(item.parts), str(item)),
        )
        return unique

    @staticmethod
    def _repository_key(root: Path, path: Path) -> str:
        if path == root:
            return "root"
        relative = path.relative_to(root)
        raw = "-".join(relative.parts).casefold()
        normalized = "".join(
            ch if ch.isalnum() or ch in "-._" else "-"
            for ch in raw
        ).strip("-._")
        return normalized or hashlib.sha256(str(relative).encode()).hexdigest()[:12]

    def _existing_repository_resource(
        self,
        *,
        project: Project,
        path: Path,
        actor: AuthenticationActor,
    ):
        absolute = str(path.resolve())
        try:
            resource = self.resources.resolve(
                absolute,
                actor=actor,
                expected_type=ResourceType.REPOSITORY,
                alias_namespace="filesystem",
            )
        except ResourceNotFoundError:
            return None
        bound_ids = {
            item.id
            for item in self.resources.project_resources(project, actor=actor)
        }
        return resource if resource.id in bound_ids else resource

    @staticmethod
    def _thread_bindings(
        bindings: list[BotBinding],
        thread_id: str,
        project_id: str,
    ) -> tuple[BotBinding, ...]:
        return tuple(
            item
            for item in bindings
            if item.thread_id == thread_id and item.project_id == project_id
        )

    @staticmethod
    def _orchestration_hint(bindings: tuple[BotBinding, ...]) -> bool:
        for binding in bindings:
            labels = {
                str(binding.thread_name or "").strip().casefold(),
                str(binding.route_prefix or "").strip().casefold(),
            }
            if binding.is_master or labels.intersection(
                {"orchestrator", "codex", "coordination"}
            ):
                return True
        return False

    @staticmethod
    def _path_repository(
        value: str | None,
        repositories: tuple[LegacyRepositoryProposal, ...],
    ) -> LegacyRepositoryProposal | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            path = Path(raw).resolve(strict=False)
        except Exception:
            return None
        matches = []
        for item in repositories:
            repo = Path(item.absolute_path)
            if path == repo or path.is_relative_to(repo):
                matches.append(item)
        if not matches:
            return None
        return max(matches, key=lambda item: len(Path(item.absolute_path).parts))

    @staticmethod
    def _authority_difference(
        settings: ThreadRunSettings,
        *,
        proposed_sandbox: str | None,
        proposed_profile: str | None,
        proposed_repository_key: str | None,
    ) -> AuthorityDifference:
        summary: list[str] = []
        material = False
        approval = False
        if settings.sandbox == "danger-full-access":
            # A legacy native thread may have had materially broader host authority
            # than current contained danger-full-access. Never claim equivalence
            # unless canonical repository/profile scope was already explicit.
            if not settings.repository_resource_id or not settings.execution_profile_id:
                material = True
                approval = True
                summary.append(
                    "Legacy danger-full-access effective host/filesystem authority "
                    "cannot be proven equivalent to current contained worker semantics."
                )
            if proposed_repository_key:
                summary.append(
                    "Current danger-full-access remains contained to the selected "
                    "canonical repository/workspace boundary."
                )
        if settings.execution_profile_id != proposed_profile:
            material = True
            summary.append(
                f"Execution profile changes from "
                f"{settings.execution_profile_id or 'legacy/unprofiled'} to "
                f"{proposed_profile or 'none'}."
            )
        if settings.repository_resource_id is None and proposed_repository_key:
            summary.append(
                "Mutable repository authority becomes explicit and project-scoped."
            )
        if settings.sandbox and proposed_sandbox and settings.sandbox != proposed_sandbox:
            material = True
            approval = True
            summary.append(
                f"Sandbox changes from {settings.sandbox} to {proposed_sandbox}."
            )
        return AuthorityDifference(
            material_change=material,
            summary=tuple(summary),
            requires_operator_approval=approval,
        )

    def _repository_proposals(
        self,
        project: Project,
        actor: AuthenticationActor,
    ) -> tuple[LegacyRepositoryProposal, ...]:
        root = self._safe_root(project)
        paths = self._discover_repositories(project)
        result = []
        for path in paths:
            key = self._repository_key(root, path)
            existing = self._existing_repository_resource(
                project=project,
                path=path,
                actor=actor,
            )
            result.append(
                LegacyRepositoryProposal(
                    key=key,
                    name=path.name or project.name,
                    absolute_path=str(path),
                    relative_path=(
                        "."
                        if path == root
                        else str(path.relative_to(root))
                    ),
                    existing_resource_id=(existing.id if existing else None),
                    proposed_resource_id=(existing.id if existing else None),
                    default_candidate=len(paths) == 1,
                )
            )
        return tuple(result)

    def _thread_proposal(
        self,
        *,
        thread: IndexedThread,
        settings: ThreadRunSettings,
        bindings: tuple[BotBinding, ...],
        repositories: tuple[LegacyRepositoryProposal, ...],
    ) -> LegacyThreadProposal:
        orchestration = self._orchestration_hint(bindings)
        current_sandbox = settings.sandbox or (
            bindings[0].sandbox if bindings else None
        )
        indexed_repo = self._path_repository(
            thread.cwd or thread.path,
            repositories,
        )

        proposed_profile = settings.execution_profile_id
        proposed_repo = None
        reason_code = "thread_unchanged"
        reason = "Thread already has canonical execution scope."
        disposition = MigrationDisposition.UNCHANGED

        if orchestration:
            proposed_profile = "orchestration-only"
            proposed_repo = None
            reason_code = "orchestration_profile"
            reason = (
                "Master/orchestrator bot binding maps deterministically to the "
                "canonical orchestration-only profile."
            )
            if settings.execution_profile_id != proposed_profile or settings.repository_resource_id:
                disposition = MigrationDisposition.CONVERT
        else:
            proposed_profile = proposed_profile or "repository-write"
            if settings.repository_resource_id:
                matching = next(
                    (
                        item
                        for item in repositories
                        if item.existing_resource_id
                        == settings.repository_resource_id
                    ),
                    None,
                )
                if matching is not None:
                    proposed_repo = matching
                else:
                    return LegacyThreadProposal(
                        thread_id=thread.id,
                        name=thread.name,
                        indexed_cwd=thread.cwd or thread.path,
                        bot_binding_ids=tuple(item.id for item in bindings),
                        current_sandbox=current_sandbox,
                        current_repository_resource_id=settings.repository_resource_id,
                        current_execution_profile_id=settings.execution_profile_id,
                        proposed_sandbox=current_sandbox,
                        proposed_execution_profile_id=proposed_profile,
                        disposition=MigrationDisposition.BLOCKED,
                        reason_code="repository_target_unresolved",
                        reason=(
                            "Existing thread repository target is not one of the "
                            "discovered/bound Project repositories."
                        ),
                    )
            elif indexed_repo is not None:
                proposed_repo = indexed_repo
                disposition = MigrationDisposition.CONVERT
                reason_code = "repository_from_thread_path"
                reason = (
                    "Thread cwd/path maps uniquely to a discovered Git repository."
                )
            elif len(repositories) == 1:
                proposed_repo = repositories[0]
                disposition = MigrationDisposition.CONVERT
                reason_code = "single_repository"
                reason = "Project has exactly one discovered repository."
            elif len(repositories) == 0:
                return LegacyThreadProposal(
                    thread_id=thread.id,
                    name=thread.name,
                    indexed_cwd=thread.cwd or thread.path,
                    bot_binding_ids=tuple(item.id for item in bindings),
                    current_sandbox=current_sandbox,
                    current_repository_resource_id=settings.repository_resource_id,
                    current_execution_profile_id=settings.execution_profile_id,
                    proposed_sandbox=current_sandbox,
                    proposed_execution_profile_id=proposed_profile,
                    disposition=MigrationDisposition.BLOCKED,
                    reason_code="repository_target_missing",
                    reason="Project contains no discovered Git repository.",
                )
            else:
                return LegacyThreadProposal(
                    thread_id=thread.id,
                    name=thread.name,
                    indexed_cwd=thread.cwd or thread.path,
                    bot_binding_ids=tuple(item.id for item in bindings),
                    current_sandbox=current_sandbox,
                    current_repository_resource_id=settings.repository_resource_id,
                    current_execution_profile_id=settings.execution_profile_id,
                    proposed_sandbox=current_sandbox,
                    proposed_execution_profile_id=proposed_profile,
                    disposition=MigrationDisposition.BLOCKED,
                    reason_code="repository_target_ambiguous",
                    reason=(
                        "Thread has no unique repository path/target and Project "
                        "contains multiple repositories."
                    ),
                )

        proposed_sandbox = current_sandbox or "workspace-write"
        difference = self._authority_difference(
            settings,
            proposed_sandbox=proposed_sandbox,
            proposed_profile=proposed_profile,
            proposed_repository_key=(proposed_repo.key if proposed_repo else None),
        )
        if difference.requires_operator_approval:
            disposition = MigrationDisposition.APPROVAL_REQUIRED
            reason_code = "authority_change_requires_approval"
            reason = (
                "Migration changes or cannot prove equivalence of effective "
                "execution authority; explicit operator approval is required."
            )
        elif disposition == MigrationDisposition.UNCHANGED and (
            settings.execution_profile_id != proposed_profile
            or (
                proposed_repo
                and settings.repository_resource_id
                != proposed_repo.existing_resource_id
            )
        ):
            disposition = MigrationDisposition.CONVERT

        return LegacyThreadProposal(
            thread_id=thread.id,
            name=thread.name,
            indexed_cwd=thread.cwd or thread.path,
            bot_binding_ids=tuple(item.id for item in bindings),
            current_sandbox=current_sandbox,
            current_repository_resource_id=settings.repository_resource_id,
            current_execution_profile_id=settings.execution_profile_id,
            proposed_sandbox=proposed_sandbox,
            proposed_repository_key=(proposed_repo.key if proposed_repo else None),
            proposed_repository_resource_id=(
                proposed_repo.existing_resource_id if proposed_repo else None
            ),
            proposed_execution_profile_id=proposed_profile,
            disposition=disposition,
            reason_code=reason_code,
            reason=reason,
            authority_difference=difference,
        )

    @staticmethod
    def _plan_id(payload: dict) -> str:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return "migration-plan-" + hashlib.sha256(canonical).hexdigest()[:24]

    def plan(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
    ) -> LegacyMigrationPlan:
        project = self.projects.get(project_id, actor.tenant)
        repositories = self._repository_proposals(project, actor)
        settings = self.thread_settings.all()
        bindings = self.load_bindings()
        threads = []

        indexed = list(self.load_threads())
        indexed_ids = {item.id for item in indexed}
        # Bot-bound persistent threads are part of the migration even if the
        # local thread index cache has not materialized them yet.
        for binding in bindings:
            if binding.project_id != project.id or binding.thread_id in indexed_ids:
                continue
            indexed.append(
                IndexedThread(
                    id=binding.thread_id,
                    name=binding.thread_name or binding.route_prefix or binding.thread_id,
                )
            )
            indexed_ids.add(binding.thread_id)

        for thread in sorted(indexed, key=lambda item: item.id):
            thread_bindings = self._thread_bindings(
                bindings,
                thread.id,
                project.id,
            )
            path_repo = self._path_repository(
                thread.cwd or thread.path,
                repositories,
            )
            if not thread_bindings and path_repo is None:
                # No evidence that this persistent thread belongs to this Project.
                continue
            threads.append(
                self._thread_proposal(
                    thread=thread,
                    settings=settings.get(thread.id, ThreadRunSettings()),
                    bindings=thread_bindings,
                    repositories=repositories,
                )
            )

        blockers = tuple(
            f"{item.thread_id}:{item.reason_code}"
            for item in threads
            if item.disposition == MigrationDisposition.BLOCKED
        )
        raw = {
            "organization_id": actor.organization_id,
            "workspace_id": actor.workspace_id,
            "project_id": project.id,
            "project_path": str(self._safe_root(project)),
            "repositories": [
                item.model_dump(mode="json") for item in repositories
            ],
            "threads": [item.model_dump(mode="json") for item in threads],
            "blockers": blockers,
        }
        return LegacyMigrationPlan(
            id=self._plan_id(raw),
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=project.id,
            project_path=raw["project_path"],
            repositories=repositories,
            threads=tuple(threads),
            blockers=blockers,
            generated_at=self.clock(),
        )

    def _resource_for_proposal(
        self,
        *,
        project: Project,
        proposal: LegacyRepositoryProposal,
        actor: AuthenticationActor,
    ):
        try:
            resource = self.resources.resolve(
                proposal.absolute_path,
                actor=actor,
                expected_type=ResourceType.REPOSITORY,
                alias_namespace="filesystem",
            )
        except ResourceNotFoundError:
            resource = self.resources.create(
                ResourceCreate(
                    resource_type=ResourceType.REPOSITORY,
                    name=proposal.name,
                    description=(
                        "Canonical repository materialized by legacy Project migration."
                    ),
                    aliases=[
                        ResourceAlias(
                            namespace="filesystem",
                            value=proposal.absolute_path,
                        ),
                        ResourceAlias(
                            namespace="legacy-path",
                            value=proposal.absolute_path,
                        ),
                    ],
                ),
                actor=actor,
            )
        self.resources.bind_project(
            project=project,
            resource_id=resource.id,
            actor=actor,
        )
        return resource

    def apply(
        self,
        plan: LegacyMigrationPlan,
        *,
        actor: AuthenticationActor,
        approve_material_authority_changes: bool = False,
        compatibility_window_seconds: int = 7 * 24 * 60 * 60,
        fail_after_operations: int | None = None,
    ) -> LegacyMigrationExecution:
        current = self.plan(plan.project_id, actor=actor)
        if current.id != plan.id:
            raise LegacyProjectMigrationPlanStale(
                "migration plan is stale; run dry-run again before apply"
            )
        if plan.blockers:
            raise LegacyProjectMigrationBlocked(
                "migration has blocked thread mappings: " + ", ".join(plan.blockers)
            )
        if plan.requires_approval and not approve_material_authority_changes:
            raise LegacyProjectMigrationApprovalRequired(
                "material authority changes require explicit operator approval"
            )
        if compatibility_window_seconds < 0:
            raise LegacyProjectMigrationError(
                "compatibility window must be zero or positive"
            )

        state = self.store.load()
        existing = next(
            (
                item
                for item in state.executions
                if item.plan_id == plan.id
                and item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
            ),
            None,
        )
        now = self.clock()
        execution = existing or LegacyMigrationExecution(
            plan_id=plan.id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=plan.project_id,
            plan=plan,
            approved_by=(
                actor.identity_id if approve_material_authority_changes else None
            ),
            approved_at=(
                now if approve_material_authority_changes else None
            ),
            status=MigrationApplyStatus.PLANNED,
            created_at=now,
            updated_at=now,
        )
        if execution.status == MigrationApplyStatus.APPLIED:
            return execution

        def persist(updated: LegacyMigrationExecution) -> LegacyMigrationExecution:
            def mutate(current_state):
                current_state.executions = [
                    item
                    for item in current_state.executions
                    if item.id != updated.id
                    and not (
                        item.plan_id == updated.plan_id
                        and item.organization_id == updated.organization_id
                        and item.workspace_id == updated.workspace_id
                    )
                ]
                current_state.executions.append(updated)
                return current_state
            saved = self.store.update(mutate)
            return next(item for item in saved.executions if item.id == updated.id)

        execution.status = MigrationApplyStatus.APPLYING
        execution.updated_at = now
        execution = persist(execution)
        completed = set(execution.applied_operation_ids)
        mappings = {
            item.legacy_path: item
            for item in execution.compatibility_mappings
        }
        operations_run = 0

        def checkpoint(operation_id: str) -> None:
            nonlocal execution, operations_run
            if operation_id in completed:
                return
            completed.add(operation_id)
            operations_run += 1
            execution.applied_operation_ids = tuple(sorted(completed))
            execution.updated_at = self.clock()
            execution = persist(execution)
            if (
                fail_after_operations is not None
                and operations_run >= fail_after_operations
            ):
                execution.status = MigrationApplyStatus.PARTIAL
                execution.error = "injected interruption after operation checkpoint"
                execution.updated_at = self.clock()
                execution = persist(execution)
                raise LegacyProjectMigrationError(execution.error)

        project = self.projects.get(plan.project_id, actor.tenant)
        repository_ids: dict[str, str] = {}
        try:
            for proposal in plan.repositories:
                operation_id = f"repository:{proposal.key}"
                resource = self._resource_for_proposal(
                    project=project,
                    proposal=proposal,
                    actor=actor,
                )
                repository_ids[proposal.key] = resource.id
                if compatibility_window_seconds > 0:
                    mappings[proposal.absolute_path] = LegacyPathCompatibilityMapping(
                        legacy_path=proposal.absolute_path,
                        resource_id=resource.id,
                        canonical_path=proposal.absolute_path,
                        expires_at=self.clock() + compatibility_window_seconds,
                    )
                    execution.compatibility_mappings = tuple(
                        mappings[key] for key in sorted(mappings)
                    )
                checkpoint(operation_id)

            for proposal in plan.threads:
                operation_id = f"thread:{proposal.thread_id}"
                if operation_id in completed:
                    continue
                if proposal.disposition == MigrationDisposition.BLOCKED:
                    raise LegacyProjectMigrationBlocked(
                        f"thread {proposal.thread_id} remains blocked"
                    )
                repository_id = (
                    repository_ids.get(proposal.proposed_repository_key)
                    if proposal.proposed_repository_key
                    else None
                )
                self.thread_settings.remember(
                    proposal.thread_id,
                    sandbox=proposal.proposed_sandbox,
                    repository_resource_id=(
                        repository_id
                        if proposal.proposed_execution_profile_id
                        != "orchestration-only"
                        else ""
                    ),
                    read_only_repository_resource_ids=(),
                    execution_profile_id=proposal.proposed_execution_profile_id,
                )
                checkpoint(operation_id)

            execution.status = MigrationApplyStatus.APPLIED
            execution.error = None
            execution.updated_at = self.clock()
            execution = persist(execution)
            return execution
        except Exception as exc:
            if execution.status != MigrationApplyStatus.PARTIAL:
                execution.status = MigrationApplyStatus.PARTIAL
                execution.error = str(exc)[:1000]
                execution.updated_at = self.clock()
                persist(execution)
            raise

    def status(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[LegacyMigrationExecution, ...]:
        rows = [
            item
            for item in self.store.load().executions
            if item.project_id == project_id
            and item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        ]
        return tuple(
            sorted(rows, key=lambda item: (item.updated_at, item.id), reverse=True)
        )

    def resolve_legacy_path(
        self,
        path: str,
        *,
        actor: AuthenticationActor,
    ) -> LegacyPathCompatibilityMapping | None:
        now = self.clock()
        candidates = [
            mapping
            for execution in self.store.load().executions
            if execution.organization_id == actor.organization_id
            and execution.workspace_id == actor.workspace_id
            for mapping in execution.compatibility_mappings
            if mapping.legacy_path == path and mapping.expires_at > now
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.created_at)
