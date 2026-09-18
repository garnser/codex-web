from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from codex_web.action_intents import (
    ActionDecisionOutcome,
    ActionDecisionSnapshot,
    ActionInboxCreate,
    ActionIntentClaimRequest,
    ActionIntentCreate,
    ActionIntentReconcileRequest,
    ActionIntentRetryRequest,
    ActionIntentRollbackRequest,
    ActionIntentStatus,
    ActionIntentUnsafeRetryError,
    ActionIntentWorkItemSuccess,
)
from codex_web.action_providers import (
    ActionCapability,
    ActionDefinition,
    ActionProviderBindingCreate,
    ActionRequest,
    ActionResult,
    ActionVerification,
)
from codex_web.artifact_evidence import (
    EvidenceCreate,
    EvidenceRequirement,
    EvidenceResult,
    EvidenceType,
)
from codex_web.models import WorkItemState
from codex_web.resources import ResourceCreate, ResourceType
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.action_providers import ActionExecutionService, ActionProviderRegistry
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.identity import IdentityService
from codex_web.services.reference_action_provider import ReferenceActionProvider
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _WorkItemHost:
    def __init__(self, state: WorkItemState) -> None:
        self.states = {state.ref: state}
        self.events: list[dict] = []

    def _load_work_item_states(self):
        return {key: value.model_copy(deep=True) for key, value in self.states.items()}

    def _save_work_item_states(self, states):
        self.states = {key: value.model_copy(deep=True) for key, value in states.items()}

    def _work_item_state(self, ref):
        return self.states[ref].model_copy(deep=True)

    def _save_work_item_state(self, state):
        self.states[state.ref] = state.model_copy(deep=True)
        return state

    def _touch_work_item_progress(
        self,
        state,
        *,
        actor=None,
        current_owner=None,
        current_stage=None,
        next_action=None,
        next_owner=None,
        next_owner_present=False,
        blocker=None,
        blocker_present=False,
        blocking_findings=None,
        blocking_findings_present=False,
        release_gate=None,
        status_label=None,
        artifact_state=None,
        event_type="progress_updated",
        note=None,
    ):
        if current_stage is not None:
            state.current_stage = current_stage
        if next_action is not None:
            state.next_action = next_action
        if next_owner_present:
            state.next_owner = next_owner
        if note:
            state.notes = [*state.notes, note]
        state.updated_at = time.time()
        self.events.append({"event_type": event_type, "actor": actor})
        return state

    @staticmethod
    def _work_item_event(ref, event_type, **kwargs):
        return {"ref": ref, "event_type": event_type, **kwargs}

    def _append_work_item_event(self, event):
        self.events.append(event)


class _SlowProvider:
    contract_version = "1.0"
    provider_type = "slow"
    provider_instance = "test"

    def actions(self):
        return (
            ActionDefinition(
                action_id="slow.write",
                title="Slow write",
                capabilities=ActionCapability(
                    prepare=True,
                    execute=True,
                    idempotency=True,
                ),
                required_resource_types=(ResourceType.OTHER,),
                timeout_seconds=1.0,
                retry_max_attempts=3,
            ),
        )

    async def prepare(self, request, *, binding):
        return {}

    async def execute(self, request, *, binding, credential=None):
        await asyncio.sleep(0.05)
        now = time.time()
        return ActionResult(
            provider_binding_id=binding.id,
            action_id=request.action_id,
            status="succeeded",
            started_at=now,
            completed_at=now,
            idempotency_key=request.idempotency_key,
        )

    async def verify(self, result, *, binding):
        return ActionVerification(verified=True)

    async def rollback(self, result, *, binding, credential=None):
        raise AssertionError


class _NonIdempotentUnknownProvider:
    contract_version = "1.0"
    provider_type = "non-idempotent"
    provider_instance = "test"

    def actions(self):
        return (
            ActionDefinition(
                action_id="unknown.write",
                title="Unknown write",
                capabilities=ActionCapability(
                    prepare=True,
                    execute=True,
                    idempotency=False,
                ),
                required_resource_types=(ResourceType.OTHER,),
                retry_max_attempts=3,
            ),
        )

    async def prepare(self, request, *, binding):
        return {}

    async def execute(self, request, *, binding, credential=None):
        raise RuntimeError("connection dropped after send")

    async def verify(self, result, *, binding):
        return ActionVerification(verified=False)

    async def rollback(self, result, *, binding, credential=None):
        raise AssertionError


class ActionIntentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(self.sqlite))
        self.identity.bootstrap_local()
        self.actor = self.identity.local_trusted_actor()

        self.resources = ResourceCatalogService(ResourceCatalogStore(self.sqlite))
        self.resource = self.resources.create(
            ResourceCreate(resource_type=ResourceType.OTHER, name="Target"),
            actor=self.actor,
        )
        self.registry = ActionProviderRegistry(ActionProviderStateStore(self.sqlite))
        self.reference = ReferenceActionProvider()
        self.registry.register(self.reference)
        self.execution = ActionExecutionService(self.registry, self.resources)

        self.work_item = WorkItemState(
            ref="group/app#42",
            organization_id="local",
            workspace_id="default",
            project_id="home",
            project_path="group/app",
            resource_ids=[self.resource.id],
            current_owner="james",
            current_stage="implementation_active",
            artifact_state="branch",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        self.host = _WorkItemHost(self.work_item)
        self.artifacts = ArtifactEvidenceService(
            ArtifactEvidenceStore(self.sqlite),
            work_item_host=self.host,
        )
        self.service = ActionIntentService(
            ActionIntentStore(self.sqlite),
            self.execution,
            artifact_evidence=self.artifacts,
            work_item_host=self.host,
        )
        self.binding = self.registry.bind(
            ActionProviderBindingCreate(
                provider_type=self.reference.provider_type,
                provider_instance=self.reference.provider_instance,
                resource_ids=(self.resource.id,),
            ),
            actor=self.actor,
            resources=self.resources,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _request(self, **overrides):
        payload = {
            "action_id": "reference.set",
            "organization_id": "local",
            "workspace_id": "default",
            "project_id": "home",
            "resource_ids": (self.resource.id,),
            "parameters": {"key": "feature", "value": "enabled"},
        }
        payload.update(overrides)
        return ActionRequest(**payload)

    def _create(self, **overrides):
        payload = {
            "binding_id": self.binding.id,
            "request": self._request(),
            "work_item_ref": self.work_item.ref,
        }
        payload.update(overrides)
        return self.service.create(ActionIntentCreate(**payload), actor=self.actor)

    async def _execute(self, intent):
        claim = self.service.claim(
            ActionIntentClaimRequest(worker_id="worker-1", lease_seconds=30),
            actor=self.actor,
            intent_id=intent.id,
        )
        self.assertIsNotNone(claim)
        return await self.service.execute_claimed(
            intent.id,
            "worker-1",
            actor=self.actor,
        )

    async def test_intent_is_durable_before_any_provider_side_effect(self) -> None:
        intent = self._create()

        self.assertEqual(intent.status, ActionIntentStatus.PENDING)
        self.assertEqual(self.reference.values, {})
        history = self.service.history(intent.id, self.actor)
        self.assertEqual(history["receipts"], [])
        self.assertEqual(history["verifications"], [])
        self.assertEqual(history["intent"]["action_definition"]["action_id"], "reference.set")

    async def test_verified_execution_advances_work_item_only_after_success(self) -> None:
        intent = self._create(
            work_item_success=ActionIntentWorkItemSuccess(
                current_stage="ready_for_validation",
                next_action="Validate exact provider result.",
                next_owner="quinn",
                note="External action verified.",
            )
        )
        self.assertEqual(
            self.host.states[self.work_item.ref].current_stage,
            "implementation_active",
        )

        completed = await self._execute(intent)

        self.assertEqual(completed.status, ActionIntentStatus.SUCCEEDED)
        current = self.host.states[self.work_item.ref]
        self.assertEqual(current.current_stage, "ready_for_validation")
        self.assertEqual(current.next_owner, "quinn")
        self.assertEqual(current.next_action, "Validate exact provider result.")
        self.assertIn("External action verified.", current.notes)
        history = self.service.history(intent.id, self.actor)
        self.assertEqual(len(history["receipts"]), 1)
        self.assertEqual(len(history["verifications"]), 1)
        self.assertTrue(history["verifications"][0]["verified"])

    async def test_missing_required_evidence_blocks_success_until_reconciliation(self) -> None:
        requirement = EvidenceRequirement(
            id="ci-pass",
            evidence_type=EvidenceType.CI_CHECK,
            accepted_results=(EvidenceResult.PASS,),
        )
        intent = self._create(
            expected_evidence=(requirement,),
            work_item_success=ActionIntentWorkItemSuccess(
                current_stage="ready_for_validation",
            ),
        )

        first = await self._execute(intent)
        self.assertEqual(first.status, ActionIntentStatus.REQUIRES_RECONCILIATION)
        self.assertEqual(
            self.host.states[self.work_item.ref].current_stage,
            "implementation_active",
        )

        self.artifacts.create_evidence(
            EvidenceCreate(
                work_item_ref=self.work_item.ref,
                evidence_type=EvidenceType.CI_CHECK,
                provider="ci",
                result=EvidenceResult.PASS,
                summary="CI passed.",
            ),
            actor=self.actor,
        )
        reconciled = await self.service.reconcile(
            intent.id,
            ActionIntentReconcileRequest(retry_if_idempotent=False),
            actor=self.actor,
        )

        self.assertEqual(reconciled.status, ActionIntentStatus.SUCCEEDED)
        self.assertEqual(
            self.host.states[self.work_item.ref].current_stage,
            "ready_for_validation",
        )

    async def test_timeout_records_unknown_receipt_and_uncertain_state(self) -> None:
        provider = _SlowProvider()
        self.registry.register(provider)
        binding = self.registry.bind(
            ActionProviderBindingCreate(
                provider_type=provider.provider_type,
                provider_instance=provider.provider_instance,
                resource_ids=(self.resource.id,),
            ),
            actor=self.actor,
            resources=self.resources,
        )
        intent = self.service.create(
            ActionIntentCreate(
                binding_id=binding.id,
                request=ActionRequest(
                    action_id="slow.write",
                    organization_id="local",
                    workspace_id="default",
                    resource_ids=(self.resource.id,),
                ),
                timeout_seconds=0.001,
            ),
            actor=self.actor,
        )

        completed = await self._execute(intent)
        self.assertEqual(completed.status, ActionIntentStatus.UNCERTAIN)
        history = self.service.history(intent.id, self.actor)
        self.assertEqual(history["receipts"][-1]["outcome"], "unknown")

    async def test_crash_after_execution_started_recovers_as_uncertain(self) -> None:
        intent = self._create()
        self.service.claim(
            ActionIntentClaimRequest(worker_id="worker-1", lease_seconds=10),
            actor=self.actor,
            intent_id=intent.id,
        )
        executing = self.service._mark_executing(intent.id, "worker-1", self.actor)

        recovered = self.service.recover_stale_claims(
            now=executing.lease.expires_at + 1,
        )
        current = self.service.get(intent.id, self.actor)

        self.assertEqual(recovered, [intent.id])
        self.assertEqual(current.status, ActionIntentStatus.UNCERTAIN)
        self.assertIsNone(current.lease)

    async def test_duplicate_intent_and_callback_delivery_are_suppressed(self) -> None:
        request = self._request(idempotency_key="stable-request-42")
        first = self.service.create(
            ActionIntentCreate(binding_id=self.binding.id, request=request),
            actor=self.actor,
        )
        second = self.service.create(
            ActionIntentCreate(binding_id=self.binding.id, request=request),
            actor=self.actor,
        )
        self.assertEqual(first.id, second.id)

        callback = ActionInboxCreate(
            provider_type=first.provider_type,
            provider_instance=first.provider_instance,
            delivery_id="delivery-1",
            intent_id=first.id,
            event_type="completed",
            outcome=ActionIntentStatus.SUCCEEDED,
        )
        one = self.service.ingest_callback(callback, actor=self.actor)
        two = self.service.ingest_callback(callback, actor=self.actor)

        self.assertFalse(one.duplicate)
        self.assertTrue(two.duplicate)
        history = self.service.history(first.id, self.actor)
        self.assertEqual(len(history["inbox"]), 1)
        self.assertEqual(
            self.service.get(first.id, self.actor).status,
            ActionIntentStatus.REQUIRES_RECONCILIATION,
        )

    async def test_callback_provider_must_match_intent_provider(self) -> None:
        intent = self._create()
        with self.assertRaises(Exception):
            self.service.ingest_callback(
                ActionInboxCreate(
                    provider_type="wrong-provider",
                    provider_instance=intent.provider_instance,
                    delivery_id="delivery-wrong-provider",
                    intent_id=intent.id,
                    event_type="completed",
                    outcome=ActionIntentStatus.SUCCEEDED,
                ),
                actor=self.actor,
            )

        history = self.service.history(intent.id, self.actor)
        self.assertEqual(history["inbox"], [])

    async def test_failed_callback_records_terminal_completion_timestamp(self) -> None:
        intent = self._create()
        self.service.ingest_callback(
            ActionInboxCreate(
                provider_type=intent.provider_type,
                provider_instance=intent.provider_instance,
                delivery_id="delivery-failed",
                intent_id=intent.id,
                event_type="failed",
                outcome=ActionIntentStatus.FAILED,
            ),
            actor=self.actor,
        )

        current = self.service.get(intent.id, self.actor)
        self.assertEqual(current.status, ActionIntentStatus.FAILED)
        self.assertIsNotNone(current.completed_at)

    async def test_non_idempotent_unknown_outcome_cannot_be_replayed(self) -> None:
        provider = _NonIdempotentUnknownProvider()
        self.registry.register(provider)
        binding = self.registry.bind(
            ActionProviderBindingCreate(
                provider_type=provider.provider_type,
                provider_instance=provider.provider_instance,
                resource_ids=(self.resource.id,),
            ),
            actor=self.actor,
            resources=self.resources,
        )
        intent = self.service.create(
            ActionIntentCreate(
                binding_id=binding.id,
                request=ActionRequest(
                    action_id="unknown.write",
                    organization_id="local",
                    workspace_id="default",
                    resource_ids=(self.resource.id,),
                ),
            ),
            actor=self.actor,
        )

        current = await self._execute(intent)
        self.assertEqual(current.status, ActionIntentStatus.UNCERTAIN)
        with self.assertRaises(ActionIntentUnsafeRetryError):
            self.service.retry(
                intent.id,
                ActionIntentRetryRequest(reason="do not duplicate"),
                actor=self.actor,
            )

    async def test_idempotent_uncertain_action_can_be_requeued_safely(self) -> None:
        provider = _SlowProvider()
        self.registry.register(provider)
        binding = self.registry.bind(
            ActionProviderBindingCreate(
                provider_type=provider.provider_type,
                provider_instance=provider.provider_instance,
                resource_ids=(self.resource.id,),
            ),
            actor=self.actor,
            resources=self.resources,
        )
        intent = self.service.create(
            ActionIntentCreate(
                binding_id=binding.id,
                request=ActionRequest(
                    action_id="slow.write",
                    organization_id="local",
                    workspace_id="default",
                    resource_ids=(self.resource.id,),
                ),
                timeout_seconds=0.001,
            ),
            actor=self.actor,
        )
        uncertain = await self._execute(intent)
        self.assertEqual(uncertain.status, ActionIntentStatus.UNCERTAIN)

        retried = self.service.retry(
            intent.id,
            ActionIntentRetryRequest(reason="same idempotency key"),
            actor=self.actor,
        )
        self.assertEqual(retried.status, ActionIntentStatus.PENDING)
        self.assertEqual(retried.request.idempotency_key, intent.request.idempotency_key)

    async def test_rollback_is_recorded_as_terminal_history(self) -> None:
        intent = self._create(rollback_required=True)
        succeeded = await self._execute(intent)
        self.assertEqual(succeeded.status, ActionIntentStatus.SUCCEEDED)

        rolled_back = await self.service.rollback(
            intent.id,
            ActionIntentRollbackRequest(reason="operator rollback"),
            actor=self.actor,
        )
        self.assertEqual(rolled_back.status, ActionIntentStatus.ROLLED_BACK)
        history = self.service.history(intent.id, self.actor)
        self.assertEqual(history["receipts"][-1]["result"]["status"], "rolled_back")

    async def test_denied_authority_is_persisted_but_never_claimable(self) -> None:
        intent = self._create(
            authority_decision=ActionDecisionSnapshot(
                decision_id="authority-1",
                outcome=ActionDecisionOutcome.DENY,
                source="test-policy",
                reason="not allowed",
                evaluated_at=time.time(),
            )
        )
        self.assertEqual(intent.status, ActionIntentStatus.CANCELLED)
        self.assertEqual(self.reference.values, {})
        claim = self.service.claim(
            ActionIntentClaimRequest(worker_id="worker-1"),
            actor=self.actor,
            intent_id=intent.id,
        )
        self.assertIsNone(claim)


if __name__ == "__main__":
    unittest.main()
