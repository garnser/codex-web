from __future__ import annotations

import unittest

from codex_web.failures import (
    FailureCategory,
    FailureOutcome,
    FailureReason,
    FailureRetryability,
    action_failure_reason,
    aggregate_failure,
    create_failure,
    failure_definition,
    failure_taxonomy_snapshot,
    sanitize_failure_details,
    worker_failure_reason,
)


class FailureTaxonomyTests(unittest.TestCase):
    def test_every_reason_has_one_stable_definition(self) -> None:
        snapshot = failure_taxonomy_snapshot()
        reasons = {
            item["reason_code"]
            for item in snapshot["reasons"]
        }

        self.assertEqual(
            reasons,
            {item.value for item in FailureReason},
        )
        self.assertEqual(snapshot["version"], "1.0")

    def test_transient_and_reconciliation_policy_are_explicit(self) -> None:
        transient = create_failure(
            FailureReason.PROVIDER_CAPACITY_OR_RATE_LIMIT,
            source_subsystem="model_gateway",
        )
        unknown_action = create_failure(
            FailureReason.ACTION_TIMEOUT_UNKNOWN_OUTCOME,
            source_subsystem="action_intent",
        )
        auth = create_failure(
            FailureReason.PROVIDER_AUTH_OR_ACCESS,
            source_subsystem="model_gateway",
        )

        self.assertTrue(transient.automatic_retry_allowed)
        self.assertEqual(
            transient.retryability,
            FailureRetryability.TRANSIENT,
        )
        self.assertFalse(unknown_action.automatic_retry_allowed)
        self.assertTrue(unknown_action.requires_reconciliation)
        self.assertEqual(
            unknown_action.outcome,
            FailureOutcome.UNKNOWN,
        )
        self.assertFalse(auth.automatic_retry_allowed)
        self.assertEqual(
            auth.retryability,
            FailureRetryability.AFTER_REMEDIATION,
        )

    def test_secret_shaped_details_are_removed_and_values_are_bounded(self) -> None:
        failure = create_failure(
            FailureReason.UNCLASSIFIED,
            source_subsystem="test",
            details={
                "provider": "example",
                "token": "SHOULD-NOT-APPEAR",
                "authorization": "Bearer SHOULD-NOT-APPEAR",
                "credential_ref": "also-hidden",
                "safe": "x" * 500,
            },
        )

        self.assertEqual(failure.details["provider"], "example")
        self.assertNotIn("token", failure.details)
        self.assertNotIn("authorization", failure.details)
        self.assertNotIn("credential_ref", failure.details)
        self.assertEqual(len(failure.details["safe"]), 256)
        self.assertNotIn(
            "SHOULD-NOT-APPEAR",
            failure.model_dump_json(),
        )

    def test_metric_labels_are_low_cardinality_contract_fields(self) -> None:
        failure = create_failure(
            FailureReason.WORKER_LEASE_LOST,
            source_subsystem="execution_worker",
            worker_id="worker-unique",
            execution_id="execution-unique",
        )

        self.assertEqual(
            failure.metric_labels(),
            {
                "category": "runtime_worker",
                "reason": "worker_lease_lost",
                "source": "execution_worker",
                "retryability": "transient",
            },
        )
        self.assertNotIn(
            "worker-unique",
            failure.metric_labels().values(),
        )

    def test_native_action_and_worker_codes_map_deterministically(self) -> None:
        self.assertEqual(
            action_failure_reason("429"),
            FailureReason.ACTION_RATE_LIMIT,
        )
        self.assertEqual(
            action_failure_reason("409"),
            FailureReason.ACTION_CONFLICT,
        )
        self.assertEqual(
            action_failure_reason("provider_weird"),
            FailureReason.UNCLASSIFIED,
        )
        self.assertEqual(
            worker_failure_reason("worker_lease_expired"),
            FailureReason.WORKER_LEASE_LOST,
        )
        self.assertEqual(
            worker_failure_reason("oom"),
            FailureReason.RESOURCE_LIMIT,
        )
        self.assertEqual(
            worker_failure_reason("mystery"),
            FailureReason.UNCLASSIFIED,
        )

    def test_repeat_failure_aggregation_preserves_first_occurrence(self) -> None:
        first = create_failure(
            FailureReason.PROVIDER_NETWORK,
            source_subsystem="model_gateway",
            execution_id="execution-1",
            occurred_at=10.0,
        )
        second = create_failure(
            FailureReason.PROVIDER_NETWORK,
            source_subsystem="model_gateway",
            execution_id="execution-1",
            occurred_at=20.0,
        )

        combined = aggregate_failure(first, second)

        self.assertEqual(combined.attempt, 2)
        self.assertEqual(combined.first_occurred_at, 10.0)
        self.assertEqual(combined.last_occurred_at, 20.0)

    def test_definition_contract_prevents_retry_semantic_drift(self) -> None:
        definition = failure_definition(
            FailureReason.CONTEXT_OVERFLOW
        )
        self.assertEqual(
            definition.category,
            FailureCategory.PROVIDER_MODEL,
        )
        self.assertEqual(
            definition.retryability,
            FailureRetryability.AFTER_REMEDIATION,
        )
        self.assertEqual(
            definition.remediation_key,
            "context.reduce",
        )


if __name__ == "__main__":
    unittest.main()
