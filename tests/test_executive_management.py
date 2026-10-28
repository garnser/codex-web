from __future__ import annotations

import json
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request, HTTPException
from fastapi.testclient import TestClient

from codex_web.api.executive_management import build_executive_management_router
from codex_web.authority import (
    AuthorityDecision,
    AuthorityDecisionOutcome,
)
from codex_web.executive_roles import (
    ExecutiveActivationCreate,
    ExecutiveActivationStatus,
    ExecutiveProposalKind,
    ExecutiveProposalStatus,
    ExecutiveReasoningBudget,
    ExecutiveTriggerKind,
)
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.executive_management import (
    ExecutiveManagementService,
    ExecutiveConsultationError,
    ExecutiveSelectionError,
)
from codex_web.services.executive_roles import install_executive_role_definitions
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.executive_activations import ExecutiveActivationStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Gateway:
    def __init__(self) -> None:
        self.requests = []

    async def invoke(self, request, *, actor):
        self.requests.append(request)
        purpose = request.purpose
        if purpose == "executive-synthesis":
            payload = {
                "recommendation": "Use the bounded option after resolving cost uncertainty.",
                "rationale": "CTO and CFO agree on direction but disagree on timing.",
                "disagreement": ["timing remains disputed"],
                "escalation_required": True,
                "escalation_reason": "material timing disagreement",
            }
        elif purpose == "executive-role:cto":
            payload = {
                "summary": "Architecture can evolve incrementally.",
                "recommendation": "Use the bounded migration.",
                "risks": ["migration defects"],
                "assumptions": [],
                "disagreement": [],
                "proposals": [],
            }
        elif purpose == "executive-role:cfo":
            payload = {
                "summary": "Cost evidence is incomplete.",
                "recommendation": "Delay irreversible spend until measured.",
                "risks": ["unbounded spend"],
                "assumptions": ["current cost data is incomplete"],
                "disagreement": ["timing"],
                "proposals": [],
            }
        elif purpose == "executive-role:chief-of-staff":
            payload = {
                "summary": "A bounded Goal is the minimum sufficient next object.",
                "recommendation": "Create one measurable Goal.",
                "risks": [],
                "assumptions": [],
                "disagreement": [],
                "proposals": [
                    {
                        "kind": "goal",
                        "title": "Create governed Goal",
                        "rationale": "Track the requested outcome canonically.",
                        "payload": {
                            "title": "Improve governed delivery",
                            "description": "Measure and improve governed delivery.",
                            "owner_identity_id": actor.identity_id,
                        },
                    }
                ],
            }
        else:
            raise AssertionError(f"unexpected purpose: {purpose}")
        return SimpleNamespace(
            text=json.dumps(payload),
            invocation=SimpleNamespace(id=f"invocation-{len(self.requests)}"),
        )


class _Authority:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.requests = []

    def evaluate(self, request, *, actor):
        self.requests.append(request)
        return AuthorityDecision(
            outcome=(
                AuthorityDecisionOutcome.ALLOW
                if self.allow
                else AuthorityDecisionOutcome.DENY
            ),
            actor_identity_id=actor.identity_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            request=request,
            reasons=(
                ("test authority allows materialization",)
                if self.allow
                else ("test authority denies materialization",)
            ),
        )


class _Goals:
    def __init__(self) -> None:
        self.created = []
        self.items = []

    def create(
        self,
        payload,
        *,
        scope,
        actor_id,
        originating_executive_activation_id=None,
        originating_executive_proposal_id=None,
    ):
        self.created.append((payload, scope, actor_id))
        item = SimpleNamespace(
            id="goal-created",
            originating_executive_activation_id=originating_executive_activation_id,
            originating_executive_proposal_id=originating_executive_proposal_id,
        )
        self.items.append(item)
        return item

    def list(self, *, scope):
        return tuple(self.items)


class _Decisions:
    def __init__(self) -> None:
        self.items = []

    async def create(
        self,
        payload,
        *,
        actor,
        originating_executive_activation_id=None,
        originating_executive_proposal_id=None,
    ):
        item = SimpleNamespace(
            id="decision-created",
            originating_executive_activation_id=originating_executive_activation_id,
            originating_executive_proposal_id=originating_executive_proposal_id,
        )
        self.items.append(item)
        return item

    def list(self, *, actor):
        return tuple(self.items)

    def get(self, decision_id, *, actor):
        raise AssertionError(f"unexpected Decision lookup: {decision_id}")


class _DecisionWork:
    def __init__(self) -> None:
        self.calls = []

    async def commit(self, decision_id, payload, *, actor):
        self.calls.append((decision_id, payload, actor))
        return SimpleNamespace(id=decision_id, work_links=())


class _WorkItems:
    state_machine = SimpleNamespace()

    def __init__(self) -> None:
        self.state_machine._work_item_state = lambda ref: (_ for _ in ()).throw(
            AssertionError(f"unexpected Work lookup: {ref}")
        )


class _WorkGraph:
    def snapshot(self, project_id, *, scope):
        raise AssertionError(f"unexpected WorkGraph lookup: {project_id}")


class _Evidence:
    def get_evidence(self, evidence_id, *, actor):
        raise AssertionError(f"unexpected Evidence lookup: {evidence_id}")


class _Memory:
    def __init__(self) -> None:
        self.queries = []

    def search(self, query, *, actor):
        self.queries.append((query, actor))
        provenance = SimpleNamespace(
            source_kind=SimpleNamespace(value="repository"),
            source_ref="repo://architecture/ADR-12",
            model_dump=lambda mode=None: {
                "source_kind": "repository",
                "source_ref": "repo://architecture/ADR-12",
                "source_revision": "abc123",
            },
        )
        item = SimpleNamespace(
            knowledge_id="knowledge-policy-a",
            logical_key="policy/database",
            version=3,
            object_type=SimpleNamespace(value="policy"),
            project_id=None,
            title="Database standard",
            summary="Use PostgreSQL for transactional services.",
            context_excerpt="PostgreSQL is the approved company database standard.",
            tags=("database",),
            provenance=provenance,
            classification=SimpleNamespace(value="internal"),
            freshness=SimpleNamespace(value="fresh"),
            score=0.94,
            reasons=("semantic:0.9400", "lexical:1.0000"),
        )
        return SimpleNamespace(
            retrieval_id="memory-retrieval-executive-a",
            items=(item,),
        )


class ExecutiveManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        registry = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        self.roles = install_executive_role_definitions(registry)
        self.store = ExecutiveActivationStore(sqlite)
        self.gateway = _Gateway()
        self.authority = _Authority()
        self.goals = _Goals()
        self.decisions = _Decisions()
        self.decision_work = _DecisionWork()
        self.service = ExecutiveManagementService(
            self.store,
            self.roles,
            self.gateway,
            self.authority,
            self.goals,
            self.decisions,
            self.decision_work,
            _WorkItems(),
            _WorkGraph(),
            _Evidence(),
        )
        self.actor = AuthenticationActor(
            identity_id="owner-a",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            assurance=AuthenticationAssurance.MFA,
            roles=("owner",),
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _consult_cpo_output(self, payload):
        activation = self.service.create(
            ExecutiveActivationCreate(
                subject="Product outcome",
                request="Assess the next product outcome.",
                requested_role_ids=("cpo",),
                budget=ExecutiveReasoningBudget(max_model_calls=1),
            ),
            actor=self.actor,
        )
        result = SimpleNamespace(
            text=json.dumps(payload), invocation=SimpleNamespace(id="invocation-cpo")
        )
        with patch.object(self.gateway, "invoke", new_callable=AsyncMock,
                          return_value=result) as invoke:
            try:
                completed = await self.service.consult(activation.id, actor=self.actor)
            finally:
                invoke.assert_awaited_once()
                self.last_cpo_activation = self.service.get(activation.id, actor=self.actor)
        return completed, invoke.call_args.args[0]

    @staticmethod
    def _cpo_output():
        return dict(summary="Assess the product outcome.", recommendation="Gather evidence.",
                    risks=[], assumptions=[], disagreement=[], context_refs=[], proposals=[])

    async def test_cpo_consultation_receives_typed_output_instructions(self):
        completed, request = await self._consult_cpo_output(self._cpo_output())
        self.assertEqual(completed.status, ExecutiveActivationStatus.COMPLETED)
        self.assertFalse(completed.synthesis.escalation_required)
        self.assertEqual(completed.consultations[0].output.disagreement, ())
        self.assertEqual(request.purpose, "executive-role:cpo")
        for instruction in (
            "summary and recommendation must be non-empty strings",
            "risks, assumptions, disagreement, and context_refs must be non-null arrays of strings",
            "Use [] for empty collections, never null",
            "proposals must be a non-null array of objects",
            "kind, title, and rationale must be non-empty strings",
            "payload must be a JSON object",
        ):
            self.assertIn(instruction, request.system_prompt)

    async def test_invalid_cpo_outputs_fail_without_retry_or_materialization(self):
        invalid = [
            (field, None, "schema validation")
            for field in ("risks", "assumptions", "disagreement", "context_refs", "proposals")
        ] + [
            (field, [42], "schema validation")
            for field in ("risks", "assumptions", "disagreement", "context_refs")
        ] + [
            ("proposals", [None], "schema validation"),
        ]
        invalid.extend(
            ("proposals", [dict(kind="goal", title="Goal", rationale="Reason", payload=value)],
             "schema validation") for value in (None, [], "invalid")
        )
        invalid.extend([
            ("proposals", [dict(kind="work", title="Work", rationale="Reason", payload={})],
             "disallowed kinds"),
            ("context_refs", ["[fact:unavailable@v1]"], "unavailable canonical context"),
        ])
        for field, value, error in invalid:
            with self.subTest(field=field, value=value):
                payload = self._cpo_output()
                payload[field] = value
                with self.assertRaisesRegex(ExecutiveConsultationError, error):
                    await self._consult_cpo_output(payload)
                failed = self.last_cpo_activation
                self.assertEqual(failed.status, ExecutiveActivationStatus.FAILED)
                self.assertIsNone(failed.synthesis)
                self.assertEqual(failed.proposals, ())
                self.assertEqual(self.goals.created, [])
                self.assertEqual(self.decisions.items, [])

    async def test_cpo_disagreement_still_requires_escalation(self):
        payload = self._cpo_output()
        payload["disagreement"] = ["Product timing remains disputed."]
        completed, _ = await self._consult_cpo_output(payload)
        self.assertEqual(completed.status, ExecutiveActivationStatus.ESCALATED)
        self.assertTrue(completed.synthesis.escalation_required)
        self.assertEqual(completed.synthesis.disagreement, tuple(payload["disagreement"]))

    async def test_context_timeout_returns_retryable_error_without_persisting_activation(self):
        original_prepare = self.service.prepare

        def slow_prepare(payload, *, actor):
            time.sleep(0.05)
            return original_prepare(payload, actor=actor)

        self.service.prepare = slow_prepare
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(
            build_executive_management_router(
                self.service,
                context_timeout_seconds=0.01,
            )
        )
        client = TestClient(app)
        try:
            payload = ExecutiveActivationCreate(
                subject="Project context timeout",
                request="Review the project work graph without blocking the web worker.",
                requested_role_ids=("cto",),
            )
            response = client.post(
                "/api/executive/activations",
                json=payload.model_dump(mode="json"),
            )
            self.assertEqual(response.status_code, 503)
            self.assertEqual(
                response.json()["detail"]["code"],
                "executive_context_timeout",
            )
            self.assertTrue(response.json()["detail"]["retryable"])
            time.sleep(0.08)
            self.assertEqual(self.service.list(actor=self.actor), ())
        finally:
            client.close()
            self.service.prepare = original_prepare

    def _create_endpoint(self, *, timeout=0.01):
        router = build_executive_management_router(self.service, context_timeout_seconds=timeout)
        request = Request({"type": "http"})
        request.state.identity_actor = self.actor
        endpoint = next(route.endpoint for route in router.routes
                        if route.path == "/api/executive/activations" and "POST" in route.methods)
        return endpoint, request

    async def test_executor_queue_expiry_never_invokes_prepare_or_persist(self):
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        occupied = threading.Event()
        release = threading.Event()

        def occupy():
            occupied.set()
            release.wait(2)

        owner = loop.run_in_executor(None, occupy)
        for _ in range(100):
            if occupied.is_set():
                break
            await asyncio.sleep(0.001)
        self.assertTrue(occupied.is_set())
        endpoint, request = self._create_endpoint()
        try:
            with patch.object(self.service, "prepare") as prepare:
                with self.assertRaises(HTTPException) as refused:
                    await endpoint(ExecutiveActivationCreate(subject="queue", request="private request"), request)
                self.assertEqual(refused.exception.detail["code"], "executive_context_timeout")
                release.set()
                await owner
                await asyncio.sleep(0.01)
                prepare.assert_not_called()
                self.assertEqual(self.service.list(actor=self.actor), ())
        finally:
            release.set()
            await owner
            executor.shutdown(wait=True)

    async def test_inner_prepare_timeout_is_not_request_deadline(self):
        endpoint, request = self._create_endpoint(timeout=1)
        with patch.object(self.service, "prepare", side_effect=TimeoutError("private dependency detail")):
            with self.assertRaises(HTTPException) as refused:
                await endpoint(ExecutiveActivationCreate(subject="dependency", request="private request"), request)
        self.assertEqual(refused.exception.detail["code"], "executive_context_dependency_timeout")
        self.assertFalse(refused.exception.detail["retryable"])
        self.assertNotIn("private", str(refused.exception.detail))
        self.assertEqual(self.service.list(actor=self.actor), ())

    async def test_persist_timeout_after_create_reports_unknown_persisted_outcome(self):
        endpoint, request = self._create_endpoint(timeout=1)

        def persist_then_timeout(activation, *, actor):
            self.store.create(activation)
            raise TimeoutError("private storage detail")

        with patch.object(self.service, "persist_prepared", side_effect=persist_then_timeout):
            with self.assertRaises(HTTPException) as refused:
                await endpoint(ExecutiveActivationCreate(subject="persist", request="private request", requested_role_ids=("cto",)), request)
        self.assertEqual(refused.exception.detail["code"], "executive_activation_persistence_timeout")
        self.assertEqual(refused.exception.detail["outcome"], "unknown")
        self.assertFalse(refused.exception.detail["retryable"])
        self.assertNotIn("no activation", refused.exception.detail["message"])
        self.assertNotIn("private", str(refused.exception.detail))
        self.assertEqual(len(self.service.list(actor=self.actor)), 1)

    async def test_context_stage_events_exclude_payload_and_exception_content(self):
        with patch("codex_web.observability.log_event") as observe:
            self.service.prepare(ExecutiveActivationCreate(
                subject="private-subject-marker", request="private-request-marker",
                requested_role_ids=("cto",)), actor=self.actor)
        self.assertGreaterEqual(observe.call_count, 3)
        for call in observe.call_args_list:
            fields = call.kwargs
            self.assertEqual(set(fields), {"stage", "outcome", "duration_seconds", "tenant_id", "workspace_id"})
            self.assertIn(fields["stage"], {"catalog", "business_scope", "context", "work_graph", "memory_search"})
            self.assertGreaterEqual(fields["duration_seconds"], 0)
            self.assertEqual(fields["tenant_id"], self.actor.organization_id)
            self.assertNotIn("private-", str(fields))

    async def test_stage_telemetry_never_masks_original_refusal(self):
        from codex_web.observability import operation_stage
        import logging
        original = RuntimeError("private refusal detail")
        with patch("codex_web.observability.log_event", side_effect=asyncio.CancelledError):
            with self.assertRaises(RuntimeError) as refused:
                with operation_stage(logging.getLogger(__name__), "executive.context.stage", "context"):
                    raise original
        self.assertIs(refused.exception, original)

    async def test_deterministic_selection_invokes_only_relevant_roles_and_preserves_disagreement(self):
        activation = self.service.create(
            ExecutiveActivationCreate(
                subject="Architecture cost trade-off",
                request=(
                    "Review the architecture migration and cloud cost constraints."
                ),
                max_roles=2,
                budget=ExecutiveReasoningBudget(
                    max_input_tokens=12000,
                    max_output_tokens=4000,
                    max_model_calls=3,
                    max_cost_usd=3,
                ),
            ),
            actor=self.actor,
        )

        self.assertEqual(
            [item.role_id for item in activation.selections],
            ["cto", "cfo"],
        )
        completed = await self.service.consult(
            activation.id,
            actor=self.actor,
        )

        self.assertEqual(completed.status, ExecutiveActivationStatus.ESCALATED)
        self.assertEqual(
            [item.role_id for item in completed.consultations],
            ["cto", "cfo"],
        )
        self.assertEqual(len(self.gateway.requests), 3)
        self.assertEqual(
            {item.purpose for item in self.gateway.requests},
            {"executive-role:cto", "executive-role:cfo", "executive-synthesis"},
        )
        self.assertTrue(completed.synthesis.escalation_required)
        self.assertIn("timing", " ".join(completed.synthesis.disagreement))
        self.assertEqual(
            len(self.service.revisions(activation.id, actor=self.actor)),
            3,
        )

    async def test_event_subscription_routes_only_subscribed_roles(self):
        activation = self.service.create(
            ExecutiveActivationCreate(
                subject="Approval transition",
                request="Review the approval transition.",
                trigger_kind=ExecutiveTriggerKind.EVENT,
                event_type="approval.transition",
                max_roles=3,
            ),
            actor=self.actor,
        )
        selected = {item.role_id for item in activation.selections}
        self.assertIn("chief-of-staff", selected)
        self.assertIn("cfo", selected)
        self.assertLessEqual(len(selected), 3)

    async def test_budget_fails_before_any_model_invocation(self):
        with self.assertRaisesRegex(
            ExecutiveSelectionError,
            "cannot cover selected roles plus synthesis",
        ):
            self.service.create(
                ExecutiveActivationCreate(
                    subject="Explicit consultation",
                    request="Review this jointly.",
                    requested_role_ids=("cto", "cfo"),
                    budget=ExecutiveReasoningBudget(
                        max_input_tokens=10000,
                        max_output_tokens=3000,
                        max_model_calls=2,
                        max_cost_usd=2,
                    ),
                ),
                actor=self.actor,
            )
        self.assertEqual(self.gateway.requests, [])

    async def test_advisory_goal_proposal_requires_canonical_authority_before_materialization(self):
        activation = self.service.create(
            ExecutiveActivationCreate(
                subject="Governed outcome",
                request="Frame the minimum sufficient canonical Goal.",
                requested_role_ids=("chief-of-staff",),
                budget=ExecutiveReasoningBudget(
                    max_input_tokens=8000,
                    max_output_tokens=2000,
                    max_model_calls=1,
                    max_cost_usd=1,
                ),
            ),
            actor=self.actor,
        )
        completed = await self.service.consult(
            activation.id,
            actor=self.actor,
        )
        self.assertEqual(completed.status, ExecutiveActivationStatus.COMPLETED)
        self.assertEqual(len(completed.proposals), 1)
        proposal = completed.proposals[0]
        self.assertEqual(proposal.kind, ExecutiveProposalKind.GOAL)
        self.assertEqual(proposal.status, ExecutiveProposalStatus.PROPOSED)
        self.assertEqual(self.goals.created, [])

        updated = await self.service.materialize(
            completed.id,
            proposal.id,
            actor=self.actor,
        )

        self.assertEqual(len(self.authority.requests), 1)
        self.assertEqual(
            self.authority.requests[0].capability,
            "executive.goal.materialize",
        )
        self.assertEqual(len(self.goals.created), 1)
        materialized = next(
            item for item in updated.proposals if item.id == proposal.id
        )
        self.assertEqual(
            materialized.status,
            ExecutiveProposalStatus.MATERIALIZED,
        )
        self.assertEqual(materialized.resulting_ref, "goal:goal-created")
        self.assertEqual(materialized.materialized_by, self.actor.identity_id)
        self.assertEqual(
            self.goals.items[0].originating_executive_activation_id,
            completed.id,
        )
        self.assertEqual(
            self.goals.items[0].originating_executive_proposal_id,
            proposal.id,
        )

    async def test_authority_denial_keeps_proposal_advisory_and_side_effect_free(self):
        self.authority.allow = False
        activation = self.service.create(
            ExecutiveActivationCreate(
                subject="Governed outcome",
                request="Frame the minimum sufficient canonical Goal.",
                requested_role_ids=("chief-of-staff",),
                budget=ExecutiveReasoningBudget(
                    max_input_tokens=8000,
                    max_output_tokens=2000,
                    max_model_calls=1,
                    max_cost_usd=1,
                ),
            ),
            actor=self.actor,
        )
        completed = await self.service.consult(
            activation.id,
            actor=self.actor,
        )
        proposal = completed.proposals[0]

        with self.assertRaisesRegex(Exception, "denied"):
            await self.service.materialize(
                completed.id,
                proposal.id,
                actor=self.actor,
            )

        self.assertEqual(self.goals.created, [])
        current = self.service.get(completed.id, actor=self.actor)
        stored = next(
            item for item in current.proposals if item.id == proposal.id
        )
        self.assertEqual(stored.status, ExecutiveProposalStatus.PROPOSED)
        self.assertEqual(
            stored.authority_capability,
            "executive.goal.materialize",
        )
        self.assertTrue(stored.authority_reasons)


    async def test_canonical_executive_context_retrieves_memory_and_preserves_citation(self):
        memory = _Memory()
        self.service.organizational_memory = memory
        activation = self.service.create(
            ExecutiveActivationCreate(
                subject="Database architecture",
                request="Choose the database for a new transactional service.",
                requested_role_ids=("cto",),
                budget=ExecutiveReasoningBudget(
                    max_input_tokens=12000,
                    max_output_tokens=3000,
                    max_model_calls=1,
                    max_cost_usd=2,
                ),
            ),
            actor=self.actor,
        )

        self.assertEqual(
            activation.context.memory_retrieval_ids,
            ("memory-retrieval-executive-a",),
        )
        self.assertEqual(
            activation.context.memory[0]["citation"],
            "[memory:knowledge-policy-a@v3]",
        )
        query, query_actor = memory.queries[0]
        self.assertEqual(query.project_ids, ())
        self.assertFalse(query.include_company_scope)
        self.assertEqual(query_actor.identity_id, self.actor.identity_id)

        completed = await self.service.consult(
            activation.id,
            actor=self.actor,
        )
        self.assertEqual(completed.status, ExecutiveActivationStatus.COMPLETED)
        request = next(
            item
            for item in self.gateway.requests
            if item.purpose == "executive-role:cto"
        )
        self.assertIn(
            "[memory:knowledge-policy-a@v3]",
            request.messages[0].content,
        )
        self.assertIn(
            "[memory:<id>@v<version>]",
            request.system_prompt,
        )



if __name__ == "__main__":
    unittest.main()
