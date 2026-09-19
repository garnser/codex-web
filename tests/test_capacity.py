from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.capacity import (
    CapacityPolicy,
    CapacityQualificationReport,
    CircuitStatus,
    WorkloadKind,
    WorkloadPriority,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.capacity import CapacityDeferredError, CapacityService
from codex_web.storage.capacity import CapacityStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


class _Evidence:
    def __init__(self) -> None:
        self.rows = []

    def create_evidence(self, payload, *, actor):
        row = SimpleNamespace(
            id=f"evidence-{len(self.rows)+1}",
            payload=payload,
            actor=actor,
        )
        self.rows.append(row)
        return row


class CapacityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.clock = _Clock(1000.0)
        self.store = CapacityStore(
            SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        )
        self.evidence = _Evidence()
        self.service = CapacityService(
            self.store,
            evidence=self.evidence,
            clock=self.clock,
        )
        self.actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def configure_small(self) -> None:
        self.service.set_policy(
            CapacityPolicy(
                max_global_inflight=4,
                max_tenant_inflight=3,
                critical_global_reserve=1,
                critical_tenant_reserve=1,
                load_shed_threshold=1.0,
                low_priority_shed_threshold=1.0,
                workload_limits={WorkloadKind.ACTION: 4},
                recovery_admissions_per_minute=2,
                circuit_failure_threshold=2,
                circuit_cooldown_seconds=30,
            )
        )

    def acquire(
        self,
        *,
        org="org-a",
        ws="ws-a",
        workload=WorkloadKind.ACTION,
        priority=WorkloadPriority.NORMAL,
        owner="work",
        component=None,
    ):
        return self.service.acquire(
            organization_id=org,
            workspace_id=ws,
            workload=workload,
            priority=priority,
            owner_ref=owner,
            component_key=component,
            lease_seconds=60,
        )

    def test_critical_reserve_and_hard_tenant_ceiling(self):
        self.configure_small()
        self.acquire(owner="normal-1")
        self.acquire(owner="normal-2")
        with self.assertRaises(CapacityDeferredError) as blocked:
            self.acquire(owner="normal-3")
        self.assertEqual(
            blocked.exception.reason,
            "critical_tenant_reserve_active",
        )

        critical = self.acquire(
            owner="incident",
            priority=WorkloadPriority.CRITICAL,
        )
        self.assertEqual(critical.priority, WorkloadPriority.CRITICAL)
        with self.assertRaises(CapacityDeferredError) as hard:
            self.acquire(
                owner="incident-2",
                priority=WorkloadPriority.CRITICAL,
            )
        self.assertEqual(
            hard.exception.reason,
            "tenant_hard_capacity_exhausted",
        )

    def test_noisy_tenant_cannot_consume_reserved_shared_capacity(self):
        self.configure_small()
        self.acquire(owner="a1")
        self.acquire(owner="a2")
        with self.assertRaises(CapacityDeferredError):
            self.acquire(owner="a3")

        other = self.acquire(
            org="org-b",
            ws="ws-b",
            owner="b1",
        )
        self.assertEqual(other.organization_id, "org-b")

    def test_expired_capacity_lease_is_reclaimed(self):
        self.configure_small()
        first = self.acquire(owner="first")
        self.clock.value = first.expires_at + 1
        replacement = self.acquire(owner="replacement")
        self.assertNotEqual(first.id, replacement.id)
        health = self.service.health(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        self.assertEqual(health.active_tenant, 1)

    def test_recovery_storm_is_rate_limited(self):
        self.configure_small()
        one = self.acquire(
            workload=WorkloadKind.RECOVERY,
            priority=WorkloadPriority.HIGH,
            owner="recovery-1",
        )
        self.service.release(one.id)
        two = self.acquire(
            workload=WorkloadKind.RECOVERY,
            priority=WorkloadPriority.HIGH,
            owner="recovery-2",
        )
        self.service.release(two.id)
        with self.assertRaises(CapacityDeferredError) as blocked:
            self.acquire(
                workload=WorkloadKind.RECOVERY,
                priority=WorkloadPriority.HIGH,
                owner="recovery-3",
            )
        self.assertEqual(
            blocked.exception.reason,
            "recovery_storm_rate_limited",
        )

        self.clock.value += 61
        allowed = self.acquire(
            workload=WorkloadKind.RECOVERY,
            priority=WorkloadPriority.HIGH,
            owner="recovery-later",
        )
        self.assertIsNotNone(allowed)

    def test_circuit_opens_half_opens_and_success_closes(self):
        self.configure_small()
        first = self.service.record_failure(
            organization_id="org-a",
            workspace_id="ws-a",
            component_key="provider:x",
            reason="timeout",
        )
        self.assertEqual(first.status, CircuitStatus.CLOSED)
        opened = self.service.record_failure(
            organization_id="org-a",
            workspace_id="ws-a",
            component_key="provider:x",
            reason="timeout",
        )
        self.assertEqual(opened.status, CircuitStatus.OPEN)

        with self.assertRaises(CapacityDeferredError) as blocked:
            self.acquire(
                component="provider:x",
                owner="blocked",
            )
        self.assertEqual(
            blocked.exception.reason,
            "circuit_open:provider:x",
        )

        self.clock.value = opened.retry_at + 0.1
        probe = self.acquire(
            component="provider:x",
            owner="probe",
        )
        with self.assertRaises(CapacityDeferredError):
            self.acquire(
                component="provider:x",
                owner="second-probe",
            )
        self.service.release(probe.id)
        closed = self.service.record_success(
            organization_id="org-a",
            workspace_id="ws-a",
            component_key="provider:x",
        )
        self.assertEqual(closed.status, CircuitStatus.CLOSED)
        self.assertEqual(closed.consecutive_failures, 0)

    def test_low_priority_load_sheds_before_normal_hard_limit(self):
        self.service.set_policy(
            CapacityPolicy(
                max_global_inflight=10,
                max_tenant_inflight=10,
                critical_global_reserve=1,
                critical_tenant_reserve=1,
                load_shed_threshold=0.8,
                low_priority_shed_threshold=0.2,
                workload_limits={WorkloadKind.ACTION: 10},
            )
        )
        self.acquire(owner="one")
        self.acquire(owner="two")
        with self.assertRaises(CapacityDeferredError) as blocked:
            self.acquire(
                owner="low",
                priority=WorkloadPriority.LOW,
            )
        self.assertEqual(
            blocked.exception.reason,
            "low_priority_load_shed",
        )

    def test_capacity_qualification_publishes_named_evidence(self):
        qualification = self.service.qualify(
            CapacityQualificationReport(
                deployment_mode="replicated",
                workload=WorkloadKind.ACTION,
                target_concurrency=32,
                burst_size=100,
                duration_seconds=600,
                p95_latency_seconds=1.5,
                error_rate=0.001,
                lost_work_count=0,
                retry_amplification=1.05,
                max_tenant_share=0.45,
                queue_or_admission_saturation_observed=True,
            ),
            actor=self.actor,
        )
        self.assertTrue(qualification.passed)
        self.assertEqual(qualification.evidence_id, "evidence-1")
        self.assertEqual(
            self.evidence.rows[0].payload.source,
            "capacity-resilience-qualification",
        )
        self.assertEqual(
            self.evidence.rows[0].payload.result.value,
            "pass",
        )

        failed = self.service.qualify(
            CapacityQualificationReport(
                deployment_mode="replicated",
                workload=WorkloadKind.ACTION,
                target_concurrency=32,
                burst_size=100,
                duration_seconds=600,
                p95_latency_seconds=50,
                error_rate=0.05,
                lost_work_count=1,
                retry_amplification=2.0,
                max_tenant_share=0.95,
            ),
            actor=self.actor,
        )
        self.assertFalse(failed.passed)
        self.assertIn("lost_work_observed", failed.blockers)
        self.assertIn("retry_amplification_above_1_25", failed.blockers)


if __name__ == "__main__":
    unittest.main()
