from __future__ import annotations

from typing import Any, Callable

from codex_web.automation_runs import (
    AUTOMATION_RUN_STATE_CONTRACT,
    AutomationRun,
    AutomationRunState,
)
from codex_web.compatibility import MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore


AUTOMATION_RUN_MIGRATIONS = MigrationRegistry("automation-run-state")
AUTOMATION_RUN_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        "runs": list(payload.get("runs", [])),
    },
)


class AutomationRunNotFoundError(KeyError):
    pass


class AutomationRunStore:
    namespace = "automation_runs"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> AutomationRunState:
        if payload is None:
            return AutomationRunState()
        if not isinstance(payload, dict):
            raise ValueError("automation run state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != AUTOMATION_RUN_STATE_CONTRACT.current:
            payload = AUTOMATION_RUN_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=AUTOMATION_RUN_STATE_CONTRACT.current,
            )
        return AutomationRunState.model_validate(payload)

    def load(self) -> AutomationRunState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[AutomationRunState], AutomationRunState],
    ) -> AutomationRunState:
        payload = self.store.update(
            self.namespace,
            lambda raw: updater(self._decode(raw)).model_dump(mode="json"),
            default=AutomationRunState().model_dump(mode="json"),
        )
        return self._decode(payload)

    def list(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        automation_id: str | None = None,
    ) -> list[AutomationRun]:
        values = [
            run
            for run in self.load().runs
            if run.organization_id == organization_id
            and run.workspace_id == workspace_id
            and (automation_id is None or run.automation_id == automation_id)
        ]
        return sorted(values, key=lambda run: (run.created_at, run.id), reverse=True)

    def get(
        self,
        run_id: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AutomationRun:
        run = next(
            (
                item
                for item in self.load().runs
                if item.id == run_id
                and item.organization_id == organization_id
                and item.workspace_id == workspace_id
            ),
            None,
        )
        if run is None:
            raise AutomationRunNotFoundError(run_id)
        return run

    def find_by_dedupe(
        self,
        dedupe_key: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AutomationRun | None:
        return next(
            (
                run
                for run in self.load().runs
                if run.organization_id == organization_id
                and run.workspace_id == workspace_id
                and run.dedupe_key == dedupe_key
            ),
            None,
        )

    def append(self, run: AutomationRun) -> AutomationRun:
        result: dict[str, AutomationRun] = {}

        def apply(state: AutomationRunState) -> AutomationRunState:
            existing = next(
                (
                    item
                    for item in state.runs
                    if item.organization_id == run.organization_id
                    and item.workspace_id == run.workspace_id
                    and item.dedupe_key == run.dedupe_key
                ),
                None,
            )
            if existing is not None:
                result["run"] = existing
                return state
            state.runs.append(run)
            result["run"] = run
            return state

        self.update(apply)
        return result["run"]

    def replace(self, run: AutomationRun) -> AutomationRun:
        found = False

        def apply(state: AutomationRunState) -> AutomationRunState:
            nonlocal found
            for index, item in enumerate(state.runs):
                if (
                    item.id == run.id
                    and item.organization_id == run.organization_id
                    and item.workspace_id == run.workspace_id
                ):
                    state.runs[index] = run
                    found = True
                    break
            if not found:
                raise AutomationRunNotFoundError(run.id)
            return state

        self.update(apply)
        return run
