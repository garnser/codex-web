"""Socket regressions with canonical ActionIntent services and isolated SQLite.

Provider effects and canonical-assignment point lookups are controlled fixtures;
this is not a live execution-worker integration or an external provider test.
"""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_action_intents as intent_fixture
from tests import test_control_plane_broker as broker_fixture
from codex_web.action_intents import ActionIntentCreate, ActionIntentStatus
from codex_web.action_providers import (
    ActionCapability, ActionDefinition, ActionProviderBindingCreate,
    ActionRequest, ActionResult, ActionVerification,
)
from codex_web.resources import (
    ResourceCreate, ResourceType, RepositoryExecutionScope, RepositoryTargetSource,
)
from codex_web.services.action_intents import ActionIntentConflictError
from codex_web.storage.control_plane_broker import ControlPlaneBrokerAuditStore


SAFE_MESSAGE = (
    "Repository action admission conflicts with origin, scope or idempotency. "
    "Inspect authorized intent and delivery evidence; an existing intent may have "
    "executed or be uncertain. Do not automatically retry or replay."
)


class _ControlledProvider:
    contract_version = "1.0"
    provider_type = "github"
    provider_instance = "controlled.invalid"

    def __init__(self):
        self.calls = []
        self.lose_response = False

    def actions(self):
        return tuple(ActionDefinition(
            action_id=action, title=action,
            capabilities=ActionCapability(idempotency=True, verification=True),
            required_resource_types=(ResourceType.REPOSITORY,),
            required_authority=("repository.pull-request.create",),
        ) for action in ("code-host.branch.publish", "code-host.pull-request.upsert"))

    async def prepare(self, request, *, binding):
        return {}

    async def execute(self, request, *, binding, credential=None):
        self.calls.append(request.idempotency_key)
        if self.lose_response:
            raise RuntimeError("Controlled response lost after provider write")
        return ActionResult(
            provider_binding_id=binding.id, action_id=request.action_id,
            status="succeeded", started_at=time.time(), completed_at=time.time(),
            idempotency_key=request.idempotency_key, external_id="controlled-receipt",
        )

    async def verify(self, result, *, binding):
        return ActionVerification(verified=True)


class RepositoryActionAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Compose fixture services; do not inherit and rerun their test cases.
        self.b = broker_fixture.ControlPlaneBrokerTests()
        await self.b.asyncSetUp()
        self.addAsyncCleanup(self.b.asyncTearDown)
        self.a = intent_fixture.ActionIntentTests()
        await self.a.asyncSetUp()
        self.addAsyncCleanup(self.a.asyncTearDown)
        self.repo = self.a.resources.create(ResourceCreate(
            resource_type=ResourceType.REPOSITORY, name="Isolated repository",
        ), actor=self.a.actor)
        self.provider = _ControlledProvider()
        self.a.registry.register(self.provider)
        self.binding = self.a.registry.bind(ActionProviderBindingCreate(
            provider_type=self.provider.provider_type,
            provider_instance=self.provider.provider_instance,
            resource_ids=(self.repo.id,),
        ), actor=self.a.actor, resources=self.a.resources)
        self.assignment = self.b.assignment.model_copy(update={
            "project_id": "home", "work_item_ref": self.a.work_item.ref,
            "execution_profile_id": "repository-write", "execution_workspace_id": None,
            "resource_ids": (self.repo.id,),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default", project_id="home",
                writable_repository_ids=(self.repo.id,),
                source=RepositoryTargetSource.SINGLE_REPOSITORY,
            ),
        })
        self.intent_assignment = self.assignment
        self.a.service.execution_workers = SimpleNamespace(
            assignment_for_execution=lambda *args, **kwargs: self.intent_assignment,
            store=SimpleNamespace(assignment=lambda _: self.intent_assignment),
        )
        self.b.current_assignment = self.assignment
        self.b.broker.snapshot = self.assignment.model_copy(deep=True)
        self.b.service.identity = self.a.identity
        self.b.service.authority = self.a.authority
        self.b.service.action_intents = self.a.service
        self.b.service.audit = ControlPlaneBrokerAuditStore(self.a.sqlite)
        self.a.identity.bootstrap_service_actor(
            identity_id=self.b.worker_actor.identity_id, name="Isolated broker worker",
            scope=self.b.scope, service_scopes=self.b.worker_actor.service_scopes,
        )
        self.params = {"head": "work", "base": "main", "title": "Isolated request"}
        self.sequence = 0

    def create(self, key, *, action="code-host.pull-request.upsert", parameters=None):
        return self.a.service.create(ActionIntentCreate(
            binding_id=self.binding.id,
            request=ActionRequest(
                action_id=action, organization_id="local", workspace_id="default",
                project_id="home", resource_ids=(self.repo.id,),
                parameters=self.params if parameters is None else parameters,
                idempotency_key=key, requested_by=self.a.actor.identity_id,
            ),
            work_item_ref=self.a.work_item.ref, execution_id=self.assignment.execution_id,
        ), actor=self.a.actor, assignment_id=self.assignment.id)

    def histories(self):
        return {i.id: self.a.service.history(i.id, self.a.actor)
                for i in self.a.service.store.load().intents}

    async def submit(self, key, parameters=None):
        self.sequence += 1
        correlation = f"repository-admission-{self.sequence}"
        status, headers, response = await self.b._request(
            "POST", "/api/repository-actions/pull-request/upsert",
            payload={"parameters": self.params if parameters is None else parameters,
                     "idempotency_key": key},
            headers={"X-Correlation-ID": correlation},
        )
        self.assertEqual(headers["x-correlation-id"], correlation)
        audits = [e for e in self.b.service.audit.load().events
                  if e.correlation_id == correlation]
        self.assertEqual(len(audits), 1)
        audit = audits[0]
        self.assertEqual(audit.operation_id, "repository.pull-request.upsert")
        self.assertEqual(audit.response_status, status)
        if "error" in response:
            self.assertEqual(response["error"]["correlation_id"], correlation)
        else:
            self.assertEqual(response["_broker"]["correlation_id"], correlation)
        return status, response, audit

    async def assert_conflict(self, key, parameters):
        before = self.histories()
        calls = list(self.provider.calls)
        with patch.object(self.a.service, "claim", wraps=self.a.service.claim) as claim, \
             patch.object(self.a.service, "execute_claimed", wraps=self.a.service.execute_claimed) as execute:
            status, response, audit = await self.submit(key, parameters)
        self.assertEqual(status, 409, response)
        self.assertEqual(response["error"], {
            "code": "repository_action_admission_conflict", "message": SAFE_MESSAGE,
            "correlation_id": audit.correlation_id,
        })
        self.assertEqual(audit.decision.value, "deny")
        self.assertEqual(audit.denial_reason, SAFE_MESSAGE)
        self.assertEqual(claim.call_count, 0)
        self.assertEqual(execute.call_count, 0)
        self.assertEqual(self.provider.calls, calls)
        self.assertEqual(self.histories(), before)

    async def test_three_observed_create_conflicts_preserve_original_outcomes(self):
        branch = await self.a._execute(self.create(
            "branch-origin", action="code-host.branch.publish",
            parameters={"branch": "work", "head_revision": "a" * 40},
        ))
        succeeded = await self.a._execute(self.create("succeeded-key"))
        self.provider.lose_response = True
        uncertain = await self.a._execute(self.create("uncertain-key"))
        self.provider.lose_response = False
        self.assertEqual(branch.status, ActionIntentStatus.SUCCEEDED)
        self.assertEqual(succeeded.status, ActionIntentStatus.SUCCEEDED)
        self.assertEqual(uncertain.status, ActionIntentStatus.UNCERTAIN)
        self.assertEqual(uncertain.attempt, 1)
        self.assertIsNotNone(uncertain.last_receipt_id)
        self.assertEqual(len(self.provider.calls), 3)
        scenarios = (
            ("wrong-origin", {**self.params, "origin_intent_id": branch.id}),
            ("succeeded-key", {**self.params, "title": "Changed"}),
            ("uncertain-key", {**self.params, "title": "Changed"}),
        )
        for key, parameters in scenarios:
            with self.subTest(key=key):
                await self.assert_conflict(key, parameters)

    async def test_missing_and_foreign_origins_have_the_same_safe_feedback(self):
        origin = self.create("foreign-origin")
        foreign = origin.model_copy(update={
            "organization_id": "foreign", "status": ActionIntentStatus.SUCCEEDED,
        })
        self.a.service.store.update(
            lambda state: state.model_copy(update={"intents": [foreign]}),
            intent_ids=(origin.id,),
        )
        for origin_id in ("action-intent-missing", origin.id):
            with self.subTest(origin_id=origin_id):
                # Foreign history cannot be read by this tenant; compare its
                # stored record directly rather than granting fixture access.
                before = self.a.service.store.get(origin.id).model_dump(mode="json")
                with patch.object(self.a.service, "claim", wraps=self.a.service.claim) as claim:
                    status, response, audit = await self.submit(
                        "new-key", {**self.params, "origin_intent_id": origin_id})
                self.assertEqual(status, 409)
                self.assertEqual(response["error"]["message"], SAFE_MESSAGE)
                self.assertEqual(response["error"]["code"], "repository_action_admission_conflict")
                self.assertEqual((audit.decision.value, audit.denial_reason), ("deny", SAFE_MESSAGE))
                self.assertEqual(claim.call_count, 0)
                self.assertEqual(self.provider.calls, [])
                self.assertEqual(self.a.service.store.get(origin.id).model_dump(mode="json"), before)
                self.assertEqual(len(self.a.service.store.load().intents), 1)

    async def test_execution_scope_and_competing_idempotency_insert_stay_conflicts(self):
        original = await self.a._execute(self.create("competing-key"))
        # Let CREATE's preflight miss an existing key; the real store's atomic
        # insert returns the competing intent and the service rejects mismatch.
        with patch.object(self.a.service.store, "find_idempotent", return_value=None):
            await self.assert_conflict("competing-key", {**self.params, "title": "Changed"})
        self.intent_assignment = self.assignment.model_copy(update={
            "repository_scope": self.assignment.repository_scope.model_copy(update={
                "writable_repository_ids": ("outside",),
            }),
        })
        await self.assert_conflict("new-scope-key", self.params)
        self.assertEqual(self.a.service.get(original.id, self.a.actor).status, ActionIntentStatus.SUCCEEDED)

    async def test_new_and_exact_idempotent_succeeded_or_uncertain_requests(self):
        for key, lost, expected in (
            ("new-success", False, ActionIntentStatus.SUCCEEDED),
            ("new-uncertain", True, ActionIntentStatus.UNCERTAIN),
        ):
            with self.subTest(key=key):
                self.provider.lose_response = lost
                status, result, audit = await self.submit(key)
                self.assertEqual(status, 200, result)
                self.assertEqual(result["item"]["status"], expected.value)
                self.assertEqual(audit.decision.value, "allow")
                self.assertEqual(self.provider.calls.count(key), 1)
                before = self.histories()
                status, repeated, audit = await self.submit(key)
                self.assertEqual(status, 200, repeated)
                self.assertEqual(repeated["item"]["id"], result["item"]["id"])
                self.assertEqual(repeated["item"]["status"], expected.value)
                self.assertEqual(self.histories(), before)
                self.assertEqual(self.provider.calls.count(key), 1)
                if lost:
                    self.assertTrue(before[result["item"]["id"]]["receipts"])

    async def test_postcreate_conflicts_and_internal_errors_remain_errors(self):
        for stage in ("claim", "execute_claimed"):
            for error_type in (ActionIntentConflictError, RuntimeError):
                with self.subTest(stage=stage, error_type=error_type.__name__):
                    before = set(self.histories())
                    key = f"{stage}-{error_type.__name__}"
                    with patch.object(self.a.service, stage, side_effect=error_type("PRIVATE_SENTINEL")):
                        status, response, audit = await self.submit(key)
                    self.assertEqual(status, 500, response)
                    self.assertEqual(audit.decision.value, "error")
                    self.assertEqual(response["error"]["code"], "control_plane_broker_denied")
                    self.assertNotIn("PRIVATE_SENTINEL", json.dumps(response))
                    self.assertNotIn("PRIVATE_SENTINEL", audit.denial_reason)
                    self.assertEqual(len(set(self.histories()) - before), 1)
                    self.assertEqual(self.provider.calls, [])

    async def test_create_conflict_exception_text_and_parameters_are_not_disclosed(self):
        with patch.object(self.a.service, "_change_request_continuation", side_effect=
                          ActionIntentConflictError("PRIVATE_EXCEPTION credentials foreign-origin")):
            await self.assert_conflict("private-key", {**self.params, "body": "PRIVATE_PARAMETER"})

    async def test_create_internal_failure_is_not_classified_from_its_message(self):
        before = self.histories()
        with patch.object(self.a.service.store, "find_idempotent", side_effect=RuntimeError(
            "ActionIntentConflictError PRIVATE_SENTINEL")):
            status, response, audit = await self.submit("internal-create")
        self.assertEqual(status, 500)
        self.assertEqual(audit.decision.value, "error")
        self.assertEqual(response["error"]["code"], "control_plane_broker_denied")
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(response))
        self.assertEqual(self.histories(), before)
        self.assertEqual(self.provider.calls, [])
