from __future__ import annotations

import time
from typing import Protocol

from codex_web.artifact_evidence import EvidenceCreate, EvidenceResult, EvidenceType
from codex_web.definitions import (
    DefinitionReference,
    DefinitionScope,
    reference_for,
)
from codex_web.evaluations import (
    EvaluationAssertionKind,
    EvaluationAssertionResult,
    EvaluationComparison,
    EvaluationReplayFixture,
    EvaluationRun,
    EvaluationRunMode,
    EvaluationRunRequest,
    EvaluationScenario,
    EvaluationScenarioCreate,
    EvaluationSuiteRun,
    EvaluationTrace,
)
from codex_web.identity import AuthenticationActor
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.storage.evaluations import (
    EvaluationConflictError,
    EvaluationNotFoundError,
    EvaluationStore,
)


class EvaluationError(RuntimeError):
    pass


class EvaluationReplayBackend(Protocol):
    id: str

    async def replay(
        self,
        scenario: EvaluationScenario,
        fixture: EvaluationReplayFixture,
        *,
        actor: AuthenticationActor,
    ) -> EvaluationTrace:
        """Return an offline trace. Implementations must not call live side-effect providers."""


class RecordedEvaluationReplayBackend:
    """Safe default: replay an immutable recorded/mock trace from the scenario."""

    id = "recorded"

    async def replay(
        self,
        scenario: EvaluationScenario,
        fixture: EvaluationReplayFixture,
        *,
        actor: AuthenticationActor,
    ) -> EvaluationTrace:
        del scenario, actor
        return fixture.trace.model_copy(deep=True)


class EvaluationService:
    def __init__(
        self,
        store: EvaluationStore,
        definitions: DefinitionRegistryService,
        *,
        artifact_evidence: ArtifactEvidenceService | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.definitions = definitions
        self.artifact_evidence = artifact_evidence
        self.clock = clock
        self._backends: dict[str, EvaluationReplayBackend] = {}
        self.register_backend(RecordedEvaluationReplayBackend())

    def register_backend(self, backend: EvaluationReplayBackend) -> None:
        backend_id = str(getattr(backend, "id", "") or "").strip()
        if not backend_id:
            raise EvaluationError("evaluation replay backend requires an id")
        existing = self._backends.get(backend_id)
        if existing is not None and existing is not backend:
            raise EvaluationConflictError(
                f"evaluation replay backend already registered: {backend_id}"
            )
        self._backends[backend_id] = backend

    def backend_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._backends))

    @staticmethod
    def _same_scope(item, actor: AuthenticationActor) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    @staticmethod
    def _definition_key(reference: DefinitionReference) -> tuple[str, str]:
        return (reference.definition_id, reference.kind)

    def _validate_reference(
        self,
        reference: DefinitionReference,
        *,
        actor: AuthenticationActor,
        project_id: str | None,
    ) -> None:
        try:
            record = self.definitions.get_record(reference.record_id)
        except Exception as exc:
            raise EvaluationError(
                f"pinned definition record is unavailable: {reference.record_id}"
            ) from exc
        actual = reference_for(record)
        if actual != reference:
            raise EvaluationError(
                f"pinned definition reference does not match immutable record: "
                f"{reference.definition_id}@{reference.revision}"
            )
        if record.scope_type == DefinitionScope.ORGANIZATION:
            if record.scope_id != actor.organization_id:
                raise EvaluationError("pinned organization definition is outside tenant scope")
        elif record.scope_type == DefinitionScope.WORKSPACE:
            if record.scope_id != actor.workspace_id:
                raise EvaluationError("pinned workspace definition is outside tenant scope")
        elif record.scope_type == DefinitionScope.PROJECT:
            if not project_id or record.scope_id != project_id:
                raise EvaluationError("pinned project definition is outside scenario project scope")

    def _validate_definition_set(
        self,
        references: tuple[DefinitionReference, ...],
        *,
        actor: AuthenticationActor,
        project_id: str | None,
    ) -> None:
        seen: set[tuple[str, str]] = set()
        for reference in references:
            key = self._definition_key(reference)
            if key in seen:
                raise EvaluationError(
                    f"evaluation definition set contains duplicate slot: "
                    f"{reference.definition_id}/{reference.kind}"
                )
            seen.add(key)
            self._validate_reference(
                reference,
                actor=actor,
                project_id=project_id,
            )

    def create_scenario(
        self,
        payload: EvaluationScenarioCreate,
        *,
        actor: AuthenticationActor,
    ) -> EvaluationScenario:
        self._validate_definition_set(
            payload.historical_definitions,
            actor=actor,
            project_id=payload.project_id,
        )
        historical_keys = {
            self._definition_key(reference)
            for reference in payload.historical_definitions
        }
        for fixture in payload.replay_fixtures:
            self._validate_definition_set(
                fixture.definition_overrides,
                actor=actor,
                project_id=payload.project_id,
            )
            override_keys = [
                self._definition_key(reference)
                for reference in fixture.definition_overrides
            ]
            if len(override_keys) != len(set(override_keys)):
                raise EvaluationError("candidate definition overrides contain duplicate slots")
            if fixture.mode == EvaluationRunMode.CANDIDATE:
                unknown = set(override_keys) - historical_keys
                if unknown:
                    raise EvaluationError(
                        "candidate definition overrides must replace historical definition slots"
                    )
        scenario = EvaluationScenario(
            **payload.model_dump(mode="python"),
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            created_by=actor.identity_id,
            created_at=float(self.clock()),
            fixture_checksum_sha256=EvaluationScenario.checksum(payload),
        )
        return self.store.create_scenario(scenario)

    def list_scenarios(
        self,
        actor: AuthenticationActor,
        *,
        suite_id: str | None = None,
    ) -> tuple[EvaluationScenario, ...]:
        rows = self.store.list_scenarios(
            actor.organization_id,
            actor.workspace_id,
        )
        if suite_id is not None:
            rows = tuple(item for item in rows if suite_id in item.suite_ids)
        return rows

    def get_scenario(
        self,
        scenario_id: str,
        version: str,
        *,
        actor: AuthenticationActor,
    ) -> EvaluationScenario:
        return self.store.get_scenario(
            actor.organization_id,
            actor.workspace_id,
            scenario_id,
            version,
        )

    @staticmethod
    def _fixture(
        scenario: EvaluationScenario,
        fixture_id: str,
    ) -> EvaluationReplayFixture:
        item = next(
            (fixture for fixture in scenario.replay_fixtures if fixture.id == fixture_id),
            None,
        )
        if item is None:
            raise EvaluationNotFoundError(
                f"evaluation replay fixture not found: {fixture_id}"
            )
        return item

    @staticmethod
    def _effective_definitions(
        scenario: EvaluationScenario,
        fixture: EvaluationReplayFixture,
    ) -> tuple[DefinitionReference, ...]:
        by_key = {
            (item.definition_id, item.kind): item
            for item in scenario.historical_definitions
        }
        for item in fixture.definition_overrides:
            by_key[(item.definition_id, item.kind)] = item
        return tuple(
            by_key[key]
            for key in sorted(by_key)
        )

    @staticmethod
    def _effective_runtime(
        scenario: EvaluationScenario,
        fixture: EvaluationReplayFixture,
    ):
        return fixture.runtime_override or scenario.runtime

    @staticmethod
    def _effective_models(
        scenario: EvaluationScenario,
        fixture: EvaluationReplayFixture,
    ):
        return fixture.model_overrides or scenario.models

    @staticmethod
    def _assertions(
        scenario: EvaluationScenario,
        trace: EvaluationTrace,
        definitions: tuple[DefinitionReference, ...],
        runtime,
        models,
    ) -> tuple[EvaluationAssertionResult, ...]:
        expected = scenario.expected
        budget = scenario.budget
        rows: list[EvaluationAssertionResult] = []

        def add(kind, passed, expected_value, actual_value, detail=None):
            rows.append(
                EvaluationAssertionResult(
                    kind=kind,
                    passed=bool(passed),
                    expected=str(expected_value),
                    actual=str(actual_value),
                    detail=detail,
                )
            )

        allowed_outcomes = {item.value for item in expected.terminal_outcomes}
        add(
            EvaluationAssertionKind.TERMINAL_OUTCOME,
            trace.terminal_outcome.value in allowed_outcomes,
            ",".join(sorted(allowed_outcomes)),
            trace.terminal_outcome.value,
        )

        missing_transitions = [
            item
            for item in expected.required_state_transitions
            if item not in set(trace.state_transitions)
        ]
        add(
            EvaluationAssertionKind.STATE_TRANSITION,
            not missing_transitions,
            ",".join(expected.required_state_transitions) or "none",
            ",".join(trace.state_transitions) or "none",
            (
                "missing: " + ", ".join(missing_transitions)
                if missing_transitions
                else None
            ),
        )

        if expected.expected_selected_role_ids:
            add(
                EvaluationAssertionKind.SELECTED_ROLES,
                tuple(trace.selected_role_ids)
                == tuple(expected.expected_selected_role_ids),
                ",".join(expected.expected_selected_role_ids),
                ",".join(trace.selected_role_ids),
            )

        expected_definitions = {
            (
                item.definition_id,
                item.kind,
                item.revision,
                item.record_id,
                item.checksum,
            )
            for item in definitions
        }
        actual_definitions = {
            (
                item.definition_id,
                item.kind,
                item.revision,
                item.record_id,
                item.checksum,
            )
            for item in trace.resolved_definitions
        }
        add(
            EvaluationAssertionKind.DEFINITION_SET,
            actual_definitions == expected_definitions,
            f"{len(expected_definitions)} exact pinned definitions",
            f"{len(actual_definitions)} resolved definitions",
            None if actual_definitions == expected_definitions else "exact definition set mismatch",
        )

        if runtime is not None:
            add(
                EvaluationAssertionKind.RUNTIME_PIN,
                trace.runtime == runtime,
                runtime.model_dump_json(),
                trace.runtime.model_dump_json() if trace.runtime is not None else "none",
            )
        if models:
            add(
                EvaluationAssertionKind.MODEL_PINS,
                tuple(trace.models) == tuple(models),
                "|".join(item.model_dump_json() for item in models),
                "|".join(item.model_dump_json() for item in trace.models) or "none",
            )

        action_kinds = [item.action_kind for item in trace.actions]
        add(
            EvaluationAssertionKind.ACTION_COUNT,
            len(trace.actions) <= budget.max_actions,
            f"<= {budget.max_actions}",
            len(trace.actions),
        )
        if expected.allowed_action_kinds:
            disallowed = sorted(
                set(action_kinds) - set(expected.allowed_action_kinds)
            )
            add(
                EvaluationAssertionKind.ALLOWED_ACTIONS,
                not disallowed,
                ",".join(expected.allowed_action_kinds),
                ",".join(action_kinds) or "none",
                "disallowed: " + ", ".join(disallowed) if disallowed else None,
            )
        forbidden = sorted(
            set(action_kinds) & set(expected.forbidden_action_kinds)
        )
        add(
            EvaluationAssertionKind.FORBIDDEN_ACTIONS,
            not forbidden,
            "none of " + (",".join(expected.forbidden_action_kinds) or "[]"),
            ",".join(forbidden) or "none",
        )

        missing_evidence = sorted(
            set(expected.required_evidence_ids) - set(trace.evidence_ids)
        )
        if expected.required_evidence_ids:
            add(
                EvaluationAssertionKind.EVIDENCE,
                not missing_evidence,
                ",".join(expected.required_evidence_ids),
                ",".join(trace.evidence_ids) or "none",
                (
                    "missing: " + ", ".join(missing_evidence)
                    if missing_evidence
                    else None
                ),
            )

        add(
            EvaluationAssertionKind.RETRIES,
            trace.retries <= budget.max_retries,
            f"<= {budget.max_retries}",
            trace.retries,
        )
        add(
            EvaluationAssertionKind.LATENCY,
            trace.latency_seconds <= budget.max_latency_seconds,
            f"<= {budget.max_latency_seconds}",
            trace.latency_seconds,
        )
        add(
            EvaluationAssertionKind.TOKENS,
            (
                trace.reasoning_calls <= budget.max_model_calls
                and trace.input_tokens <= budget.max_input_tokens
                and trace.output_tokens <= budget.max_output_tokens
            ),
            (
                f"calls<={budget.max_model_calls},input<={budget.max_input_tokens},"
                f"output<={budget.max_output_tokens}"
            ),
            (
                f"calls={trace.reasoning_calls},input={trace.input_tokens},"
                f"output={trace.output_tokens}"
            ),
        )
        add(
            EvaluationAssertionKind.COST,
            trace.cost_usd <= budget.max_cost_usd,
            f"<= {budget.max_cost_usd}",
            trace.cost_usd,
        )
        if expected.require_no_llm:
            add(
                EvaluationAssertionKind.NO_LLM,
                trace.reasoning_calls == 0 and trace.total_tokens == 0,
                "0 model calls and 0 tokens",
                f"{trace.reasoning_calls} calls, {trace.total_tokens} tokens",
            )
        if expected.forbid_policy_violations:
            add(
                EvaluationAssertionKind.POLICY_VIOLATIONS,
                not trace.policy_violations,
                "none",
                ",".join(trace.policy_violations) or "none",
            )
        if expected.max_interventions is not None:
            add(
                EvaluationAssertionKind.POLICY_VIOLATIONS,
                trace.intervention_count <= expected.max_interventions,
                f"interventions <= {expected.max_interventions}",
                f"interventions = {trace.intervention_count}",
            )
        if expected.min_quality_score is not None:
            add(
                EvaluationAssertionKind.QUALITY_SCORE,
                (
                    trace.quality_score is not None
                    and trace.quality_score >= expected.min_quality_score
                ),
                f">= {expected.min_quality_score}",
                trace.quality_score if trace.quality_score is not None else "unavailable",
            )
        return tuple(rows)

    def _comparison(
        self,
        scenario: EvaluationScenario,
        baseline: EvaluationRun,
        candidate: EvaluationRun,
    ) -> EvaluationComparison:
        if (
            baseline.scenario_id != candidate.scenario_id
            or baseline.scenario_version != candidate.scenario_version
            or baseline.organization_id != candidate.organization_id
            or baseline.workspace_id != candidate.workspace_id
        ):
            raise EvaluationError("baseline and candidate runs must share scenario and tenant")

        thresholds = scenario.regression_thresholds
        reasons: list[str] = []
        if baseline.passed and not candidate.passed:
            reasons.append("candidate failed deterministic assertions while baseline passed")
        if len(candidate.trace.policy_violations) > len(baseline.trace.policy_violations):
            reasons.append("candidate introduced additional policy violations")

        def ratio_exceeded(candidate_value: float, baseline_value: float, limit: float) -> bool:
            if baseline_value <= 0:
                return candidate_value > 0
            return ((candidate_value - baseline_value) / baseline_value) > limit

        if ratio_exceeded(
            candidate.trace.total_tokens,
            baseline.trace.total_tokens,
            thresholds.max_token_increase_ratio,
        ):
            reasons.append("token usage regression exceeds configured threshold")
        if ratio_exceeded(
            candidate.trace.cost_usd,
            baseline.trace.cost_usd,
            thresholds.max_cost_increase_ratio,
        ):
            reasons.append("cost regression exceeds configured threshold")
        if ratio_exceeded(
            candidate.trace.latency_seconds,
            baseline.trace.latency_seconds,
            thresholds.max_latency_increase_ratio,
        ):
            reasons.append("latency regression exceeds configured threshold")
        if (
            len(candidate.trace.actions) - len(baseline.trace.actions)
            > thresholds.max_action_increase
        ):
            reasons.append("action-count regression exceeds configured threshold")
        if (
            candidate.trace.intervention_count - baseline.trace.intervention_count
            > thresholds.max_intervention_increase
        ):
            reasons.append("human-intervention regression exceeds configured threshold")

        baseline_defs = {
            (item.definition_id, item.kind, item.record_id, item.checksum)
            for item in baseline.definitions
        }
        candidate_defs = {
            (item.definition_id, item.kind, item.record_id, item.checksum)
            for item in candidate.definitions
        }
        definition_changed = baseline_defs != candidate_defs
        if definition_changed and not thresholds.allow_definition_resolution_changes:
            reasons.append("definition resolution changed outside allowed regression policy")

        runtime_changed = baseline.runtime != candidate.runtime
        if runtime_changed and not thresholds.allow_runtime_changes:
            reasons.append("runtime pin changed outside allowed regression policy")

        baseline_models = tuple(
            (
                item.provider_id,
                item.model_id,
                item.model_version,
                item.prompt_template_id,
                item.prompt_template_version,
                item.prompt_template_checksum_sha256,
                item.policy_fingerprint_sha256,
            )
            for item in baseline.models
        )
        candidate_models = tuple(
            (
                item.provider_id,
                item.model_id,
                item.model_version,
                item.prompt_template_id,
                item.prompt_template_version,
                item.prompt_template_checksum_sha256,
                item.policy_fingerprint_sha256,
            )
            for item in candidate.models
        )
        model_changed = baseline_models != candidate_models
        if model_changed and not thresholds.allow_model_prompt_changes:
            reasons.append("model/prompt pin changed outside allowed regression policy")

        comparison = EvaluationComparison(
            organization_id=candidate.organization_id,
            workspace_id=candidate.workspace_id,
            scenario_id=candidate.scenario_id,
            scenario_version=candidate.scenario_version,
            baseline_run_id=baseline.id,
            candidate_run_id=candidate.id,
            success_changed=baseline.passed != candidate.passed,
            token_delta=candidate.trace.total_tokens - baseline.trace.total_tokens,
            cost_delta_usd=candidate.trace.cost_usd - baseline.trace.cost_usd,
            latency_delta_seconds=(
                candidate.trace.latency_seconds - baseline.trace.latency_seconds
            ),
            action_delta=len(candidate.trace.actions) - len(baseline.trace.actions),
            intervention_delta=(
                candidate.trace.intervention_count
                - baseline.trace.intervention_count
            ),
            policy_violation_delta=(
                len(candidate.trace.policy_violations)
                - len(baseline.trace.policy_violations)
            ),
            definition_resolution_changed=definition_changed,
            runtime_changed=runtime_changed,
            model_or_prompt_changed=model_changed,
            regression_reasons=tuple(reasons),
            passed=not reasons,
            created_at=float(self.clock()),
        )
        return self.store.append_comparison(comparison)

    def _create_evidence(
        self,
        run: EvaluationRun,
        *,
        actor: AuthenticationActor,
    ) -> str | None:
        if self.artifact_evidence is None:
            return None
        evidence = self.artifact_evidence.create_evidence(
            EvidenceCreate(
                project_id=(
                    self.get_scenario(
                        run.scenario_id,
                        run.scenario_version,
                        actor=actor,
                    ).project_id
                ),
                evidence_type=EvidenceType.POLICY_EVALUATION,
                provider="codex-web",
                source="autonomy-evaluation",
                external_id=run.id,
                deep_link=f"/api/evaluations/runs/{run.id}",
                result=EvidenceResult.PASS if run.passed else EvidenceResult.FAIL,
                summary=(
                    f"Evaluation {run.scenario_id}@{run.scenario_version} "
                    f"{'passed' if run.passed else 'failed'} deterministic replay"
                ),
                metadata={
                    "evaluation_run_id": run.id,
                    "scenario_id": run.scenario_id,
                    "scenario_version": run.scenario_version,
                    "fixture_checksum_sha256": run.fixture_checksum_sha256,
                    "replay_fixture_id": run.replay_fixture_id,
                    "backend_id": run.backend_id,
                    "passed": run.passed,
                    "assertion_count": len(run.assertions),
                    "failure_count": sum(
                        1 for item in run.assertions if not item.passed
                    ),
                },
            ),
            actor=actor,
        )
        return evidence.id

    async def run(
        self,
        request: EvaluationRunRequest,
        *,
        actor: AuthenticationActor,
    ) -> tuple[EvaluationRun, EvaluationComparison | None]:
        scenario = self.get_scenario(
            request.scenario_id,
            request.scenario_version,
            actor=actor,
        )
        fixture = self._fixture(scenario, request.replay_fixture_id)
        backend = self._backends.get(request.backend_id)
        if backend is None:
            raise EvaluationError(
                f"evaluation replay backend is not registered: {request.backend_id}"
            )
        definitions = self._effective_definitions(scenario, fixture)
        runtime = self._effective_runtime(scenario, fixture)
        models = self._effective_models(scenario, fixture)
        self._validate_definition_set(
            definitions,
            actor=actor,
            project_id=scenario.project_id,
        )
        started = float(self.clock())
        trace = await backend.replay(scenario, fixture, actor=actor)
        completed = float(self.clock())
        assertions = self._assertions(
            scenario,
            trace,
            definitions,
            runtime,
            models,
        )
        run = EvaluationRun(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.version,
            suite_ids=scenario.suite_ids,
            ci_tier=scenario.ci_tier,
            replay_fixture_id=fixture.id,
            backend_id=request.backend_id,
            mode=fixture.mode,
            failure_injection=fixture.failure_injection,
            fixture_checksum_sha256=scenario.fixture_checksum_sha256,
            starting_state=scenario.starting_state,
            event_ids=tuple(item.event_id for item in scenario.events),
            definitions=definitions,
            runtime=runtime,
            models=models,
            trace=trace,
            assertions=assertions,
            passed=all(item.passed for item in assertions),
            baseline_run_id=request.baseline_run_id,
            executed_by=actor.identity_id,
            started_at=started,
            completed_at=completed,
        )
        self.store.append_run(run)
        evidence_id = self._create_evidence(run, actor=actor)
        if evidence_id is not None:
            run = run.model_copy(update={"evidence_id": evidence_id})
            self.store.update_run(run)

        comparison = None
        if request.baseline_run_id:
            baseline = self.get_run(request.baseline_run_id, actor=actor)
            comparison = self._comparison(scenario, baseline, run)
        return run, comparison

    def get_run(
        self,
        run_id: str,
        *,
        actor: AuthenticationActor,
    ) -> EvaluationRun:
        run = self.store.get_run(run_id)
        if not self._same_scope(run, actor):
            raise EvaluationNotFoundError(run_id)
        return run

    def list_runs(
        self,
        actor: AuthenticationActor,
        *,
        scenario_id: str | None = None,
    ) -> tuple[EvaluationRun, ...]:
        return self.store.list_runs(
            actor.organization_id,
            actor.workspace_id,
            scenario_id=scenario_id,
        )

    def list_comparisons(
        self,
        actor: AuthenticationActor,
        *,
        scenario_id: str | None = None,
    ) -> tuple[EvaluationComparison, ...]:
        return self.store.list_comparisons(
            actor.organization_id,
            actor.workspace_id,
            scenario_id=scenario_id,
        )

    async def run_suite(
        self,
        suite_id: str,
        *,
        actor: AuthenticationActor,
        candidate_fixture_id: str | None = None,
    ) -> EvaluationSuiteRun:
        scenarios = self.list_scenarios(actor, suite_id=suite_id)
        if not scenarios:
            raise EvaluationNotFoundError(f"evaluation suite not found: {suite_id}")
        run_ids: list[str] = []
        comparison_ids: list[str] = []
        evidence_ids: list[str] = []
        passed = True

        for scenario in scenarios:
            historical = next(
                item
                for item in scenario.replay_fixtures
                if item.mode == EvaluationRunMode.HISTORICAL
            )
            baseline, _ = await self.run(
                EvaluationRunRequest(
                    scenario_id=scenario.scenario_id,
                    scenario_version=scenario.version,
                    replay_fixture_id=historical.id,
                ),
                actor=actor,
            )
            run_ids.append(baseline.id)
            if baseline.evidence_id:
                evidence_ids.append(baseline.evidence_id)
            passed = passed and baseline.passed

            if candidate_fixture_id:
                candidate = next(
                    (
                        item
                        for item in scenario.replay_fixtures
                        if item.id == candidate_fixture_id
                    ),
                    None,
                )
                if candidate is None:
                    raise EvaluationNotFoundError(
                        f"candidate fixture {candidate_fixture_id} is missing "
                        f"from {scenario.scenario_id}@{scenario.version}"
                    )
                candidate_run, comparison = await self.run(
                    EvaluationRunRequest(
                        scenario_id=scenario.scenario_id,
                        scenario_version=scenario.version,
                        replay_fixture_id=candidate.id,
                        baseline_run_id=baseline.id,
                    ),
                    actor=actor,
                )
                run_ids.append(candidate_run.id)
                if candidate_run.evidence_id:
                    evidence_ids.append(candidate_run.evidence_id)
                passed = passed and candidate_run.passed
                if comparison is not None:
                    comparison_ids.append(comparison.id)
                    passed = passed and comparison.passed

        suite = EvaluationSuiteRun(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            suite_id=suite_id,
            run_ids=tuple(run_ids),
            comparison_ids=tuple(comparison_ids),
            passed=passed,
            evidence_ids=tuple(evidence_ids),
            executed_by=actor.identity_id,
            created_at=float(self.clock()),
        )
        return self.store.append_suite_run(suite)

    def list_suite_runs(
        self,
        actor: AuthenticationActor,
        *,
        suite_id: str | None = None,
    ) -> tuple[EvaluationSuiteRun, ...]:
        return self.store.list_suite_runs(
            actor.organization_id,
            actor.workspace_id,
            suite_id=suite_id,
        )

