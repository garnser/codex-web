from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.action_intents import ActionIntentStatus
from codex_web.approval_requests import ApprovalRequestStatus
from codex_web.artifact_evidence import EvidenceResult
from codex_web.definitions import (
    DefinitionLifecycle,
    DefinitionRecord,
    DefinitionScope,
    definition_checksum,
)
from codex_web.execution_workers import (
    AssignmentStatus,
    WorkerLifecycle,
)
from codex_web.extensions import ExtensionLifecycleState
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.recovery import RecoveryHealth
from codex_web.releases import ReleaseStatus
from codex_web.services.upgrades import (
    UpgradeConflictError,
    UpgradeService,
)
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.upgrades import UpgradeStore
from codex_web.upgrades import (
    UpgradeCompatibilityProfile,
    UpgradeDeploymentMode,
    UpgradePhase,
    UpgradePlanCreate,
    UpgradeStepCreate,
    UpgradeStepExecute,
    UpgradeStepKind,
    UpgradeStepStatus,
)


class _Evidence:
    def __init__(self):
        self.items = []

    def create_evidence(self, payload, *, actor):
        item = SimpleNamespace(
            id=f"evidence-{len(self.items)+1}",
            payload=payload,
            actor=actor,
        )
        self.items.append(item)
        return item


class _Definitions:
    def __init__(self, records=()):
        self.records = {item.record_id: item for item in records}

    def list_records(self):
        return list(self.records.values())

    def get_record(self, record_id):
        return self.records[record_id]


class _Workers:
    def __init__(self, workers=(), assignments=()):
        self._workers = list(workers)
        self._assignments = list(assignments)

    def list_workers(self, actor):
        del actor
        return list(self._workers)

    def list_assignments(self, actor):
        del actor
        return list(self._assignments)


class _Extensions:
    def __init__(self, installations=()):
        self.installations = list(installations)

    def list(self, actor):
        del actor
        return list(self.installations)


class _Recovery:
    def __init__(self, qualified=True):
        self.qualified = qualified
        self.backup_count = 0

    def health(self, *, actor):
        del actor
        return RecoveryHealth(
            policy_configured=True,
            latest_backup_id="backup-old",
            latest_backup_age_seconds=10,
            latest_restore_verification_id="verify-old",
            latest_restore_verification_age_seconds=10,
            latest_restore_passed=self.qualified,
            rpo_satisfied=self.qualified,
            rto_satisfied=self.qualified,
            recovery_qualified=self.qualified,
            blockers=() if self.qualified else ("recovery_not_ready",),
        )

    def create_backup(self, *, actor):
        del actor
        self.backup_count += 1
        return SimpleNamespace(id=f"backup-{self.backup_count}")


class _Releases:
    def __init__(self, target_version="1.1.0"):
        self.release = SimpleNamespace(
            id="release-target",
            version=target_version,
            status=ReleaseStatus.QUALIFIED,
            build=SimpleNamespace(digest="sha256:" + "a" * 64),
        )

    def get(self, release_id, *, actor):
        del actor
        if release_id != self.release.id:
            raise KeyError(release_id)
        return self.release


class _Actions:
    def __init__(self, intents=()):
        self.intents = list(intents)

    def list(self, actor):
        del actor
        return list(self.intents)


class _Approvals:
    def __init__(self):
        self.created = []
        self.consumed = []
        self.status = ApprovalRequestStatus.APPROVED

    async def create(self, payload, *, requester):
        item = SimpleNamespace(
            id=f"approval-{len(self.created)+1}",
            payload=payload,
            requester=requester,
        )
        self.created.append(item)
        return item

    def get(self, request_id, *, actor):
        del actor
        return SimpleNamespace(
            id=request_id,
            status=self.status,
        )

    async def consume(self, request_id, payload, *, actor):
        self.consumed.append((request_id, payload, actor))
        return SimpleNamespace(id=request_id)


class UpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.definitions = _Definitions()
        self.workers = _Workers()
        self.extensions = _Extensions()
        self.recovery = _Recovery()
        self.releases = _Releases()
        self.actions = _Actions()
        self.approvals = _Approvals()
        self.evidence = _Evidence()
        self.service = UpgradeService(
            UpgradeStore(self.state),
            state_store=self.state,
            definitions=self.definitions,
            workers=self.workers,
            extensions=self.extensions,
            recovery=self.recovery,
            releases=self.releases,
            action_intents=self.actions,
            approvals=self.approvals,
            evidence=self.evidence,
            clock=lambda: 1000.0,
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    def profile(self, **overrides):
        values = dict(
            source_app_version="1.0.0",
            target_app_version="1.1.0",
            deployment_mode=UpgradeDeploymentMode.REPLICATED,
            supported_control_plane_versions_during_rollout=(
                "1.0.0",
                "1.1.0",
            ),
            supported_worker_versions_during_rollout=("worker-v1", "worker-v2"),
            supported_execution_contract_versions=("1.0",),
            source_state_schema_version=1,
            target_state_schema_version=1,
            supported_event_contract_versions=("1.0",),
            supported_api_contract_versions=("1.0",),
            target_definition_engine_version="1.1",
            target_definition_schemas={},
            target_extension_host_version="3.1.0",
            rollback_supported_to_app_version="1.0.0",
        )
        values.update(overrides)
        return UpgradeCompatibilityProfile(**values)

    def create_plan(self, *, steps=(), profile=None, require_drain=False):
        return self.service.create(
            UpgradePlanCreate(
                release_id="release-target",
                current_app_version="1.0.0",
                target_app_version="1.1.0",
                compatibility=profile or self.profile(),
                observed_control_plane_versions=("1.0.0", "1.1.0"),
                steps=steps,
                require_recovery_qualification=True,
                require_drain=require_drain,
            ),
            actor=self.actor,
        )

    async def test_preflight_blocks_incompatible_definition_worker_extension_and_active_work(self):
        payload = {"mode": "strict"}
        record = DefinitionRecord(
            definition_id="policy.main",
            kind="policy",
            definition_schema_version="2.0",
            revision=1,
            scope_type=DefinitionScope.GLOBAL,
            lifecycle=DefinitionLifecycle.PUBLISHED,
            payload=payload,
            checksum=definition_checksum(
                definition_id="policy.main",
                kind="policy",
                definition_schema_version="2.0",
                payload=payload,
            ),
            created_by="admin",
            published_by="admin",
            published_at=900,
        )
        self.definitions.records[record.record_id] = record
        self.workers._workers.append(
            SimpleNamespace(
                id="worker-old",
                version="worker-v0",
                lifecycle=WorkerLifecycle.ACTIVE,
            )
        )
        self.workers._assignments.append(
            SimpleNamespace(
                id="assignment-old",
                status=AssignmentStatus.RUNNING,
                execution_contract_version="0.9",
            )
        )
        self.extensions.installations.append(
            SimpleNamespace(
                id="extension-old",
                lifecycle=ExtensionLifecycleState.ENABLED,
                manifest=SimpleNamespace(
                    compatibility=SimpleNamespace(codex_web=">=2.0.0 <3.0.0")
                ),
            )
        )
        self.actions.intents.append(
            SimpleNamespace(status=ActionIntentStatus.EXECUTING)
        )

        plan = self.create_plan(require_drain=True)
        plan = self.service.preflight(plan.id, actor=self.actor)

        self.assertFalse(plan.preflight.satisfied)
        self.assertIn(record.record_id, plan.preflight.incompatible_definition_record_ids)
        self.assertIn("worker-old", plan.preflight.incompatible_worker_ids)
        self.assertIn("extension-old", plan.preflight.incompatible_extension_ids)
        self.assertGreater(plan.preflight.active_action_intents, 0)
        self.assertGreater(plan.preflight.active_worker_assignments, 0)
        self.assertIsNotNone(plan.preflight.evidence_id)
        self.assertEqual(self.evidence.items[-1].payload.result, EvidenceResult.FAIL)

    async def test_drain_guard_blocks_ordinary_actions_but_allows_incident_and_blocks_workers(self):
        plan = self.create_plan(require_drain=True)
        plan = self.service.start_drain(plan.id, actor=self.actor)
        self.assertTrue(plan.maintenance_mode)

        ordinary = SimpleNamespace(
            organization_id="org-a",
            workspace_id="ws-a",
            request=SimpleNamespace(parameters={}),
            action_id="deploy.normal",
        )
        incident = SimpleNamespace(
            organization_id="org-a",
            workspace_id="ws-a",
            request=SimpleNamespace(parameters={"incident_id": "incident-1"}),
            action_id="service.disable",
        )
        self.assertFalse(self.service.action_execution_allowed(ordinary))
        self.assertTrue(self.service.action_execution_allowed(incident))
        self.assertFalse(
            self.service.worker_assignment_allowed("org-a", "ws-a")
        )

    async def test_failed_idempotent_step_resumes_and_publishes_step_evidence(self):
        attempts = {"count": 0}

        def handler(plan, step, actor):
            del plan, step, actor
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("temporary failure")
            return {"migrated": True}

        self.service.register_migration_handler("migrate", handler)
        plan = self.create_plan(
            steps=(
                UpgradeStepCreate(
                    id="expand",
                    phase=UpgradePhase.EXPAND,
                    kind=UpgradeStepKind.STATE_SCHEMA,
                    description="Expand compatible schema",
                    handler_id="migrate",
                    idempotent=True,
                ),
            )
        )
        plan = self.service.preflight(plan.id, actor=self.actor)
        self.assertTrue(plan.preflight.satisfied)

        failed = await self.service.execute_step(
            plan.id,
            "expand",
            UpgradeStepExecute(idempotency_key="expand-1"),
            actor=self.actor,
        )
        self.assertEqual(failed.steps[0].status, UpgradeStepStatus.FAILED)
        self.assertEqual(failed.steps[0].attempts, 1)
        self.assertIsNotNone(failed.steps[0].evidence_id)

        succeeded = await self.service.execute_step(
            plan.id,
            "expand",
            UpgradeStepExecute(idempotency_key="expand-2"),
            actor=self.actor,
        )
        self.assertEqual(succeeded.steps[0].status, UpgradeStepStatus.SUCCEEDED)
        self.assertEqual(succeeded.steps[0].attempts, 2)
        self.assertTrue(succeeded.steps[0].result["migrated"])

    async def test_irreversible_step_requires_backup_approval_and_closes_rollback(self):
        plan = self.create_plan(
            steps=(
                UpgradeStepCreate(
                    id="contract",
                    phase=UpgradePhase.CONTRACT,
                    kind=UpgradeStepKind.CLEANUP,
                    description="Drop legacy representation",
                    irreversible=True,
                    reversible=False,
                    requires_backup=True,
                ),
            )
        )
        plan = self.service.preflight(plan.id, actor=self.actor)
        self.assertTrue(plan.preflight.satisfied)

        with self.assertRaises(UpgradeConflictError):
            await self.service.request_step_approval(
                plan.id,
                "contract",
                actor=self.actor,
            )

        plan = self.service.capture_pre_upgrade_backup(
            plan.id,
            actor=self.actor,
        )
        self.assertEqual(plan.pre_upgrade_backup_id, "backup-1")

        plan = await self.service.request_step_approval(
            plan.id,
            "contract",
            actor=self.actor,
        )
        self.assertIsNotNone(plan.steps[0].approval_request_id)
        self.assertEqual(
            self.approvals.created[0].payload.evidence_refs,
            (plan.preflight.evidence_id,),
        )

        plan = await self.service.execute_step(
            plan.id,
            "contract",
            UpgradeStepExecute(idempotency_key="contract-1"),
            actor=self.actor,
        )
        self.assertTrue(plan.irreversible_boundary_crossed)
        self.assertFalse(plan.rollback_available)
        self.assertEqual(len(self.approvals.consumed), 1)

        with self.assertRaises(UpgradeConflictError):
            self.service.mark_rolled_back(
                plan.id,
                actor=self.actor,
                reason="operator requested rollback",
            )

    async def test_post_verification_completes_and_exits_maintenance(self):
        plan = self.create_plan(
            steps=(
                UpgradeStepCreate(
                    id="verify",
                    phase=UpgradePhase.VERIFY,
                    kind=UpgradeStepKind.VERIFICATION,
                    description="Verify target",
                ),
            ),
            require_drain=True,
        )
        plan = self.service.start_drain(plan.id, actor=self.actor)
        plan = self.service.preflight(plan.id, actor=self.actor)
        self.assertTrue(plan.preflight.satisfied)

        plan = await self.service.execute_step(
            plan.id,
            "verify",
            UpgradeStepExecute(idempotency_key="verify-1"),
            actor=self.actor,
        )
        plan = self.service.verify_post_upgrade(plan.id, actor=self.actor)
        self.assertEqual(plan.status.value, "completed")
        self.assertFalse(plan.maintenance_mode)
        self.assertIsNotNone(plan.post_upgrade_evidence_id)
        self.assertEqual(self.evidence.items[-1].payload.result, EvidenceResult.PASS)


if __name__ == "__main__":
    unittest.main()
