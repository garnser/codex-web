from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from codex_web.autonomy import (
    AutonomyControl,
    AutonomyCycleOutcome,
    AutonomyCycleRecord,
    AutonomyObservation,
    AutonomyReasoningResult,
)
from codex_web.autonomy_audit import (
    AuditIntegrityStatus,
    AuditSignature,
    AutonomyAuditKind,
    AutonomyReliabilityPolicy,
)
from codex_web.autonomy_policy import AutonomyCycleBudgetUsage
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.autonomy_audit import AutonomyAuditService
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.autonomy_audit import AutonomyAuditStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Signer:
    def sign(self, payload: bytes) -> AuditSignature:
        return AuditSignature(
            algorithm="test-sha256",
            key_ref="test-key-v1",
            signature=hashlib.sha256(b"test-key" + payload).hexdigest(),
        )

    def verify(self, payload: bytes, signature: AuditSignature) -> bool:
        return (
            signature.algorithm == "test-sha256"
            and signature.key_ref == "test-key-v1"
            and signature.signature
            == hashlib.sha256(b"test-key" + payload).hexdigest()
        )


class _Exporter:
    def __init__(self) -> None:
        self.ids = []

    def export(self, checkpoint):
        self.ids.append(checkpoint.id)
        return f"worm://audit/{checkpoint.id}"


class AutonomyAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.autonomy_store = AutonomyStateStore(self.sqlite)
        self.audit_store = AutonomyAuditStore(self.sqlite)
        self.actor = AuthenticationActor(
            identity_id="admin-a",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.service = AutonomyAuditService(
            self.audit_store,
            autonomy_store=self.autonomy_store,
            clock=lambda: 2_000_000_000.0,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def event(event_id="evt-1"):
        return CanonicalEventEnvelope(
            event_id=event_id,
            event_type="work.transition",
            occurred_at=1.0,
            source="test",
            correlation_id="corr-a",
            tenant_id="org-a",
            workspace_id="ws-a",
            payload={"project_id": "project-a"},
        )

    @staticmethod
    def cycle(
        cycle_id,
        event_id,
        outcome=AutonomyCycleOutcome.COMPLETED,
        *,
        reason="done",
        tokens=100,
        cost=0.25,
    ):
        return AutonomyCycleRecord(
            id=cycle_id,
            cycle_key=cycle_id,
            event_id=event_id,
            event_type="work.transition",
            source="test",
            correlation_id="corr-a",
            organization_id="org-a",
            workspace_id="ws-a",
            recursion_depth=0,
            reasoning_score=1.0,
            reasoning_invoked=True,
            reasoning_attempts=1,
            budget_usage=AutonomyCycleBudgetUsage(
                model_tokens=tokens,
                model_cost_usd=cost,
            ),
            outcome=outcome,
            reason=reason,
            started_at=100.0,
            completed_at=101.0,
        )

    def test_chain_verification_detects_historical_payload_mutation(self):
        first = self.service.record_cycle(
            self.cycle("cycle-1", "evt-1"),
            self.event("evt-1"),
            actor=self.actor,
        )
        second = self.service.record_cycle(
            self.cycle("cycle-2", "evt-2"),
            self.event("evt-2"),
            actor=self.actor,
        )
        self.assertEqual(second.previous_hash, first.record_hash)

        verified = self.service.verify(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        self.assertEqual(verified.status, AuditIntegrityStatus.VERIFIED)
        self.assertEqual(verified.records_checked, 2)

        raw = self.sqlite.get("autonomy_audit")
        raw["records"][0]["payload"]["reason_code"] = "tampered"
        self.sqlite.put("autonomy_audit", raw)

        failed = self.service.verify(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        self.assertEqual(failed.status, AuditIntegrityStatus.FAILED)
        self.assertEqual(failed.failed_record_id, first.id)
        self.assertEqual(failed.reason, "audit_payload_hash_mismatch")

    def test_signed_checkpoint_and_external_export_are_verifiable(self):
        signer = _Signer()
        exporter = _Exporter()
        service = AutonomyAuditService(
            self.audit_store,
            signer=signer,
            exporters=(exporter,),
            clock=lambda: 2_000_000_000.0,
        )
        service.record_cycle(
            self.cycle("cycle-1", "evt-1"),
            self.event("evt-1"),
            actor=self.actor,
        )
        checkpoint = service.checkpoint(
            organization_id="org-a",
            workspace_id="ws-a",
        )

        self.assertEqual(checkpoint.signature_algorithm, "test-sha256")
        self.assertEqual(checkpoint.signing_key_ref, "test-key-v1")
        self.assertEqual(
            checkpoint.export_refs,
            (f"worm://audit/{checkpoint.id}",),
        )
        self.assertEqual(exporter.ids, [checkpoint.id])
        verified = service.verify(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        self.assertEqual(verified.status, AuditIntegrityStatus.VERIFIED)
        self.assertEqual(verified.checkpoint_id, checkpoint.id)

    def test_governed_redaction_appends_proof_without_rewriting_chain(self):
        record = self.service.record_cycle(
            self.cycle("cycle-1", "evt-1"),
            self.event("evt-1"),
            actor=self.actor,
        )
        original_hash = record.record_hash

        redaction = self.service.redact(
            record.id,
            "privacy retention request",
            actor=self.actor,
        )
        self.assertEqual(redaction.payload.kind, AutonomyAuditKind.REDACTION)
        public = self.service.public_record(record)
        self.assertTrue(public["redacted"])
        self.assertIsNone(public["payload"]["actor_identity_id"])
        self.assertEqual(public["record_hash"], original_hash)
        self.assertEqual(
            self.service.verify(
                organization_id="org-a",
                workspace_id="ws-a",
            ).status,
            AuditIntegrityStatus.VERIFIED,
        )

    def test_reliability_threshold_can_auto_pause_autonomy(self):
        self.autonomy_store.set_control(
            AutonomyControl(),
            actor_id="test",
        )
        self.service.set_reliability_policy(
            AutonomyReliabilityPolicy(
                minimum_samples=2,
                max_failure_rate=0.25,
                max_consecutive_failures=2,
                auto_suspend=True,
                suspension_signal_kinds=(),
            )
        )
        for number in (1, 2):
            self.service.record_cycle(
                self.cycle(
                    f"cycle-{number}",
                    f"evt-{number}",
                    outcome=AutonomyCycleOutcome.FAILED,
                    reason="provider_failure",
                ),
                self.event(f"evt-{number}"),
                actor=self.actor,
            )

        self.assertEqual(
            self.autonomy_store.load().control.mode.value,
            "paused",
        )
        metrics = self.service.metrics(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        self.assertTrue(metrics.should_suspend)
        self.assertIn("failure_rate_exceeded", metrics.suspension_reasons)
        self.assertIn("consecutive_failures_exceeded", metrics.suspension_reasons)
        records = self.service.list_records(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        self.assertTrue(
            any(
                item.payload.kind == AutonomyAuditKind.AUTO_SUSPENSION
                for item in records
            )
        )

    async def test_controller_audits_deterministic_and_reasoned_cycles(self):
        controller = AutonomyController(
            self.autonomy_store,
            audit=self.service,
        )
        deterministic = await controller.process(
            self.event("det"),
            AutonomyObservation(
                deterministic_resolved=True,
                reasoning_score=0.0,
                reason="handled by deterministic rule",
            ),
            actor=self.actor,
        )
        self.assertEqual(
            deterministic.outcome,
            AutonomyCycleOutcome.DETERMINISTIC,
        )

        async def reasoner(*_args):
            return AutonomyReasoningResult(
                summary="reasoned",
                model_input_tokens=120,
                model_output_tokens=30,
                model_cost_usd=0.4,
                model_provider_id="openai",
                model_id="gpt-test",
                model_revision="r7",
                prompt_template_id="autonomy-v3",
                model_routing_reason="strategic-routing",
                model_invocation_ids=("model-invocation-1",),
            )

        reasoned = await controller.process(
            self.event("reasoned"),
            AutonomyObservation(
                deterministic_resolved=False,
                reasoning_score=1.0,
                reason="requires bounded reasoning",
            ),
            cycle_key="reasoned",
            reasoner=reasoner,
            actor=self.actor,
        )
        self.assertEqual(reasoned.outcome, AutonomyCycleOutcome.COMPLETED)
        self.assertEqual(reasoned.model_provider_id, "openai")

        records = self.service.list_records(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        cycle_rows = [
            item for item in records
            if item.payload.kind == AutonomyAuditKind.CYCLE
        ]
        self.assertEqual(len(cycle_rows), 2)
        latest = cycle_rows[0]
        self.assertEqual(latest.payload.model_provider_id, "openai")
        self.assertEqual(latest.payload.model_id, "gpt-test")
        self.assertEqual(
            latest.payload.model_invocation_ids,
            ("model-invocation-1",),
        )
        self.assertEqual(latest.payload.model_tokens, 150)
        self.assertEqual(latest.payload.model_cost_usd, 0.4)


if __name__ == "__main__":
    unittest.main()
