from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from codex_web.models import WorkItemState
from codex_web.services.work_item_execution import WorkItemExecutionLifecycleService
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.work_item_execution_models import (
    WorkItemCheckpointCreate,
    WorkItemExecutionUpdate,
    WorkItemUsageRecord,
)


class _Host:
    DEFAULT_VALIDATION_OWNER = "quinn"
    DEFAULT_RELEASE_OWNER = "release manager"
    NON_IMPLEMENTATION_OWNERS = {"quinn", "release manager", "orchestrator"}

    def __init__(self, root: Path) -> None:
        self.DATA_DIR = root
        self.WORK_ITEM_EVENTS_FILE = root / "work_item_events.jsonl"
        self.states: dict[str, WorkItemState] = {}

    def _load_work_item_states(self):
        return {ref: state.model_copy(deep=True) for ref, state in self.states.items()}

    def _save_work_item_states(self, states):
        self.states = {ref: state.model_copy(deep=True) for ref, state in states.items()}

    @staticmethod
    def _leading_owner_cue_in_action(value):
        return None


class WorkItemExecutionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.host = _Host(self.root)
        self.machine = WorkItemStateMachine(self.host)
        self.service = WorkItemExecutionLifecycleService(self.host, self.machine)
        self.state = WorkItemState(
            ref="group/app#42",
            project_id="app",
            project_path="group/app",
            current_owner="james",
            current_stage="implementation_active",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        self.host.states[self.state.ref] = self.state.model_copy(deep=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_existing_persisted_shape_gets_safe_execution_defaults(self) -> None:
        payload = self.state.model_dump(mode="json")
        payload.pop("execution")

        migrated = WorkItemState.model_validate(payload)

        self.assertEqual(migrated.execution.retry.attempt, 0)
        self.assertEqual(migrated.execution.retry.policy.max_attempts, 3)
        self.assertIsNone(migrated.execution.latest_checkpoint)
        self.assertEqual(migrated.execution.usage.calls, 0)

    def test_retry_deadline_and_failure_state_are_structured(self) -> None:
        result = self.service.update(
            self.state.ref,
            WorkItemExecutionUpdate(
                actor="james",
                source="worker",
                reason="transient provider failure",
                retry_attempt=1,
                retry_max_attempts=4,
                retry_backoff_seconds=10,
                timeout_seconds=120,
                deadline_at=999.0,
                failure_category="provider",
                failure_code="rate_limited",
                failure_message="Provider requested retry.",
                failure_retryable=True,
            ),
        )

        execution = result["execution"]
        self.assertEqual(execution["retry"]["attempt"], 1)
        self.assertEqual(execution["retry"]["policy"]["max_attempts"], 4)
        self.assertEqual(execution["timeout_seconds"], 120.0)
        self.assertEqual(execution["deadline_at"], 999.0)
        self.assertEqual(execution["failure_reason"]["category"], "provider")
        self.assertEqual(execution["failure_reason"]["code"], "rate_limited")
        self.assertTrue(execution["failure_reason"]["retryable"])
        canonical = execution["failure_reason"]["canonical"]
        self.assertEqual(
            canonical["reason_code"],
            "provider_capacity_or_rate_limit",
        )
        self.assertEqual(
            canonical["category"],
            "provider_model",
        )
        self.assertEqual(
            canonical["retryability"],
            "transient",
        )
        self.assertEqual(
            canonical["remediation_key"],
            "provider.capacity",
        )

    def test_writable_repository_scope_is_canonical_execution_metadata(self) -> None:
        result = self.service.update(
            self.state.ref,
            WorkItemExecutionUpdate(
                actor="james",
                source="operator",
                writable_repository_resource_ids=(
                    "repo-app",
                    "repo-api",
                    "repo-app",
                ),
            ),
        )

        self.assertEqual(
            result["execution"]["writable_repository_resource_ids"],
            ["repo-app", "repo-api"],
        )
        persisted = self.host.states[self.state.ref]
        self.assertEqual(
            persisted.execution.writable_repository_resource_ids,
            ("repo-app", "repo-api"),
        )
        history = self.service.history(self.state.ref)
        self.assertEqual(
            history["items"][-1]["payload"]["writable_repository_resource_ids"],
            ["repo-app", "repo-api"],
        )

    def test_retry_attempt_cannot_exceed_policy(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            self.service.update(
                self.state.ref,
                WorkItemExecutionUpdate(retry_attempt=4, retry_max_attempts=3),
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "retry_attempt_exceeds_policy")

    def test_checkpoint_is_compact_resumable_state_and_history_is_attributable(self) -> None:
        result = self.service.checkpoint(
            self.state.ref,
            WorkItemCheckpointCreate(
                actor="james",
                source="worker",
                reason="context boundary",
                summary="Implementation complete; validation remains.",
                objective="Finish issue 99.",
                current_state="Focused tests prepared.",
                important_decisions=["Keep execution metadata on canonical WorkItemState."],
                blockers=[],
                changed_files=["codex_web/models.py"],
                next_actions=["Run validation."],
            ),
        )

        checkpoint = result["checkpoint"]
        self.assertEqual(checkpoint["id"], "checkpoint-1")
        self.assertEqual(checkpoint["sequence"], 1)
        self.assertEqual(checkpoint["next_actions"], ["Run validation."])

        history = self.service.history(self.state.ref)
        self.assertEqual(history["count"], 1)
        event = history["items"][0]
        self.assertEqual(event["event_type"], "execution_checkpoint_recorded")
        self.assertEqual(event["actor"], "james")
        self.assertEqual(event["source"], "worker")
        self.assertEqual(event["reason"], "context boundary")
        self.assertEqual(event["payload"]["checkpoint_id"], "checkpoint-1")

    def test_usage_records_accumulate_and_keep_future_goal_decision_hooks(self) -> None:
        self.service.record_usage(
            self.state.ref,
            WorkItemUsageRecord(
                actor="james",
                source="model-gateway",
                provider="openai",
                model="example-model",
                role="implementer",
                calls=1,
                input_tokens=100,
                output_tokens=40,
                reasoning_tokens=10,
                estimated_cost_usd=0.12,
                goal_id="goal-1",
            ),
        )
        result = self.service.record_usage(
            self.state.ref,
            WorkItemUsageRecord(
                actor="james",
                source="model-gateway",
                calls=2,
                input_tokens=50,
                output_tokens=25,
                reasoning_tokens=5,
                estimated_cost_usd=0.03,
                decision_id="decision-9",
            ),
        )

        usage = result["usage"]
        self.assertEqual(usage["calls"], 3)
        self.assertEqual(usage["input_tokens"], 150)
        self.assertEqual(usage["output_tokens"], 65)
        self.assertEqual(usage["reasoning_tokens"], 15)
        self.assertAlmostEqual(usage["estimated_cost_usd"], 0.15)
        self.assertEqual(usage["goal_id"], "goal-1")
        self.assertEqual(usage["decision_id"], "decision-9")

    def test_history_skips_malformed_lines_and_enforces_limit(self) -> None:
        self.service.checkpoint(
            self.state.ref,
            WorkItemCheckpointCreate(summary="first"),
        )
        with self.host.WORK_ITEM_EVENTS_FILE.open("a", encoding="utf-8") as handle:
            handle.write("not-json\n")
        self.service.checkpoint(
            self.state.ref,
            WorkItemCheckpointCreate(summary="second"),
        )

        history = self.service.history(self.state.ref, limit=1)

        self.assertEqual(history["count"], 2)
        self.assertEqual(history["returned"], 1)
        self.assertEqual(history["items"][0]["payload"]["summary"], "second")


if __name__ == "__main__":
    unittest.main()
