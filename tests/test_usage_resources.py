from __future__ import annotations

import unittest

from pydantic import ValidationError

from codex_web.agent_runtime_usage import (
    AgentRuntimeUsage,
    UsageMeasurementMode,
    UsageResource,
    UsageResourceKind,
    UsageResourceSource,
)
from codex_web.services.agent_runtime_telemetry import AgentRuntimeTelemetryService
from codex_web.services.runtime import RuntimeService
from codex_web.storage.agent_runtime_usage import AGENT_RUNTIME_USAGE_MIGRATIONS
from codex_web.storage.model_gateway import MODEL_GATEWAY_MIGRATIONS


def _resource(**overrides) -> UsageResource:
    payload = {
        "resource_id": "tokens",
        "label": "Token usage",
        "kind": UsageResourceKind.TOKEN_USAGE,
        "consumed": 100,
        "unit": "tokens",
        "source": UsageResourceSource.PROVIDER_REPORTED,
        "authoritative": True,
        "measurement_mode": UsageMeasurementMode.INCREMENT,
        "provider_id": "provider-a",
        "runtime_id": "runtime-a",
        "model_id": "model-a",
        "observed_at": 100.0,
    }
    payload.update(overrides)
    return UsageResource(**payload)


def _record(resource: UsageResource, *, suffix: str) -> AgentRuntimeUsage:
    return AgentRuntimeUsage(
        id=f"usage-{suffix}",
        organization_id="org-a",
        workspace_id="workspace-a",
        project_id="project-a",
        provider_id=resource.provider_id,
        runtime_id=resource.runtime_id,
        runtime_type="test",
        observed_model_ids=((resource.model_id,) if resource.model_id else ()),
        observed_at=resource.observed_at,
        resources=(resource,),
    )


class UsageResourceTests(unittest.TestCase):
    def test_usage_state_and_legacy_cost_migrate_without_inventing_provenance(self) -> None:
        usage = AGENT_RUNTIME_USAGE_MIGRATIONS.migrate(
            {"schema_version": "1.0", "records": []},
            from_version="1.0",
            to_version="1.1",
        )
        self.assertEqual(usage, {"schema_version": "1.1", "records": []})

        gateway = MODEL_GATEWAY_MIGRATIONS.migrate(
            {
                "schema_version": "1.4",
                "invocations": [{"attempts": [{"actual_cost_usd": 1.25}]}],
            },
            from_version="1.4",
            to_version="1.5",
        )
        attempt = gateway["invocations"][0]["attempts"][0]
        self.assertEqual((attempt["actual_cost"], attempt["cost_currency"]), (1.25, "USD"))
        self.assertIsNone(attempt["cost_source"])
        self.assertIsNone(attempt["pricing_revision"])

    def test_percentage_requires_authoritative_denominator(self) -> None:
        self.assertIsNone(_resource(limit=None).utilization_percent)
        self.assertIsNone(_resource(limit=200, authoritative=False).utilization_percent)
        self.assertEqual(_resource(limit=200).utilization_percent, 50.0)

    def test_money_requires_currency_and_calculated_cost_provenance(self) -> None:
        with self.assertRaises(ValidationError):
            _resource(kind="money", unit="currency", currency=None)
        with self.assertRaises(ValidationError):
            _resource(
                kind="money",
                unit="currency",
                currency="usd",
                source="codex_calculated",
            )
        calculated = _resource(
            kind="money",
            unit="currency",
            currency="eur",
            source="codex_calculated",
            model_id="model-a@2026-10",
            model_version="2026-10",
            pricing_revision="pricing-sha256",
        )
        self.assertEqual(calculated.currency, "EUR")
        self.assertFalse(calculated.authoritative)

    def test_increment_rollup_preserves_provider_model_and_currency_boundaries(self) -> None:
        records = [
            _record(_resource(consumed=10, observed_at=1), suffix="a1"),
            _record(_resource(consumed=20, observed_at=2), suffix="a2"),
            _record(
                _resource(
                    resource_id="cost",
                    label="Spend",
                    kind="money",
                    consumed=3,
                    unit="currency",
                    currency="USD",
                    provider_id="provider-b",
                    runtime_id="runtime-b",
                    model_id="model-b",
                    observed_at=3,
                ),
                suffix="b1",
            ),
        ]

        aggregates = AgentRuntimeTelemetryService.aggregate_resources(records)

        self.assertEqual(len(aggregates), 2)
        tokens = next(item for item in aggregates if item.kind == UsageResourceKind.TOKEN_USAGE)
        money = next(item for item in aggregates if item.kind == UsageResourceKind.MONEY)
        self.assertEqual(tokens.consumed, 30)
        self.assertEqual((tokens.provider_id, tokens.model_id), ("provider-a", "model-a"))
        self.assertEqual((money.consumed, money.currency), (3, "USD"))

    def test_freshest_quota_snapshot_replaces_older_window_without_summing(self) -> None:
        old = _resource(
            resource_id="five-hour",
            kind="token_quota",
            label="5 hour allowance",
            consumed=40,
            limit=100,
            period="5 hours",
            reset_at=500,
            measurement_mode="snapshot",
            observed_at=1,
        )
        current = old.model_copy(update={"consumed": 60.0, "observed_at": 2.0})
        aggregates = AgentRuntimeTelemetryService.aggregate_resources(
            [_record(old, suffix="old"), _record(current, suffix="current")]
        )
        self.assertEqual(len(aggregates), 1)
        self.assertEqual(aggregates[0].consumed, 60)
        self.assertEqual(aggregates[0].utilization_percent, 60)

    def test_codex_windows_normalize_as_multiple_authoritative_resources(self) -> None:
        resources = RuntimeService._rate_limit_resources(
            {
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "primary": {
                            "usedPercent": 64,
                            "windowDurationMins": 300,
                            "resetsAt": 2000,
                        },
                        "secondary": {
                            "usedPercent": 41,
                            "windowDurationMins": 10080,
                            "resetsAt": 3000,
                        },
                    }
                }
            }
        )
        self.assertEqual([item.label for item in resources], ["5 hour rate limit", "Weekly rate limit"])
        self.assertEqual([item.utilization_percent for item in resources], [64, 41])
        self.assertEqual([item.reset_at for item in resources], [2000, 3000])


if __name__ == "__main__":
    unittest.main()
