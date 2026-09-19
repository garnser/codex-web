from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any

from codex_web.action_intents import (
    ActionIntent,
    ActionIntentStatus,
    TERMINAL_ACTION_INTENT_STATUSES,
)
from codex_web.approval_requests import (
    ApprovalConsumeRequest,
    ApprovalRequestCreate,
    ApprovalRequestStatus,
    ApprovalRequirement,
    ApprovalTarget,
)
from codex_web.artifact_evidence import (
    EvidenceCreate,
    EvidenceResult,
    EvidenceType,
)
from codex_web.compatibility import (
    API_CONTRACT,
    CANONICAL_EVENT_CONTRACT,
    ContractVersion,
)
from codex_web.definitions import (
    DefinitionLifecycle,
    definition_is_effective,
)
from codex_web.execution_workers import AssignmentStatus, WorkerLifecycle
from codex_web.extensions import ExtensionLifecycleState
from codex_web.identity import AuthenticationActor
from codex_web.releases import ReleaseStatus
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.approval_requests import ApprovalRequestService
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.extensions import (
    ExtensionService,
    extension_version_satisfies,
)
from codex_web.services.recovery import RecoveryService
from codex_web.services.releases import ReleaseService
from codex_web.storage.state_store import StateStore
from codex_web.storage.upgrades import UpgradeNotFoundError, UpgradeStore
from codex_web.upgrades import (
    DefinitionMigrationRecord,
    UpgradeDefinitionBaseline,
    UpgradeDefinitionMigrationCreate,
    UpgradePhase,
    UpgradePlan,
    UpgradePlanCreate,
    UpgradePreflight,
    UpgradeStatus,
    UpgradeStep,
    UpgradeStepExecute,
    UpgradeStepStatus,
)


class UpgradeError(RuntimeError):
    pass


class UpgradeConflictError(UpgradeError):
    pass


class UpgradePreflightError(UpgradeConflictError):
    pass


MigrationHandler = Callable[
    [UpgradePlan, UpgradeStep, AuthenticationActor],
    dict[str, str | int | float | bool | None] | None,
]


class UpgradeService:
    def __init__(
        self,
        store: UpgradeStore,
        *,
        state_store: StateStore,
        definitions: DefinitionRegistryService,
        workers: ExecutionWorkerService,
        extensions: ExtensionService,
        recovery: RecoveryService,
        releases: ReleaseService,
        action_intents: ActionIntentService,
        approvals: ApprovalRequestService,
        evidence: ArtifactEvidenceService,
        clock=time.time,
    ) -> None:
        self.store = store
        self.state_store = state_store
        self.definitions = definitions
        self.workers = workers
        self.extensions = extensions
        self.recovery = recovery
        self.releases = releases
        self.action_intents = action_intents
        self.approvals = approvals
        self.evidence = evidence
        self.clock = clock
        self._migration_handlers: dict[str, MigrationHandler] = {}

    @staticmethod
    def _same_scope(plan: UpgradePlan, actor: AuthenticationActor) -> bool:
        return (
            plan.organization_id == actor.organization_id
            and plan.workspace_id == actor.workspace_id
        )

    def register_migration_handler(
        self,
        handler_id: str,
        handler: MigrationHandler,
    ) -> None:
        key = str(handler_id or "").strip()
        if not key:
            raise ValueError("upgrade migration handler id must not be empty")
        if key in self._migration_handlers:
            raise UpgradeConflictError(
                f"upgrade migration handler already registered: {key}"
            )
        self._migration_handlers[key] = handler

    def list(self, actor: AuthenticationActor) -> list[UpgradePlan]:
        return self.store.list(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )

    def get(self, plan_id: str, *, actor: AuthenticationActor) -> UpgradePlan:
        try:
            item = self.store.get(plan_id)
        except UpgradeNotFoundError as exc:
            raise UpgradeError("upgrade plan not found") from exc
        if not self._same_scope(item, actor):
            raise UpgradeError("upgrade plan not found")
        return item

    def _update(self, plan_id: str, actor: AuthenticationActor, updater) -> UpgradePlan:
        result: list[UpgradePlan] = []

        def apply(state):
            current = state.plans.get(plan_id)
            if current is None or not self._same_scope(current, actor):
                raise UpgradeError("upgrade plan not found")
            updated = updater(current)
            state.plans[plan_id] = updated
            result.append(updated)
            return state

        self.store.update(apply)
        return result[0]

    def create(
        self,
        payload: UpgradePlanCreate,
        *,
        actor: AuthenticationActor,
    ) -> UpgradePlan:
        release = self.releases.get(payload.release_id, actor=actor)
        if release.version != payload.target_app_version:
            raise UpgradeConflictError(
                "target Release version does not match upgrade target application version"
            )
        if release.status in {
            ReleaseStatus.BLOCKED,
            ReleaseStatus.SUPERSEDED,
            ReleaseStatus.ROLLED_BACK,
        }:
            raise UpgradeConflictError(
                f"target Release is not eligible for upgrade: {release.status.value}"
            )
        plan = UpgradePlan(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=payload.project_id,
            release_id=payload.release_id,
            current_app_version=payload.current_app_version,
            target_app_version=payload.target_app_version,
            compatibility=payload.compatibility,
            observed_control_plane_versions=tuple(
                dict.fromkeys(payload.observed_control_plane_versions)
            ),
            steps=tuple(
                UpgradeStep.from_create(item)
                for item in payload.steps
            ),
            require_recovery_qualification=payload.require_recovery_qualification,
            require_drain=payload.require_drain,
            rollback_available=(
                payload.compatibility.rollback_supported_to_app_version
                == payload.current_app_version
            ),
            created_by=actor.identity_id,
            created_at=float(self.clock()),
            updated_at=float(self.clock()),
        )

        def apply(state):
            state.plans[plan.id] = plan
            return state

        self.store.update(apply)
        return plan

    @staticmethod
    def _definition_target_compatible(
        record,
        *,
        engine_version: str,
        schemas: dict[str, tuple[str, ...]],
    ) -> bool:
        versions = schemas.get(record.kind, ())
        if record.definition_schema_version not in versions:
            return False
        engine = ContractVersion.parse(engine_version)
        if (
            record.min_engine_version is not None
            and engine < ContractVersion.parse(record.min_engine_version)
        ):
            return False
        if (
            record.max_engine_version is not None
            and engine > ContractVersion.parse(record.max_engine_version)
        ):
            return False
        return True

    def _active_action_count(self, actor: AuthenticationActor) -> int:
        return sum(
            item.status in {
                ActionIntentStatus.CLAIMED,
                ActionIntentStatus.EXECUTING,
                ActionIntentStatus.UNCERTAIN,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
            }
            for item in self.action_intents.list(actor)
        )

    def _active_assignment_count(self, actor: AuthenticationActor) -> int:
        return sum(
            item.status in {
                AssignmentStatus.CLAIMED,
                AssignmentStatus.RUNNING,
            }
            for item in self.workers.list_assignments(actor)
        )

    def preflight(
        self,
        plan_id: str,
        *,
        actor: AuthenticationActor,
    ) -> UpgradePlan:
        plan = self.get(plan_id, actor=actor)
        profile = plan.compatibility
        blockers: list[str] = []
        warnings: list[str] = []

        state_status = self.state_store.status()
        state_schema = int(state_status.get("schemaVersion") or 0)
        if state_schema != profile.source_state_schema_version:
            blockers.append(
                "state_schema_source_mismatch:"
                f"{state_schema}!={profile.source_state_schema_version}"
            )
        if profile.target_state_schema_version < profile.source_state_schema_version:
            blockers.append("state_schema_target_downgrade_not_supported")

        if API_CONTRACT.current not in profile.supported_api_contract_versions:
            blockers.append(
                f"api_contract_unsupported:{API_CONTRACT.current}"
            )
        if (
            CANONICAL_EVENT_CONTRACT.current
            not in profile.supported_event_contract_versions
        ):
            blockers.append(
                "canonical_event_contract_unsupported:"
                f"{CANONICAL_EVENT_CONTRACT.current}"
            )

        allowed_control = set(
            profile.supported_control_plane_versions_during_rollout
        )
        unsupported_control = sorted(
            set(plan.observed_control_plane_versions) - allowed_control
        )
        blockers.extend(
            f"control_plane_version_unsupported:{item}"
            for item in unsupported_control
        )
        if (
            profile.source_app_version
            not in allowed_control
            or profile.target_app_version not in allowed_control
        ):
            blockers.append(
                "rollout_window_must_include_source_and_target_control_plane_versions"
            )

        release = self.releases.get(plan.release_id, actor=actor)
        if release.version != plan.target_app_version:
            blockers.append("target_release_version_changed")
        if release.status in {
            ReleaseStatus.BLOCKED,
            ReleaseStatus.SUPERSEDED,
            ReleaseStatus.ROLLED_BACK,
        }:
            blockers.append(
                f"target_release_not_eligible:{release.status.value}"
            )

        recovery_health = self.recovery.health(actor=actor)
        if plan.require_recovery_qualification and not recovery_health.recovery_qualified:
            blockers.append("recovery_not_qualified")
            blockers.extend(
                f"recovery:{item}"
                for item in recovery_health.blockers
            )

        baseline: list[UpgradeDefinitionBaseline] = []
        incompatible_definitions: list[str] = []
        for record in self.definitions.list_records():
            if (
                record.lifecycle != DefinitionLifecycle.PUBLISHED
                or not definition_is_effective(
                    record,
                    now=float(self.clock()),
                )
            ):
                continue
            baseline.append(
                UpgradeDefinitionBaseline(
                    record_id=record.record_id,
                    definition_id=record.definition_id,
                    kind=record.kind,
                    revision=record.revision,
                    definition_schema_version=record.definition_schema_version,
                    checksum=record.checksum,
                )
            )
            if not self._definition_target_compatible(
                record,
                engine_version=profile.target_definition_engine_version,
                schemas=profile.target_definition_schemas,
            ):
                incompatible_definitions.append(record.record_id)
        blockers.extend(
            f"definition_incompatible:{item}"
            for item in incompatible_definitions
        )

        incompatible_workers: list[str] = []
        allowed_worker_versions = set(
            profile.supported_worker_versions_during_rollout
        )
        for worker in self.workers.list_workers(actor):
            if worker.lifecycle in {
                WorkerLifecycle.REVOKED,
                WorkerLifecycle.OFFLINE,
            }:
                continue
            if (
                allowed_worker_versions
                and worker.version not in allowed_worker_versions
            ):
                incompatible_workers.append(worker.id)
        blockers.extend(
            f"worker_version_incompatible:{item}"
            for item in incompatible_workers
        )

        for assignment in self.workers.list_assignments(actor):
            if assignment.status in {
                AssignmentStatus.CANCELLED,
                AssignmentStatus.FAILED,
                AssignmentStatus.SUCCEEDED,
                AssignmentStatus.LOST,
            }:
                continue
            if (
                assignment.execution_contract_version
                not in profile.supported_execution_contract_versions
            ):
                blockers.append(
                    "assignment_execution_contract_incompatible:"
                    f"{assignment.id}:{assignment.execution_contract_version}"
                )

        incompatible_extensions: list[str] = []
        for installation in self.extensions.list(actor):
            if installation.lifecycle not in {
                ExtensionLifecycleState.ENABLED,
                ExtensionLifecycleState.CONFIGURED,
                ExtensionLifecycleState.UPGRADING,
            }:
                continue
            try:
                compatible = extension_version_satisfies(
                    profile.target_extension_host_version,
                    installation.manifest.compatibility.codex_web,
                )
            except Exception:
                compatible = False
            if not compatible:
                incompatible_extensions.append(installation.id)
            if installation.lifecycle == ExtensionLifecycleState.UPGRADING:
                warnings.append(
                    f"extension_upgrade_in_progress:{installation.id}"
                )
        blockers.extend(
            f"extension_incompatible:{item}"
            for item in incompatible_extensions
        )

        active_actions = self._active_action_count(actor)
        active_assignments = self._active_assignment_count(actor)
        if plan.require_drain and (active_actions or active_assignments):
            blockers.append(
                "active_operations_not_drained:"
                f"actions={active_actions}:assignments={active_assignments}"
            )
        if plan.require_drain and not plan.maintenance_mode:
            warnings.append("maintenance_mode_not_entered")

        preflight_evidence = self.evidence.create_evidence(
            EvidenceCreate(
                project_id=plan.project_id,
                evidence_type=EvidenceType.POLICY_EVALUATION,
                provider="codex-web",
                source="upgrade-preflight",
                result=(
                    EvidenceResult.PASS
                    if not blockers
                    else EvidenceResult.FAIL
                ),
                summary=(
                    f"Upgrade preflight {'passed' if not blockers else 'failed'} "
                    f"for {plan.current_app_version}->{plan.target_app_version}"
                ),
                metadata={
                    "upgrade_plan_id": plan.id,
                    "source_app_version": plan.current_app_version,
                    "target_app_version": plan.target_app_version,
                    "state_schema_version": state_schema,
                    "blocker_count": len(blockers),
                    "active_action_intents": active_actions,
                    "active_worker_assignments": active_assignments,
                    "definition_baseline_count": len(baseline),
                },
            ),
            actor=actor,
        )
        preflight = UpgradePreflight(
            satisfied=not blockers,
            blockers=tuple(dict.fromkeys(blockers)),
            warnings=tuple(dict.fromkeys(warnings)),
            state_schema_version=state_schema,
            recovery_qualified=recovery_health.recovery_qualified,
            active_action_intents=active_actions,
            active_worker_assignments=active_assignments,
            incompatible_worker_ids=tuple(incompatible_workers),
            incompatible_extension_ids=tuple(incompatible_extensions),
            incompatible_definition_record_ids=tuple(
                incompatible_definitions
            ),
            definition_baseline=tuple(baseline),
            evidence_id=preflight_evidence.id,
            evaluated_at=float(self.clock()),
        )
        status = (
            UpgradeStatus.READY
            if preflight.satisfied
            else (
                UpgradeStatus.DRAINING
                if plan.maintenance_mode
                else UpgradeStatus.PREFLIGHT_BLOCKED
            )
        )
        return self._update(
            plan.id,
            actor,
            lambda current: current.model_copy(
                update={
                    "preflight": preflight,
                    "status": status,
                    "updated_at": float(self.clock()),
                }
            ),
        )

    def start_drain(
        self,
        plan_id: str,
        *,
        actor: AuthenticationActor,
    ) -> UpgradePlan:
        plan = self.get(plan_id, actor=actor)
        if plan.status in {
            UpgradeStatus.COMPLETED,
            UpgradeStatus.ROLLED_BACK,
        }:
            raise UpgradeConflictError("terminal upgrade cannot enter maintenance")
        now = float(self.clock())
        return self._update(
            plan.id,
            actor,
            lambda current: current.model_copy(
                update={
                    "maintenance_mode": True,
                    "drain_started_at": current.drain_started_at or now,
                    "status": UpgradeStatus.DRAINING,
                    "updated_at": now,
                }
            ),
        )

    def _maintenance_plan(
        self,
        organization_id: str,
        workspace_id: str,
    ) -> UpgradePlan | None:
        candidates = [
            item
            for item in self.store.load().plans.values()
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
            and item.maintenance_mode
            and item.status not in {
                UpgradeStatus.COMPLETED,
                UpgradeStatus.ROLLED_BACK,
            }
        ]
        return max(
            candidates,
            key=lambda item: (item.created_at, item.id),
            default=None,
        )

    def action_execution_allowed(self, intent: ActionIntent) -> bool:
        plan = self._maintenance_plan(
            intent.organization_id,
            intent.workspace_id,
        )
        if plan is None:
            return True
        parameters = intent.request.parameters
        return bool(
            parameters.get("incident_id")
            or parameters.get("recovery") is True
            or "rollback" in intent.action_id.casefold()
            or "reconcile" in intent.action_id.casefold()
        )

    def worker_assignment_allowed(
        self,
        organization_id: str,
        workspace_id: str,
    ) -> bool:
        return self._maintenance_plan(
            organization_id,
            workspace_id,
        ) is None

    def capture_pre_upgrade_backup(
        self,
        plan_id: str,
        *,
        actor: AuthenticationActor,
    ) -> UpgradePlan:
        plan = self.get(plan_id, actor=actor)
        manifest = self.recovery.create_backup(actor=actor)
        return self._update(
            plan.id,
            actor,
            lambda current: current.model_copy(
                update={
                    "pre_upgrade_backup_id": manifest.id,
                    "updated_at": float(self.clock()),
                }
            ),
        )

    def _approval_target(
        self,
        plan: UpgradePlan,
        step: UpgradeStep,
        *,
        actor: AuthenticationActor,
    ) -> ApprovalTarget:
        release = self.releases.get(plan.release_id, actor=actor)
        return ApprovalTarget(
            operation="upgrade.irreversible_step",
            object_type="upgrade_step",
            object_id=f"{plan.id}:{step.id}",
            target_version=plan.target_app_version,
            target_digest=release.build.digest,
        )

    async def request_step_approval(
        self,
        plan_id: str,
        step_id: str,
        *,
        actor: AuthenticationActor,
    ) -> UpgradePlan:
        plan = self.get(plan_id, actor=actor)
        step = next((item for item in plan.steps if item.id == step_id), None)
        if step is None:
            raise UpgradeError("upgrade step not found")
        if not step.irreversible:
            raise UpgradeConflictError(
                "approval is reserved for declared irreversible upgrade steps"
            )
        if step.requires_backup and plan.pre_upgrade_backup_id is None:
            raise UpgradeConflictError(
                "irreversible upgrade step requires a pre-upgrade backup"
            )
        target = self._approval_target(plan, step, actor=actor)
        request = await self.approvals.create(
            ApprovalRequestCreate(
                target=target,
                project_id=plan.project_id,
                reason=(
                    f"Authorize irreversible upgrade step {step.id}: "
                    f"{step.description}"
                ),
                policy_source="canonical:safe-upgrade",
                authority_source="canonical:role-authority",
                requirement=ApprovalRequirement(
                    quorum=1,
                    distinct_humans=True,
                    allow_self_approval=False,
                ),
                evidence_refs=(
                    (plan.preflight.evidence_id,)
                    if plan.preflight is not None
                    and plan.preflight.evidence_id is not None
                    else ()
                ),
            ),
            requester=actor,
        )
        updated_step = step.model_copy(
            update={
                "approval_request_id": request.id,
                "status": UpgradeStepStatus.AWAITING_APPROVAL,
            }
        )
        return self._replace_step(plan, updated_step, actor)

    def _replace_step(
        self,
        plan: UpgradePlan,
        step: UpgradeStep,
        actor: AuthenticationActor,
        *,
        extra: dict[str, Any] | None = None,
    ) -> UpgradePlan:
        values = {
            "steps": tuple(
                step if item.id == step.id else item
                for item in plan.steps
            ),
            "updated_at": float(self.clock()),
        }
        values.update(extra or {})
        return self._update(
            plan.id,
            actor,
            lambda current: current.model_copy(update=values),
        )

    @staticmethod
    def _prior_steps_complete(
        plan: UpgradePlan,
        step: UpgradeStep,
    ) -> bool:
        for item in plan.steps:
            if item.id == step.id:
                return True
            if item.status not in {
                UpgradeStepStatus.SUCCEEDED,
                UpgradeStepStatus.SKIPPED,
            }:
                return False
        return False

    async def execute_step(
        self,
        plan_id: str,
        step_id: str,
        payload: UpgradeStepExecute,
        *,
        actor: AuthenticationActor,
    ) -> UpgradePlan:
        plan = self.get(plan_id, actor=actor)
        if plan.preflight is None or not plan.preflight.satisfied:
            raise UpgradePreflightError(
                "upgrade preflight must pass before migration execution"
            )
        step = next((item for item in plan.steps if item.id == step_id), None)
        if step is None:
            raise UpgradeError("upgrade step not found")
        if not self._prior_steps_complete(plan, step):
            raise UpgradeConflictError(
                "upgrade steps must execute in declared order"
            )
        if step.status == UpgradeStepStatus.SUCCEEDED:
            if step.result.get("idempotency_key") == payload.idempotency_key:
                return plan
            raise UpgradeConflictError("upgrade step already completed")
        if step.attempts > 0 and not step.idempotent:
            raise UpgradeConflictError(
                "non-idempotent failed upgrade step cannot be replayed automatically"
            )
        if step.requires_drain and not plan.maintenance_mode:
            raise UpgradeConflictError(
                "upgrade step requires maintenance/drain mode"
            )
        if step.requires_backup and plan.pre_upgrade_backup_id is None:
            raise UpgradeConflictError(
                "upgrade step requires pre-upgrade backup"
            )

        if step.irreversible:
            if step.approval_request_id is None:
                raise UpgradeConflictError(
                    "irreversible upgrade step requires canonical approval"
                )
            approval = self.approvals.get(
                step.approval_request_id,
                actor=actor,
            )
            if approval.status not in {
                ApprovalRequestStatus.APPROVED,
                ApprovalRequestStatus.CONSUMED,
            }:
                raise UpgradeConflictError(
                    "irreversible upgrade approval is not approved/consumed for resume"
                )
            target = self._approval_target(plan, step, actor=actor)
            await self.approvals.consume(
                approval.id,
                ApprovalConsumeRequest(
                    target=target,
                    idempotency_key=(
                        f"upgrade-step:{plan.id}:{step.id}:"
                        f"{payload.idempotency_key}"
                    ),
                    resulting_operation_reference=(
                        f"{plan.id}:{step.id}"
                    ),
                ),
                actor=actor,
            )

        now = float(self.clock())
        running = step.model_copy(
            update={
                "status": UpgradeStepStatus.RUNNING,
                "attempts": step.attempts + 1,
                "started_at": step.started_at or now,
                "last_error": None,
            }
        )
        plan = self._replace_step(
            plan,
            running,
            actor,
            extra={
                "status": (
                    UpgradeStatus.VERIFYING
                    if step.phase == UpgradePhase.VERIFY
                    else UpgradeStatus.IN_PROGRESS
                )
            },
        )

        handler = (
            self._migration_handlers.get(step.handler_id)
            if step.handler_id is not None
            else None
        )
        try:
            result = (
                handler(plan, running, actor)
                if handler is not None
                else {}
            )
            if inspect.isawaitable(result):
                result = await result
            values = dict(result or {})
            values["idempotency_key"] = payload.idempotency_key
        except Exception as exc:
            failure_evidence = self.evidence.create_evidence(
                EvidenceCreate(
                    project_id=plan.project_id,
                    evidence_type=EvidenceType.POLICY_EVALUATION,
                    provider="codex-web",
                    source="upgrade-step-verification",
                    result=EvidenceResult.FAIL,
                    summary=(
                        f"Upgrade step {running.id} failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    metadata={
                        "upgrade_plan_id": plan.id,
                        "step_id": running.id,
                        "phase": running.phase.value,
                        "kind": running.kind.value,
                        "attempt": running.attempts,
                    },
                ),
                actor=actor,
            )
            failed = running.model_copy(
                update={
                    "status": UpgradeStepStatus.FAILED,
                    "last_error": f"{type(exc).__name__}: {exc}",
                    "evidence_id": failure_evidence.id,
                    "completed_at": float(self.clock()),
                }
            )
            return self._replace_step(
                plan,
                failed,
                actor,
                extra={"status": UpgradeStatus.FAILED},
            )

        success_evidence = self.evidence.create_evidence(
            EvidenceCreate(
                project_id=plan.project_id,
                evidence_type=EvidenceType.POLICY_EVALUATION,
                provider="codex-web",
                source="upgrade-step-verification",
                result=EvidenceResult.PASS,
                summary=(
                    f"Upgrade step {running.id} completed "
                    f"({running.phase.value}/{running.kind.value})"
                ),
                metadata={
                    "upgrade_plan_id": plan.id,
                    "step_id": running.id,
                    "phase": running.phase.value,
                    "kind": running.kind.value,
                    "attempt": running.attempts,
                    "irreversible": running.irreversible,
                },
            ),
            actor=actor,
        )
        succeeded = running.model_copy(
            update={
                "status": UpgradeStepStatus.SUCCEEDED,
                "result": values,
                "evidence_id": success_evidence.id,
                "completed_at": float(self.clock()),
            }
        )
        extra: dict[str, Any] = {
            "status": (
                UpgradeStatus.VERIFYING
                if step.phase == UpgradePhase.VERIFY
                else UpgradeStatus.IN_PROGRESS
            )
        }
        if step.irreversible:
            extra.update(
                {
                    "irreversible_boundary_crossed": True,
                    "rollback_available": False,
                }
            )
        return self._replace_step(
            plan,
            succeeded,
            actor,
            extra=extra,
        )

    def record_definition_migration(
        self,
        plan_id: str,
        payload: UpgradeDefinitionMigrationCreate,
        *,
        actor: AuthenticationActor,
    ) -> UpgradePlan:
        plan = self.get(plan_id, actor=actor)
        source = self.definitions.get_record(payload.from_record_id)
        target = self.definitions.get_record(payload.to_record_id)
        baseline = {
            item.record_id: item
            for item in (
                plan.preflight.definition_baseline
                if plan.preflight is not None
                else ()
            )
        }
        expected = baseline.get(source.record_id)
        if expected is None or expected.checksum != source.checksum:
            raise UpgradeConflictError(
                "definition migration source is not the frozen preflight baseline"
            )
        if target.lifecycle != DefinitionLifecycle.PUBLISHED:
            raise UpgradeConflictError(
                "definition migration target must be published"
            )
        if (
            source.definition_id != target.definition_id
            or source.kind != target.kind
            or source.scope_type != target.scope_type
            or source.scope_id != target.scope_id
        ):
            raise UpgradeConflictError(
                "definition migration target must preserve canonical definition slot"
            )
        migration = DefinitionMigrationRecord(
            migration_id=payload.migration_id,
            from_record_id=source.record_id,
            from_checksum=source.checksum,
            to_record_id=target.record_id,
            to_checksum=target.checksum,
            reason=payload.reason,
            actor_id=actor.identity_id,
            recorded_at=float(self.clock()),
        )
        return self._update(
            plan.id,
            actor,
            lambda current: current.model_copy(
                update={
                    "definition_migrations": tuple(
                        dict.fromkeys(
                            (*current.definition_migrations, migration)
                        )
                    ),
                    "updated_at": float(self.clock()),
                }
            ),
        )

    def _verify_definition_baseline(
        self,
        plan: UpgradePlan,
    ) -> tuple[str, ...]:
        blockers: list[str] = []
        migrations = {
            item.from_record_id: item
            for item in plan.definition_migrations
        }
        for baseline in (
            plan.preflight.definition_baseline
            if plan.preflight is not None
            else ()
        ):
            source = self.definitions.get_record(baseline.record_id)
            if source.checksum != baseline.checksum:
                blockers.append(
                    f"definition_baseline_checksum_changed:{baseline.record_id}"
                )
                continue
            migration = migrations.get(baseline.record_id)
            if migration is None:
                if not self._definition_target_compatible(
                    source,
                    engine_version=plan.compatibility.target_definition_engine_version,
                    schemas=plan.compatibility.target_definition_schemas,
                ):
                    blockers.append(
                        f"definition_not_target_compatible:{baseline.record_id}"
                    )
                continue
            target = self.definitions.get_record(migration.to_record_id)
            if (
                target.checksum != migration.to_checksum
                or target.lifecycle != DefinitionLifecycle.PUBLISHED
            ):
                blockers.append(
                    f"definition_migration_target_invalid:{target.record_id}"
                )
            elif not self._definition_target_compatible(
                target,
                engine_version=plan.compatibility.target_definition_engine_version,
                schemas=plan.compatibility.target_definition_schemas,
            ):
                blockers.append(
                    f"definition_migration_target_incompatible:{target.record_id}"
                )
        return tuple(blockers)

    def verify_post_upgrade(
        self,
        plan_id: str,
        *,
        actor: AuthenticationActor,
    ) -> UpgradePlan:
        plan = self.get(plan_id, actor=actor)
        blockers: list[str] = []
        if any(
            item.status not in {
                UpgradeStepStatus.SUCCEEDED,
                UpgradeStepStatus.SKIPPED,
            }
            for item in plan.steps
        ):
            blockers.append("upgrade_steps_incomplete")

        state_schema = int(
            self.state_store.status().get("schemaVersion") or 0
        )
        if state_schema != plan.compatibility.target_state_schema_version:
            blockers.append(
                "target_state_schema_not_active:"
                f"{state_schema}!={plan.compatibility.target_state_schema_version}"
            )

        blockers.extend(self._verify_definition_baseline(plan))

        allowed_workers = set(
            plan.compatibility.supported_worker_versions_during_rollout
        )
        for worker in self.workers.list_workers(actor):
            if worker.lifecycle in {
                WorkerLifecycle.REVOKED,
                WorkerLifecycle.OFFLINE,
            }:
                continue
            if allowed_workers and worker.version not in allowed_workers:
                blockers.append(
                    f"worker_version_incompatible:{worker.id}"
                )

        for installation in self.extensions.list(actor):
            if installation.lifecycle != ExtensionLifecycleState.ENABLED:
                continue
            try:
                compatible = extension_version_satisfies(
                    plan.compatibility.target_extension_host_version,
                    installation.manifest.compatibility.codex_web,
                )
            except Exception:
                compatible = False
            if not compatible:
                blockers.append(
                    f"extension_incompatible:{installation.id}"
                )

        passed = not blockers
        evidence = self.evidence.create_evidence(
            EvidenceCreate(
                project_id=plan.project_id,
                evidence_type=EvidenceType.POLICY_EVALUATION,
                provider="codex-web",
                source="upgrade-post-verification",
                result=EvidenceResult.PASS if passed else EvidenceResult.FAIL,
                summary=(
                    f"Upgrade verification {'passed' if passed else 'failed'} "
                    f"for {plan.current_app_version}->{plan.target_app_version}"
                ),
                metadata={
                    "upgrade_plan_id": plan.id,
                    "source_app_version": plan.current_app_version,
                    "target_app_version": plan.target_app_version,
                    "state_schema_version": state_schema,
                    "blocker_count": len(blockers),
                    "irreversible_boundary_crossed": plan.irreversible_boundary_crossed,
                    "rollback_available": plan.rollback_available,
                },
            ),
            actor=actor,
        )
        if not passed:
            return self._update(
                plan.id,
                actor,
                lambda current: current.model_copy(
                    update={
                        "status": UpgradeStatus.FAILED,
                        "post_upgrade_evidence_id": evidence.id,
                        "updated_at": float(self.clock()),
                    }
                ),
            )

        now = float(self.clock())
        return self._update(
            plan.id,
            actor,
            lambda current: current.model_copy(
                update={
                    "status": UpgradeStatus.COMPLETED,
                    "maintenance_mode": False,
                    "post_upgrade_evidence_id": evidence.id,
                    "completed_at": now,
                    "updated_at": now,
                }
            ),
        )

    def mark_rolled_back(
        self,
        plan_id: str,
        *,
        actor: AuthenticationActor,
        reason: str,
    ) -> UpgradePlan:
        plan = self.get(plan_id, actor=actor)
        if plan.irreversible_boundary_crossed or not plan.rollback_available:
            raise UpgradeConflictError(
                "rollback is no longer truthful after the irreversible compatibility boundary"
            )
        if (
            plan.compatibility.rollback_supported_to_app_version
            != plan.current_app_version
        ):
            raise UpgradeConflictError(
                "compatibility profile does not support rollback to source version"
            )
        evidence = self.evidence.create_evidence(
            EvidenceCreate(
                project_id=plan.project_id,
                evidence_type=EvidenceType.POLICY_EVALUATION,
                provider="codex-web",
                source="upgrade-rollback-verification",
                result=EvidenceResult.PASS,
                summary=(
                    f"Upgrade rollback recorded to {plan.current_app_version}: {reason}"
                ),
                metadata={
                    "upgrade_plan_id": plan.id,
                    "source_app_version": plan.current_app_version,
                    "target_app_version": plan.target_app_version,
                    "pre_upgrade_backup_id": plan.pre_upgrade_backup_id,
                },
            ),
            actor=actor,
        )
        now = float(self.clock())
        return self._update(
            plan.id,
            actor,
            lambda current: current.model_copy(
                update={
                    "status": UpgradeStatus.ROLLED_BACK,
                    "maintenance_mode": False,
                    "rollback_evidence_id": evidence.id,
                    "updated_at": now,
                    "completed_at": now,
                }
            ),
        )
