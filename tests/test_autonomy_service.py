from __future__ import annotations

import time
import unittest

from fastapi import FastAPI

from codex_web.models import WorkItemHandoff, WorkItemState
from codex_web.services.autonomy import AutonomyService, install_autonomy_service


class _Host:
    def __init__(self) -> None:
        self.saved_states: dict[str, WorkItemState] | None = None
        self.events: list[dict] = []
        self.OWNER_QUEUE_AGENTS = ()
        self.HANDOFF_COORDINATION_CHANNEL = None

    def _load_work_item_states(self) -> dict[str, WorkItemState]:
        return self.states

    def _save_work_item_states(self, states: dict[str, WorkItemState]) -> None:
        self.saved_states = states

    @staticmethod
    def _coerce_owner(value: str | None) -> str | None:
        value = (value or "").strip().lower()
        return value or None

    @staticmethod
    def _work_item_handoff_timeout_seconds() -> float:
        return 10.0

    @staticmethod
    def _archive_active_handoff(
        state: WorkItemState,
        *,
        now: float,
        status: str,
        reason_code: str | None = None,
    ) -> WorkItemState:
        assert state.handoff is not None
        archived = state.handoff.model_copy(
            update={
                "status": status,
                "acknowledged_at": now,
                "reason_code": reason_code,
            }
        )
        # Validate the archived value through the typed model before persisting.
        archived = WorkItemHandoff.model_validate(archived.model_dump())
        state.handoff_history = [*state.handoff_history, archived]
        state.handoff = None
        return state

    @staticmethod
    def _work_item_event(ref: str, event_type: str, **kwargs) -> dict:
        return {"ref": ref, "event_type": event_type, **kwargs}

    def _append_work_item_event(self, event: dict) -> None:
        self.events.append(event)


class AutonomyInstallationTests(unittest.TestCase):
    def test_install_rebinds_all_cycle_entrypoints_and_is_idempotent(self) -> None:
        app = FastAPI()
        host = _Host()

        first = install_autonomy_service(app, host)
        second = install_autonomy_service(app, host)

        self.assertIs(first, second)
        self.assertIs(app.state.autonomy_service, first)
        self.assertIs(host._run_owner_work_watchdog_cycle.__self__, first)
        self.assertIs(host._run_release_gate_watchdog_cycle.__self__, first)
        self.assertIs(host._run_work_item_sla_cycle.__self__, first)
        self.assertIs(host._run_orchestrator_watchdog_cycle.__self__, first)
        self.assertIs(host._run_split_brain_watchdog_cycle.__self__, first)
        self.assertIs(host._prepare_external_action.__self__, first)
        self.assertIs(host._execute_external_action.__self__, first)
        self.assertIs(host._verify_external_action.__self__, first)
        self.assertIs(host._rollback_external_action.__self__, first)


class AutonomyStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_pending_handoff_is_archived_with_typed_status(self) -> None:
        host = _Host()
        now = time.time()
        state = WorkItemState(
            ref="example/project#17",
            project_id="project-a",
            current_owner="alice",
            current_stage="implementation_active",
            handoff=WorkItemHandoff(
                from_agent="alice",
                to_agent="bob",
                requested_at=now - 60,
                status="pending",
            ),
            last_meaningful_update_at=now - 60,
            created_at=now - 120,
            updated_at=now - 60,
        )
        host.states = {state.ref: state}
        service = AutonomyService(host)

        await service.run_work_item_sla_cycle()

        saved = host.saved_states
        self.assertIsNotNone(saved)
        current = saved[state.ref]
        self.assertIsNone(current.handoff)
        self.assertEqual(current.current_owner, "alice")
        self.assertEqual(current.next_owner, "alice")
        self.assertEqual(current.current_stage, "implementation_active")
        self.assertEqual(current.blocker, "Structured handoff expired without acknowledgement.")
        self.assertEqual(current.handoff_history[-1].status, "superseded")
        self.assertEqual(current.handoff_history[-1].reason_code, "handoff_expired")
        # Re-validate the whole state to prove the watchdog cannot write a value
        # rejected by the typed state-machine model on the next read.
        WorkItemState.model_validate(current.model_dump())
        self.assertEqual(host.events[-1]["event_type"], "handoff_expired")


if __name__ == "__main__":
    unittest.main()
