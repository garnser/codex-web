from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

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
from codex_web.capacity import CapacityPolicy, WorkloadKind, WorkloadPriority
from codex_web.entitlements import (
    CapabilityEntitlementUpdate,
    EntitlementMode,
    QuotaBehavior,
    QuotaPolicyUpdate,
    QuotaWindow,
)
from codex_web.artifact_evidence import (
    EvidenceCreate,
    EvidenceRequirement,
    EvidenceResult,
    EvidenceType,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    PrincipalKind,
)
from codex_web.models import WorkItemState
from codex_web.resources import (
    RepositoryExecutionScope,
    RepositoryTargetSource,
    RepositoryWriteMode,
    ResourceCreate,
    ResourceType,
)
from codex_web.services.action_intents import (
    ActionIntentConflictError,
    ActionIntentService,
    ActionIntentUnsafeRetryError,
)
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.action_providers import ActionExecutionService, ActionProviderRegistry
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.capacity import CapacityService
from codex_web.services.entitlements import EntitlementDeniedError, EntitlementService
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.reference_action_provider import ReferenceActionProvider
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.capacity import CapacityStore
from codex_web.storage.entitlements import EntitlementStore
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
                required_authority=("action.test.write",),
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
                required_authority=("action.test.write",),
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
        self.worker_actor = AuthenticationActor(
            identity_id="action-worker",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="local",
            workspace_id="default",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("action-intent:worker",),
        )
        self.callback_actor = AuthenticationActor(
            identity_id="provider-callback",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="local",
            workspace_id="default",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("action-intent:callback",),
        )
        self.admin_service_actor = AuthenticationActor(
            identity_id="action-admin-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="local",
            workspace_id="default",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("action-intent:admin",),
        )

        self.resources = ResourceCatalogService(ResourceCatalogStore(self.sqlite))
        self.resource = self.resources.create(
            ResourceCreate(resource_type=ResourceType.OTHER, name="Target"),
            actor=self.actor,
        )
        self.definition_registry = DefinitionRegistryService(
            DefinitionRegistryStore(self.sqlite)
        )
        self.authority = install_authority_roles(
            self.definition_registry,
            self.resources,
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
        self.entitlements = EntitlementService(EntitlementStore(self.sqlite))
        self.service = ActionIntentService(
            ActionIntentStore(self.sqlite),
            self.execution,
            artifact_evidence=self.artifacts,
            work_item_host=self.host,
            entitlements=self.entitlements,
            authority=self.authority,
            identity=self.identity,
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
            actor=self.worker_actor,
            intent_id=intent.id,
        )
        self.assertIsNotNone(claim)
        return await self.service.execute_claimed(
            intent.id,
            "worker-1",
            actor=self.worker_actor,
        )

    async def test_enforced_entitlement_denies_intent_creation_without_capability(self) -> None:
        self.entitlements.set_mode(EntitlementMode.ENFORCED, actor=self.actor)

        with self.assertRaises(EntitlementDeniedError):
            self._create()

        self.assertEqual(self.reference.values, {})

    async def test_existing_idempotent_intent_is_retrievable_after_entitlement_revocation(self) -> None:
        stable = self._request(idempotency_key="stable-before-plan-change")
        first = self.service.create(
            ActionIntentCreate(
                binding_id=self.binding.id,
                request=stable,
                work_item_ref=self.work_item.ref,
            ),
            actor=self.actor,
        )

        self.entitlements.set_mode(EntitlementMode.ENFORCED, actor=self.actor)
        with self.assertRaises(EntitlementDeniedError):
            self._create()

        duplicate = self.service.create(
            ActionIntentCreate(
                binding_id=self.binding.id,
                request=stable,
                work_item_ref=self.work_item.ref,
            ),
            actor=self.actor,
        )
        self.assertEqual(duplicate.id, first.id)

    async def test_hard_quota_denies_before_provider_execution_and_keeps_attempt_unspent(self) -> None:
        self.entitlements.set_mode(EntitlementMode.ENFORCED, actor=self.actor)
        self.entitlements.set_capability(
            "external_actions",
            CapabilityEntitlementUpdate(enabled=True),
            actor=self.actor,
        )
        self.entitlements.set_quota(
            "external_action_attempts",
            QuotaPolicyUpdate(
                limit=0,
                window=QuotaWindow.LIFETIME,
                behavior=QuotaBehavior.HARD_STOP,
            ),
            actor=self.actor,
        )
        intent = self._create()

        completed = await self._execute(intent)

        self.assertEqual(completed.status, ActionIntentStatus.FAILED)
        self.assertEqual(completed.attempt, 0)
        self.assertIsNone(completed.lease)
        self.assertIn("quota_exceeded_hard_stop", completed.last_error)
        self.assertEqual(self.reference.values, {})
        self.assertEqual(
            self.entitlements.usage_total(
                self.actor,
                metric="external_action_attempts",
            ),
            0,
        )

    async def test_entitlement_does_not_override_policy_denial(self) -> None:
        self.entitlements.set_mode(EntitlementMode.ENFORCED, actor=self.actor)
        self.entitlements.set_capability(
            "external_actions",
            CapabilityEntitlementUpdate(enabled=True),
            actor=self.actor,
        )
        intent = self._create(
            policy_decision=ActionDecisionSnapshot(
                outcome=ActionDecisionOutcome.DENY,
                source="policy:test",
                reason="not authorized",
            ),
        )

        self.assertEqual(intent.status, ActionIntentStatus.CANCELLED)
        self.assertIsNotNone(intent.failure)
        self.assertEqual(
            intent.failure.reason_code.value,
            "authority_denied",
        )
        self.assertEqual(self.reference.values, {})

    async def test_caller_authority_allow_or_deny_does_not_replace_canonical_decision(self) -> None:
        for caller_outcome in (
            ActionDecisionOutcome.ALLOW,
            ActionDecisionOutcome.DENY,
        ):
            with self.subTest(caller_outcome=caller_outcome):
                intent = self._create(
                    authority_decision=ActionDecisionSnapshot(
                        outcome=caller_outcome,
                        source="approval:caller-assertion",
                        reason="caller assertion must not authorize",
                    ),
                )
                self.assertEqual(intent.status, ActionIntentStatus.PENDING)
                self.assertEqual(
                    intent.authority_decision.outcome,
                    ActionDecisionOutcome.ALLOW,
                )
                self.assertEqual(
                    intent.authority_decision.source,
                    "canonical:role-authority",
                )
                self.assertNotEqual(
                    intent.authority_decision.source,
                    "approval:caller-assertion",
                )
                self.assertTrue(intent.authority_decision.definition_refs)

    async def test_intent_is_durable_before_any_provider_side_effect(self) -> None:
        intent = self._create()

        self.assertEqual(intent.status, ActionIntentStatus.PENDING)
        self.assertEqual(intent.authority_decision.source, "canonical:role-authority")
        self.assertEqual(
            intent.authority_decision.capabilities,
            ("action.reference.set",),
        )
        self.assertTrue(intent.authority_decision.definition_refs)
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
            actor=self.worker_actor,
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
            actor=self.worker_actor,
            intent_id=intent.id,
        )
        executing = self.service._mark_executing(
            intent.id,
            "worker-1",
            self.worker_actor,
        )

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
        one = self.service.ingest_callback(callback, actor=self.callback_actor)
        two = self.service.ingest_callback(callback, actor=self.callback_actor)

        self.assertFalse(one.duplicate)
        self.assertTrue(two.duplicate)
        history = self.service.history(first.id, self.actor)
        self.assertEqual(len(history["inbox"]), 1)
        self.assertEqual(
            self.service.get(first.id, self.actor).status,
            ActionIntentStatus.REQUIRES_RECONCILIATION,
        )

    async def test_idempotency_key_cannot_cross_resource_targets(self) -> None:
        other = self.resources.create(
            ResourceCreate(resource_type=ResourceType.OTHER, name="Other target"),
            actor=self.actor,
        )
        binding = self.registry.bind(
            ActionProviderBindingCreate(
                provider_type=self.reference.provider_type,
                provider_instance=self.reference.provider_instance,
                resource_ids=(self.resource.id, other.id),
            ),
            actor=self.actor,
            resources=self.resources,
        )
        stable_key = "stable-resource-target"
        first = self.service.create(
            ActionIntentCreate(
                binding_id=binding.id,
                request=self._request(
                    resource_ids=(self.resource.id,),
                    idempotency_key=stable_key,
                ),
            ),
            actor=self.actor,
        )

        with self.assertRaisesRegex(
            ActionIntentConflictError,
            "different action target or execution context",
        ):
            self.service.create(
                ActionIntentCreate(
                    binding_id=binding.id,
                    request=self._request(
                        resource_ids=(other.id,),
                        idempotency_key=stable_key,
                    ),
                ),
                actor=self.actor,
            )

        self.assertEqual(first.resource_ids, (self.resource.id,))
        self.assertEqual(len(self.service.list(self.actor)), 1)

    async def test_idempotency_key_cannot_cross_execution_attribution(self) -> None:
        request = self._request(idempotency_key="stable-execution-context")
        first = self.service.create(
            ActionIntentCreate(
                binding_id=self.binding.id,
                request=request,
                execution_id="exec-a",
            ),
            actor=self.actor,
        )

        with self.assertRaises(ActionIntentConflictError):
            self.service.create(
                ActionIntentCreate(
                    binding_id=self.binding.id,
                    request=request,
                    execution_id="exec-b",
                ),
                actor=self.actor,
            )

        self.assertEqual(first.execution_id, "exec-a")

    async def test_execution_bound_repository_action_must_stay_in_writable_scope(self) -> None:
        writable_repository = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Writable repository",
            ),
            actor=self.actor,
        )
        read_only_repository = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Read-only repository",
            ),
            actor=self.actor,
        )
        binding = self.registry.bind(
            ActionProviderBindingCreate(
                provider_type=self.reference.provider_type,
                provider_instance=self.reference.provider_instance,
                resource_ids=(
                    self.resource.id,
                    writable_repository.id,
                    read_only_repository.id,
                ),
            ),
            actor=self.actor,
            resources=self.resources,
        )
        assignment = SimpleNamespace(
            repository_scope=RepositoryExecutionScope(
                organization_id="local",
                workspace_id="default",
                project_id="home",
                writable_repository_ids=(writable_repository.id,),
                read_only_repository_ids=(read_only_repository.id,),
                write_mode=RepositoryWriteMode.SINGLE,
                source=RepositoryTargetSource.EXPLICIT,
            ),
            repository_target=None,
        )
        self.service.execution_workers = SimpleNamespace(
            assignment_for_execution=lambda execution_id, actor: assignment
        )

        allowed = self.service.create(
            ActionIntentCreate(
                binding_id=binding.id,
                request=self._request(
                    resource_ids=(self.resource.id, writable_repository.id),
                    idempotency_key="execution-scope-allowed",
                ),
                execution_id="exec-coordinated",
            ),
            actor=self.actor,
        )
        self.assertEqual(
            set(allowed.resource_ids),
            {self.resource.id, writable_repository.id},
        )

        with self.assertRaisesRegex(
            ActionIntentConflictError,
            "outside execution writable scope",
        ):
            self.service.create(
                ActionIntentCreate(
                    binding_id=binding.id,
                    request=self._request(
                        resource_ids=(self.resource.id, read_only_repository.id),
                        idempotency_key="execution-scope-denied",
                    ),
                    execution_id="exec-coordinated",
                ),
                actor=self.actor,
            )

        self.assertEqual(len(self.service.list(self.actor)), 1)

    async def test_execution_bound_repository_action_requires_assignment_state(self) -> None:
        repository = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Repository",
            ),
            actor=self.actor,
        )
        binding = self.registry.bind(
            ActionProviderBindingCreate(
                provider_type=self.reference.provider_type,
                provider_instance=self.reference.provider_instance,
                resource_ids=(self.resource.id, repository.id),
            ),
            actor=self.actor,
            resources=self.resources,
        )

        with self.assertRaisesRegex(
            ActionIntentConflictError,
            "requires canonical execution assignment state",
        ):
            self.service.create(
                ActionIntentCreate(
                    binding_id=binding.id,
                    request=self._request(
                        resource_ids=(self.resource.id, repository.id),
                    ),
                    execution_id="exec-missing-assignment",
                ),
                actor=self.actor,
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
                actor=self.callback_actor,
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
            actor=self.callback_actor,
        )

        current = self.service.get(intent.id, self.actor)
        self.assertEqual(current.status, ActionIntentStatus.FAILED)
        self.assertIsNotNone(current.completed_at)
        self.assertIsNotNone(current.failure)
        self.assertEqual(
            current.failure.reason_code.value,
            "unclassified",
        )

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

    async def test_idempotent_uncertain_action_requires_reconciliation_before_requeue(self) -> None:
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
        self.assertIsNotNone(uncertain.failure)
        self.assertEqual(
            uncertain.failure.reason_code.value,
            "action_timeout_unknown_outcome",
        )
        self.assertTrue(uncertain.failure.requires_reconciliation)

        with self.assertRaises(ActionIntentUnsafeRetryError):
            self.service.retry(
                intent.id,
                ActionIntentRetryRequest(reason="same idempotency key"),
                actor=self.actor,
            )

        retried = await self.service.reconcile(
            intent.id,
            ActionIntentReconcileRequest(
                retry_if_idempotent=True,
            ),
            actor=self.worker_actor,
        )
        self.assertEqual(retried.status, ActionIntentStatus.PENDING)
        self.assertIsNone(retried.failure)
        self.assertEqual(
            retried.request.idempotency_key,
            intent.request.idempotency_key,
        )

    async def test_rollback_is_recorded_as_terminal_history(self) -> None:
        intent = self._create(rollback_required=True)
        succeeded = await self._execute(intent)
        self.assertEqual(succeeded.status, ActionIntentStatus.SUCCEEDED)

        rolled_back = await self.service.rollback(
            intent.id,
            ActionIntentRollbackRequest(reason="operator rollback"),
            actor=self.worker_actor,
        )
        self.assertEqual(rolled_back.status, ActionIntentStatus.ROLLED_BACK)
        history = self.service.history(intent.id, self.actor)
        self.assertEqual(history["receipts"][-1]["result"]["status"], "rolled_back")

    async def test_human_admin_cannot_impersonate_action_worker(self) -> None:
        intent = self._create()

        with self.assertRaisesRegex(
            AuthorizationError,
            "service principal.*action-intent:worker",
        ):
            self.service.claim(
                ActionIntentClaimRequest(worker_id="human-worker"),
                actor=self.actor,
                intent_id=intent.id,
            )

    async def test_action_intent_admin_service_cannot_execute_worker_plane(self) -> None:
        intent = self._create()

        with self.assertRaisesRegex(
            AuthorizationError,
            "action-intent:worker",
        ):
            self.service.claim(
                ActionIntentClaimRequest(worker_id="admin-service"),
                actor=self.admin_service_actor,
                intent_id=intent.id,
            )

    async def test_callback_ingestion_requires_explicit_callback_service_scope(self) -> None:
        intent = self._create()
        callback = ActionInboxCreate(
            provider_type=intent.provider_type,
            provider_instance=intent.provider_instance,
            delivery_id="delivery-authority-test",
            intent_id=intent.id,
            event_type="completed",
            outcome=ActionIntentStatus.SUCCEEDED,
        )

        for actor in (self.actor, self.admin_service_actor):
            with self.subTest(actor=actor.identity_id):
                with self.assertRaisesRegex(
                    AuthorizationError,
                    "action-intent:callback",
                ):
                    self.service.ingest_callback(callback, actor=actor)

        accepted = self.service.ingest_callback(
            callback,
            actor=self.callback_actor,
        )
        self.assertFalse(accepted.duplicate)

    async def test_denied_policy_is_persisted_but_never_claimable(self) -> None:
        intent = self._create(
            policy_decision=ActionDecisionSnapshot(
                decision_id="policy-1",
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
            actor=self.worker_actor,
            intent_id=intent.id,
        )
        self.assertIsNone(claim)


    async def test_capacity_saturation_defers_claim_without_provider_execution(self) -> None:
        capacity = CapacityService(CapacityStore(self.sqlite))
        capacity.set_policy(
            CapacityPolicy(
                max_global_inflight=4,
                max_tenant_inflight=3,
                critical_global_reserve=1,
                critical_tenant_reserve=1,
                load_shed_threshold=1.0,
                low_priority_shed_threshold=1.0,
                workload_limits={WorkloadKind.ACTION: 1},
            )
        )
        blocker = capacity.acquire(
            organization_id="local",
            workspace_id="default",
            workload=WorkloadKind.ACTION,
            priority=WorkloadPriority.NORMAL,
            owner_ref="other-action",
            lease_seconds=60,
        )
        self.service.capacity = capacity

        intent = self._create()
        claim = self.service.claim(
            ActionIntentClaimRequest(
                worker_id="worker-1",
                lease_seconds=30,
            ),
            actor=self.worker_actor,
            intent_id=intent.id,
        )
        self.assertIsNotNone(claim)

        deferred = await self.service.execute_claimed(
            intent.id,
            "worker-1",
            actor=self.worker_actor,
        )
        self.assertEqual(deferred.status, ActionIntentStatus.PENDING)
        self.assertIsNone(deferred.lease)
        self.assertIsNotNone(deferred.not_before)
        self.assertIn("capacity deferred", deferred.last_error or "")
        self.assertEqual(self.reference.values, {})
        self.assertTrue(capacity.release(blocker.id))


    async def test_status_notifier_observes_terminal_transition(self) -> None:
        observed = []
        self.service.status_notifier = (
            lambda intent: observed.append((intent.id, intent.status))
        )
        intent = self._create()

        completed = await self._execute(intent)

        self.assertEqual(completed.status, ActionIntentStatus.SUCCEEDED)
        self.assertIn(
            (intent.id, ActionIntentStatus.SUCCEEDED),
            observed,
        )


if __name__ == "__main__":
    unittest.main()
