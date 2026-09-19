from __future__ import annotations

from collections.abc import Callable
from typing import Any

from codex_web.compatibility import MigrationRegistry
from codex_web.evaluations import (
    EVALUATION_STATE_CONTRACT,
    EvaluationComparison,
    EvaluationRun,
    EvaluationScenario,
    EvaluationState,
    EvaluationSuiteRun,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


EVALUATION_STATE_MIGRATIONS = MigrationRegistry("evaluation-state")
EVALUATION_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": EVALUATION_STATE_CONTRACT.current,
        "scenarios": list(payload.get("scenarios") or []),
        "runs": list(payload.get("runs") or []),
        "comparisons": list(payload.get("comparisons") or []),
        "suite_runs": list(payload.get("suite_runs") or []),
    },
)


class EvaluationNotFoundError(KeyError):
    pass


class EvaluationConflictError(RuntimeError):
    pass


class EvaluationStore:
    namespace = "evaluations"
    max_runs = 10000
    max_comparisons = 10000
    max_suite_runs = 2000

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> EvaluationState:
        if payload is None:
            return EvaluationState()
        if not isinstance(payload, dict):
            raise ValueError("evaluation state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != EVALUATION_STATE_CONTRACT.current:
            payload = EVALUATION_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=EVALUATION_STATE_CONTRACT.current,
            )
        EVALUATION_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return EvaluationState.model_validate(payload)

    def load(self) -> EvaluationState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[EvaluationState], EvaluationState],
    ) -> EvaluationState:
        def apply(raw: Any) -> dict[str, Any]:
            state = updater(self._decode(raw))
            state.runs = state.runs[-self.max_runs :]
            state.comparisons = state.comparisons[-self.max_comparisons :]
            state.suite_runs = state.suite_runs[-self.max_suite_runs :]
            return state.model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=EvaluationState().model_dump(mode="json"),
        )
        return self._decode(payload)

    def create_scenario(self, scenario: EvaluationScenario) -> EvaluationScenario:
        def apply(state: EvaluationState) -> EvaluationState:
            if any(
                item.organization_id == scenario.organization_id
                and item.workspace_id == scenario.workspace_id
                and item.scenario_id == scenario.scenario_id
                and item.version == scenario.version
                for item in state.scenarios
            ):
                raise EvaluationConflictError(
                    f"evaluation scenario already exists: {scenario.scenario_id}@{scenario.version}"
                )
            state.scenarios.append(scenario)
            return state

        self.update(apply)
        return scenario

    def list_scenarios(
        self,
        organization_id: str,
        workspace_id: str,
    ) -> tuple[EvaluationScenario, ...]:
        rows = [
            item
            for item in self.load().scenarios
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
        ]
        rows.sort(key=lambda item: (item.scenario_id, item.version))
        return tuple(rows)

    def get_scenario(
        self,
        organization_id: str,
        workspace_id: str,
        scenario_id: str,
        version: str,
    ) -> EvaluationScenario:
        for item in self.load().scenarios:
            if (
                item.organization_id == organization_id
                and item.workspace_id == workspace_id
                and item.scenario_id == scenario_id
                and item.version == version
            ):
                return item
        raise EvaluationNotFoundError(f"{scenario_id}@{version}")

    def append_run(self, run: EvaluationRun) -> EvaluationRun:
        def apply(state: EvaluationState) -> EvaluationState:
            if any(item.id == run.id for item in state.runs):
                raise EvaluationConflictError(f"evaluation run already exists: {run.id}")
            state.runs.append(run)
            return state

        self.update(apply)
        return run

    def get_run(self, run_id: str) -> EvaluationRun:
        for item in self.load().runs:
            if item.id == run_id:
                return item
        raise EvaluationNotFoundError(run_id)

    def list_runs(
        self,
        organization_id: str,
        workspace_id: str,
        *,
        scenario_id: str | None = None,
    ) -> tuple[EvaluationRun, ...]:
        rows = [
            item
            for item in self.load().runs
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
            and (scenario_id is None or item.scenario_id == scenario_id)
        ]
        rows.sort(key=lambda item: (item.completed_at, item.id), reverse=True)
        return tuple(rows)

    def update_run(self, run: EvaluationRun) -> EvaluationRun:
        def apply(state: EvaluationState) -> EvaluationState:
            found = False
            rows = []
            for item in state.runs:
                if item.id == run.id:
                    if (
                        item.organization_id != run.organization_id
                        or item.workspace_id != run.workspace_id
                    ):
                        raise EvaluationConflictError(
                            "evaluation run tenant scope cannot change"
                        )
                    rows.append(run)
                    found = True
                else:
                    rows.append(item)
            if not found:
                raise EvaluationNotFoundError(run.id)
            state.runs = rows
            return state

        self.update(apply)
        return run

    def append_comparison(
        self,
        comparison: EvaluationComparison,
    ) -> EvaluationComparison:
        self.update(
            lambda state: state.model_copy(
                update={"comparisons": [*state.comparisons, comparison]}
            )
        )
        return comparison

    def list_comparisons(
        self,
        organization_id: str,
        workspace_id: str,
        *,
        scenario_id: str | None = None,
    ) -> tuple[EvaluationComparison, ...]:
        rows = [
            item
            for item in self.load().comparisons
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
            and (scenario_id is None or item.scenario_id == scenario_id)
        ]
        rows.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return tuple(rows)

    def append_suite_run(self, suite: EvaluationSuiteRun) -> EvaluationSuiteRun:
        self.update(
            lambda state: state.model_copy(
                update={"suite_runs": [*state.suite_runs, suite]}
            )
        )
        return suite

    def list_suite_runs(
        self,
        organization_id: str,
        workspace_id: str,
        *,
        suite_id: str | None = None,
    ) -> tuple[EvaluationSuiteRun, ...]:
        rows = [
            item
            for item in self.load().suite_runs
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
            and (suite_id is None or item.suite_id == suite_id)
        ]
        rows.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return tuple(rows)
