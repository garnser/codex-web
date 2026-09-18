from __future__ import annotations

import unittest

from pydantic import ValidationError

from codex_web.execution_contract_schema import (
    EXECUTION_CONTRACT_SCHEMA_VERSION,
    ExecutionContractV1,
    execution_contract_for_work_item,
)
from codex_web.execution_contracts import ROLE_CONTRACTS
from codex_web.models import WorkItemHandoff, WorkItemState
from codex_web.services.work_item_contracts import WorkItemContractService
from codex_web.work_item_execution_models import (
    WorkItemExecutionCheckpoint,
    WorkItemExecutionLifecycle,
    WorkItemUsageAttribution,
)


class ExecutionContractSchemaTests(unittest.TestCase):
    def _state(self, **overrides) -> WorkItemState:
        payload = {
            "ref": "group/app#42",
            "project_id": "app",
            "project_path": "group/app",
            "current_owner": "james",
            "current_stage": "implementation_active",
            "artifact_state": "branch",
            "next_action": "Implement the requested change.",
            "last_meaningful_update_at": 1.0,
            "updated_at": 1.0,
            "created_at": 1.0,
        }
        payload.update(overrides)
        return WorkItemState(**payload)

    def test_factory_builds_versioned_contract_from_canonical_state(self) -> None:
        contract = execution_contract_for_work_item(
            self._state(),
            ROLE_CONTRACTS["james"],
        )

        self.assertEqual(contract.schema_version, EXECUTION_CONTRACT_SCHEMA_VERSION)
        self.assertEqual(contract.schema_version, "1.3")
        self.assertEqual(contract.work_item_ref, "group/app#42")
        self.assertEqual(contract.role_id, "james")
        self.assertEqual(contract.agent_id, "james")
        self.assertEqual(contract.target.repository, "group/app")
        self.assertEqual(contract.inputs.current_stage, "implementation_active")
        self.assertEqual(contract.inputs.artifact_state, "branch")
        self.assertEqual(contract.inputs.retry_attempt, 0)
        self.assertEqual(contract.inputs.retry_max_attempts, 3)
        self.assertEqual(contract.accounting.work_item_ref, "group/app#42")
        self.assertTrue(contract.accounting.usage_recording_required)
        self.assertEqual(contract.permissions.sandbox, "inherit")
        self.assertEqual(contract.permissions.approval_policy, "inherit")
        self.assertFalse(contract.permissions.can_weaken_controls)
        self.assertEqual(contract.expected_outputs, ROLE_CONTRACTS["james"].required_artifacts)
        self.assertEqual(contract.failure_conditions, ROLE_CONTRACTS["james"].failure_conditions)

    def test_checkpoint_and_attribution_are_carried_without_replaying_history(self) -> None:
        checkpoint = WorkItemExecutionCheckpoint(
            id="checkpoint-4",
            sequence=4,
            created_at=4.0,
            summary="Tests are green; prepare validation handoff.",
            next_actions=["Open validation handoff."],
        )
        lifecycle = WorkItemExecutionLifecycle(
            deadline_at=20.0,
            latest_checkpoint=checkpoint,
            checkpoint_history=[checkpoint],
            usage=WorkItemUsageAttribution(goal_id="goal-7", decision_id="decision-2"),
        )
        lifecycle.retry.attempt = 2
        lifecycle.retry.policy.max_attempts = 5

        contract = execution_contract_for_work_item(
            self._state(execution=lifecycle),
            ROLE_CONTRACTS["james"],
        )

        self.assertEqual(contract.inputs.retry_attempt, 2)
        self.assertEqual(contract.inputs.retry_max_attempts, 5)
        self.assertEqual(contract.inputs.deadline_at, 20.0)
        self.assertEqual(contract.inputs.checkpoint_id, "checkpoint-4")
        self.assertEqual(
            contract.inputs.checkpoint_summary,
            "Tests are green; prepare validation handoff.",
        )
        self.assertEqual(contract.accounting.checkpoint_id, "checkpoint-4")
        self.assertEqual(contract.accounting.goal_id, "goal-7")
        self.assertEqual(contract.accounting.decision_id, "decision-2")

    def test_canonical_resource_ids_are_carried_into_target(self) -> None:
        contract = execution_contract_for_work_item(
            self._state(resource_ids=["resource-repo", "resource-prod", "resource-repo"]),
            ROLE_CONTRACTS["james"],
        )

        self.assertEqual(
            contract.target.resource_ids,
            ("resource-repo", "resource-prod"),
        )
        self.assertEqual(
            contract.compact_public()["target"]["resource_ids"],
            ["resource-repo", "resource-prod"],
        )

    def test_pending_handoff_makes_recipient_the_contract_agent(self) -> None:
        handoff = WorkItemHandoff(
            from_agent="james",
            to_agent="quinn",
            requested_at=1.0,
            status="pending",
            artifact_state="merge_request",
            stage="ready_for_validation",
        )
        state = self._state(
            current_stage="ready_for_validation",
            artifact_state="merge_request",
            handoff=handoff,
            next_owner="quinn",
        )

        contract = execution_contract_for_work_item(state, ROLE_CONTRACTS["quinn"])

        self.assertEqual(contract.agent_id, "quinn")
        self.assertEqual(contract.inputs.handoff_from, "james")
        self.assertEqual(contract.inputs.handoff_to, "quinn")
        self.assertEqual(contract.inputs.handoff_status, "pending")

    def test_contract_schema_is_strict_and_versioned(self) -> None:
        payload = execution_contract_for_work_item(
            self._state(),
            ROLE_CONTRACTS["james"],
        ).model_dump(mode="json")

        payload["schema_version"] = "2.0"
        with self.assertRaises(ValidationError):
            ExecutionContractV1.model_validate(payload)

        payload = execution_contract_for_work_item(
            self._state(),
            ROLE_CONTRACTS["james"],
        ).model_dump(mode="json")
        payload["unexpected"] = True
        with self.assertRaises(ValidationError):
            ExecutionContractV1.model_validate(payload)

    def test_compact_public_omits_unknown_target_values(self) -> None:
        contract = execution_contract_for_work_item(
            self._state(project_path=None),
            ROLE_CONTRACTS["james"],
        )

        public = contract.compact_public()

        self.assertEqual(public["schema_version"], "1.3")
        self.assertEqual(public["target"], {})
        self.assertNotIn("branch", public["target"])
        self.assertNotIn("environment", public["target"])

    def test_service_validates_contract_before_preserving_prompt_path(self) -> None:
        class Host:
            @staticmethod
            def _work_item_split_brain_findings(state):
                return []

        service = WorkItemContractService(Host(), lambda state: f"WORK ITEM {state.ref}")
        state = self._state()

        contract = service.contract_for_state(state)
        text = service.dispatch_text(state)

        self.assertEqual(contract.role_id, "james")
        self.assertIn("CANONICAL EXECUTION CONTRACT (schema 1.3)", text)
        self.assertIn("WORK ITEM group/app#42", text)
        self.assertNotIn('"expected_outputs"', text)

    def test_split_brain_findings_are_carried_as_structured_inputs(self) -> None:
        class Host:
            @staticmethod
            def _work_item_split_brain_findings(state):
                return ["owner drift"]

        service = WorkItemContractService(Host(), lambda state: state.ref)
        contract = service.contract_for_state(self._state())

        self.assertEqual(contract.inputs.split_brain_findings, ("owner drift",))
        self.assertTrue(contract.role_id)


if __name__ == "__main__":
    unittest.main()
