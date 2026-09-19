from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from codex_web.definitions import (
    DefinitionLifecycle,
    DefinitionRecord,
    DefinitionScope,
    definition_checksum,
    reference_for,
)
from codex_web.evaluations import (
    EvaluationBudget,
    EvaluationEventFixture,
    EvaluationExpectedInvariants,
    EvaluationFailureInjection,
    EvaluationRegressionThresholds,
    EvaluationReplayFixture,
    EvaluationRunMode,
    EvaluationRunRequest,
    EvaluationScenarioCreate,
    EvaluationStateSnapshot,
    EvaluationTerminalOutcome,
    EvaluationTrace,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    PrincipalKind,
)
from codex_web.services.evaluations import EvaluationService
from codex_web.storage.evaluations import EvaluationStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Definitions:
    def __init__(self, records):
        self.records = {item.record_id: item for item in records}

    def get_record(self, record_id):
        return self.records[record_id]


class _Evidence:
    def __init__(self):
        self.created = []

    def create_evidence(self, payload, *, actor):
        self.created.append((payload, actor))
        return SimpleNamespace(id=f"evidence-evaluation-{len(self.created)}")


def _record(revision: int, payload: dict) -> DefinitionRecord:
    definition_id = "authority.roles.default"
    kind = "authority-role-set"
    schema = "1.0"
    return DefinitionRecord(
        record_id=f"definition-record-{revision}",
        definition_id=definition_id,
        kind=kind,
        definition_schema_version=schema,
        revision=revision,
        scope_type=DefinitionScope.WORKSPACE,
        scope_id="default",
        lifecycle=(
            DefinitionLifecycle.SUPERSEDED
            if revision == 1
            else DefinitionLifecycle.VALIDATED
        ),
        payload=payload,
        checksum=definition_checksum(
            definition_id=definition_id,
            kind=kind,
            definition_schema_version=schema,
            payload=payload,
        ),
        created_by="owner-a",
    )


class EvaluationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.historical_record = _record(1, {"roles": [{"id": "developer"}]})
        self.candidate_record = _record(
            2,
            {"roles": [{"id": "developer"}, {"id": "reviewer"}]},
        )
        self.historical_ref = reference_for(self.historical_record)
        self.candidate_ref = reference_for(self.candidate_record)
        self.evidence = _Evidence()
        self.service = EvaluationService(
            EvaluationStore(sqlite),
            _Definitions([self.historical_record, self.candidate_record]),
            artifact_evidence=self.evidence,
            clock=lambda: 1900000000.0,
        )
        self.actor = AuthenticationActor(
            identity_id="owner-a",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            assurance=AuthenticationAssurance.MFA,
            roles=("owner",),
        )

    def tearDown(self):
        self.temp.cleanup()

    def scenario(self):
        historical = EvaluationTrace(
            terminal_outcome=EvaluationTerminalOutcome.SUCCEEDED,
            state_transitions=("work.ready", "work.completed"),
            selected_role_ids=("developer",),
            resolved_definitions=(self.historical_ref,),
            actions=(),
            evidence_ids=("evidence-tests",),
            reasoning_calls=1,
            retries=0,
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.10,
            latency_seconds=2.0,
        )
        candidate = historical.model_copy(
            update={
                "resolved_definitions": (self.candidate_ref,),
                "input_tokens": 180,
                "output_tokens": 80,
                "cost_usd": 0.20,
                "latency_seconds": 2.2,
            }
        )
        timeout = historical.model_copy(
            update={
                "terminal_outcome": EvaluationTerminalOutcome.BLOCKED,
                "resolved_definitions": (self.historical_ref,),
                "retries": 2,
                "policy_violations": (),
            }
        )
        return EvaluationScenarioCreate(
            scenario_id="work.lifecycle",
            version="1.0",
            title="Canonical work lifecycle",
            description="Recorded replay for deterministic autonomy regression.",
            suite_ids=("autonomy-smoke", "release-qualification"),
            starting_state=EvaluationStateSnapshot(
                object_refs=("work:TASK-1",),
                fingerprint_sha256="a" * 64,
                description="sanitized canonical work state",
            ),
            events=(
                EvaluationEventFixture(
                    event_id="evt-1",
                    event_type="work.transition",
                    source="fixture",
                    occurred_at=100.0,
                    payload={"ref": "TASK-1", "stage": "ready"},
                ),
            ),
            historical_definitions=(self.historical_ref,),
            expected=EvaluationExpectedInvariants(
                terminal_outcomes=(
                    EvaluationTerminalOutcome.SUCCEEDED,
                    EvaluationTerminalOutcome.BLOCKED,
                ),
                required_state_transitions=("work.ready",),
                expected_selected_role_ids=("developer",),
                required_evidence_ids=("evidence-tests",),
                forbid_policy_violations=True,
            ),
            budget=EvaluationBudget(
                max_model_calls=3,
                max_input_tokens=500,
                max_output_tokens=200,
                max_cost_usd=1.0,
                max_actions=0,
                max_retries=3,
                max_latency_seconds=10,
            ),
            regression_thresholds=EvaluationRegressionThresholds(
                max_token_increase_ratio=0.20,
                max_cost_increase_ratio=0.20,
                max_latency_increase_ratio=0.50,
                max_action_increase=0,
                max_intervention_increase=0,
                allow_definition_resolution_changes=True,
            ),
            replay_fixtures=(
                EvaluationReplayFixture(
                    id="historical",
                    mode=EvaluationRunMode.HISTORICAL,
                    trace=historical,
                ),
                EvaluationReplayFixture(
                    id="candidate-role-v2",
                    mode=EvaluationRunMode.CANDIDATE,
                    definition_overrides=(self.candidate_ref,),
                    trace=candidate,
                ),
                EvaluationReplayFixture(
                    id="provider-timeout",
                    mode=EvaluationRunMode.FAILURE_INJECTION,
                    failure_injection=EvaluationFailureInjection.PROVIDER_TIMEOUT,
                    trace=timeout,
                ),
            ),
            allowed_failure_injections=(
                EvaluationFailureInjection.PROVIDER_TIMEOUT,
            ),
        )

    async def test_historical_and_candidate_runs_pin_exact_definitions_and_detect_regression(self):
        scenario = self.service.create_scenario(self.scenario(), actor=self.actor)

        baseline, comparison = await self.service.run(
            EvaluationRunRequest(
                scenario_id=scenario.scenario_id,
                scenario_version=scenario.version,
                replay_fixture_id="historical",
            ),
            actor=self.actor,
        )
        self.assertTrue(baseline.passed)
        self.assertIsNone(comparison)
        self.assertEqual(baseline.definitions, (self.historical_ref,))
        self.assertEqual(baseline.evidence_id, "evidence-evaluation-1")

        candidate, comparison = await self.service.run(
            EvaluationRunRequest(
                scenario_id=scenario.scenario_id,
                scenario_version=scenario.version,
                replay_fixture_id="candidate-role-v2",
                baseline_run_id=baseline.id,
            ),
            actor=self.actor,
        )

        self.assertTrue(candidate.passed)
        self.assertEqual(candidate.definitions, (self.candidate_ref,))
        self.assertIsNotNone(comparison)
        self.assertFalse(comparison.passed)
        self.assertTrue(comparison.definition_resolution_changed)
        self.assertGreater(comparison.token_delta, 0)
        self.assertGreater(comparison.cost_delta_usd, 0)
        self.assertIn(
            "token usage regression exceeds configured threshold",
            comparison.regression_reasons,
        )
        self.assertIn(
            "cost regression exceeds configured threshold",
            comparison.regression_reasons,
        )

    async def test_failure_injection_replays_recorded_blocked_path_without_live_provider(self):
        scenario = self.service.create_scenario(self.scenario(), actor=self.actor)
        run, _ = await self.service.run(
            EvaluationRunRequest(
                scenario_id=scenario.scenario_id,
                scenario_version=scenario.version,
                replay_fixture_id="provider-timeout",
            ),
            actor=self.actor,
        )

        self.assertEqual(
            run.failure_injection,
            EvaluationFailureInjection.PROVIDER_TIMEOUT,
        )
        self.assertEqual(
            run.trace.terminal_outcome,
            EvaluationTerminalOutcome.BLOCKED,
        )
        self.assertTrue(run.passed)
        self.assertEqual(run.backend_id, "recorded")

    async def test_named_suite_produces_qualification_state_and_evidence(self):
        self.service.create_scenario(self.scenario(), actor=self.actor)
        suite = await self.service.run_suite(
            "autonomy-smoke",
            actor=self.actor,
        )

        self.assertTrue(suite.passed)
        self.assertEqual(len(suite.run_ids), 1)
        self.assertEqual(len(suite.evidence_ids), 1)
        self.assertEqual(len(self.evidence.created), 1)
        payload, _actor = self.evidence.created[0]
        self.assertEqual(payload.source, "autonomy-evaluation")
        self.assertTrue(payload.metadata["passed"])

    def test_scenario_versions_are_immutable(self):
        payload = self.scenario()
        self.service.create_scenario(payload, actor=self.actor)
        with self.assertRaisesRegex(
            RuntimeError,
            "evaluation scenario already exists",
        ):
            self.service.create_scenario(payload, actor=self.actor)

    def test_sensitive_event_fixture_keys_are_rejected(self):
        with self.assertRaises(ValidationError):
            EvaluationEventFixture(
                event_id="evt-secret",
                event_type="failure.observed",
                source="fixture",
                occurred_at=1.0,
                payload={"api_key": "must-not-be-stored"},
            )


if __name__ == "__main__":
    unittest.main()
