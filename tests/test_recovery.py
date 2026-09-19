from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.autonomy_audit import AutonomyAuditKind, AutonomyAuditPayload
from codex_web.crypto import KeyPurpose, ManagedKeyCreate
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.key_backends import LocalFileKeyBackend
from codex_web.recovery import (
    RecoveryDeploymentMode,
    RecoveryPolicy,
    RestoreVerificationStatus,
)
from codex_web.services.autonomy_audit import AutonomyAuditService
from codex_web.services.crypto_keys import CryptoKeyService
from codex_web.services.canonical_events import (
    CanonicalEventBus,
    CanonicalEventIngestionService,
)
from codex_web.services.recovery import (
    LocalBackupDestination,
    RecoveryConflictError,
    RecoveryService,
)
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.autonomy_audit import AutonomyAuditStore
from codex_web.storage.crypto_keys import CryptoKeyStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.recovery import RecoveryStore
from codex_web.storage.scheduler import SchedulerStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


class _Evidence:
    def __init__(self) -> None:
        self.items = []

    def create_evidence(self, payload, *, actor):
        item = SimpleNamespace(
            id=f"evidence-{len(self.items)+1}",
            payload=payload,
            actor=actor,
        )
        self.items.append(item)
        return item


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = SQLiteStateStore(root / "state.sqlite3")
        self.actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )
        self.other_actor = AuthenticationActor(
            identity_id="other-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-b",
            workspace_id="ws-b",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )
        self.crypto = CryptoKeyService(
            CryptoKeyStore(self.state),
            {"local": LocalFileKeyBackend(root / "keys")},
        )
        self.backup_key = self.crypto.create_key(
            ManagedKeyCreate(
                purpose=KeyPurpose.BACKUP,
                backend_type="local",
            ),
            actor=self.actor,
        )
        self.audit_store = AutonomyAuditStore(self.state)
        self.audit = AutonomyAuditService(self.audit_store)
        self.audit_store.append(
            AutonomyAuditPayload(
                kind=AutonomyAuditKind.INTEGRITY_VERIFICATION,
                organization_id="org-a",
                workspace_id="ws-a",
                outcome="verified",
                reason_code="fixture",
            )
        )
        self.clock = _Clock(1000.0)
        self.evidence = _Evidence()
        self.destination = LocalBackupDestination(root / "backups")
        self.service = RecoveryService(
            RecoveryStore(self.state),
            state_store=self.state,
            crypto=self.crypto,
            evidence=self.evidence,
            audit=self.audit,
            destinations=(self.destination,),
            clock=self.clock,
        )
        self.policy = RecoveryPolicy(
            deployment_mode=RecoveryDeploymentMode.LOCAL,
            backup_key_id=self.backup_key.id,
            destination_id="local",
            backup_interval_seconds=3600,
            restore_verification_interval_seconds=86400,
            retention_count=2,
            require_audit_integrity=True,
        )
        self.service.configure(self.policy, actor=self.actor)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _seed_executable_state(self) -> None:
        self.state.put(
            "autonomy",
            {
                "schema_version": "2.0",
                "control": {"mode": "active"},
            },
        )
        self.state.put(
            "scheduler",
            {
                "schema_version": "1.0",
                "schedules": {
                    "schedule-a": {
                        "status": "active",
                        "lease_owner": "old-instance",
                        "lease_expires_at": 999999,
                    }
                },
            },
        )
        self.state.put(
            "action_intents",
            {
                "schema_version": "1.1",
                "intents": [
                    {
                        "id": "intent-a",
                        "status": "pending",
                        "lease": {
                            "owner": "old-worker",
                            "acquired_at": 1,
                            "expires_at": 999999,
                        },
                        "last_error": None,
                    }
                ],
                "receipts": [],
                "verifications": [],
                "inbox": [],
            },
        )

    def test_backup_is_encrypted_and_restore_verification_publishes_evidence(self):
        self.state.put("business", {"value": 42})
        manifest = self.service.create_backup(actor=self.actor)

        raw = self.destination.get(manifest.destination_ref)
        self.assertNotIn(b'"business"', raw)
        envelope = json.loads(raw)
        self.assertEqual(envelope["key_id"], self.backup_key.id)
        self.assertEqual(manifest.document_count, len(manifest.namespaces))
        self.assertEqual(manifest.audit_root_hash, self.audit.verify(
            organization_id="org-a",
            workspace_id="ws-a",
        ).root_hash)

        verification = self.service.verify_restore(
            manifest.id,
            actor=self.actor,
            publish_evidence=True,
        )
        self.assertEqual(verification.status, RestoreVerificationStatus.PASS)
        self.assertTrue(verification.checksum_valid)
        self.assertTrue(verification.schema_compatible)
        self.assertTrue(verification.key_manifest_valid)
        self.assertTrue(verification.audit_continuity_valid)
        self.assertFalse(verification.side_effects_enabled)
        self.assertEqual(verification.evidence_id, "evidence-1")
        self.assertEqual(
            self.evidence.items[0].payload.source,
            "recovery-restore-verification",
        )
        self.assertEqual(
            self.evidence.items[0].payload.result.value,
            "pass",
        )

    def test_encrypted_backup_tampering_is_detected(self):
        manifest = self.service.create_backup(actor=self.actor)
        path = Path(manifest.destination_ref.removeprefix("file://"))
        raw = bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 1
        path.write_bytes(bytes(raw))

        verification = self.service.verify_restore(
            manifest.id,
            actor=self.actor,
            publish_evidence=False,
        )
        self.assertEqual(verification.status, RestoreVerificationStatus.FAIL)
        self.assertTrue(
            any(
                blocker.startswith("restore_validation_failed:")
                for blocker in verification.blockers
            )
        )

    def test_wrong_tenant_cannot_decrypt_scope_bound_backup(self):
        manifest = self.service.create_backup(actor=self.actor)
        with self.assertRaises(Exception):
            self.service._snapshot_from_backup(
                manifest,
                actor=self.other_actor,
            )

    def test_isolated_restore_pauses_execution_surfaces(self):
        self._seed_executable_state()
        manifest = self.service.create_backup(actor=self.actor)
        target = SQLiteStateStore(
            Path(self.temp.name) / "isolated" / "state.sqlite3"
        )

        verification = self.service.restore_into_isolated(
            manifest.id,
            target,
            actor=self.actor,
        )
        self.assertEqual(verification.status, RestoreVerificationStatus.PASS)

        autonomy = target.get("autonomy")
        self.assertEqual(autonomy["control"]["mode"], "paused")
        scheduler = target.get("scheduler")
        self.assertEqual(
            scheduler["schedules"]["schedule-a"]["status"],
            "paused",
        )
        self.assertIsNone(
            scheduler["schedules"]["schedule-a"]["lease_owner"]
        )
        intents = target.get("action_intents")
        intent = intents["intents"][0]
        self.assertEqual(intent["status"], "requires_reconciliation")
        self.assertIsNone(intent["lease"])
        self.assertIn("reconciliation", intent["last_error"])

    def test_health_requires_rpo_and_fresh_successful_restore_drill(self):
        manifest = self.service.create_backup(actor=self.actor)
        verification = self.service.verify_restore(
            manifest.id,
            actor=self.actor,
            publish_evidence=False,
        )
        self.assertEqual(verification.status, RestoreVerificationStatus.PASS)
        health = self.service.health(actor=self.actor)
        self.assertTrue(health.rpo_satisfied)
        self.assertTrue(health.recovery_qualified)

        self.clock.value += 90_000
        stale = self.service.health(actor=self.actor)
        self.assertFalse(stale.rpo_satisfied)
        self.assertFalse(stale.recovery_qualified)
        self.assertIn("rpo_not_satisfied", stale.blockers)
        self.assertIn("restore_verification_not_fresh", stale.blockers)

    def test_retention_deletes_old_encrypted_backups(self):
        first = self.service.create_backup(actor=self.actor)
        self.clock.value += 10
        second = self.service.create_backup(actor=self.actor)
        self.clock.value += 10
        third = self.service.create_backup(actor=self.actor)

        state = self.service.store.load()
        self.assertNotIn(first.id, state.backups)
        self.assertIn(second.id, state.backups)
        self.assertIn(third.id, state.backups)
        self.assertFalse(
            Path(first.destination_ref.removeprefix("file://")).exists()
        )


class ScheduledRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = SQLiteStateStore(root / "state.sqlite3")
        self.clock = _Clock(1000.0)
        self.actor = AuthenticationActor(
            identity_id="recovery-service",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )
        self.crypto = CryptoKeyService(
            CryptoKeyStore(self.state),
            {"local": LocalFileKeyBackend(root / "keys")},
        )
        self.key = self.crypto.create_key(
            ManagedKeyCreate(
                purpose=KeyPurpose.BACKUP,
                backend_type="local",
            ),
            actor=self.actor,
        )
        event_store = CanonicalEventStore(self.state)
        self.bus = CanonicalEventBus(event_store)
        ingestion = CanonicalEventIngestionService(self.bus)
        self.scheduler = SchedulerService(
            SchedulerStore(self.state),
            ingestion,
            clock=self.clock,
            owner_id="scheduler-test",
        )
        self.evidence = _Evidence()
        self.service = RecoveryService(
            RecoveryStore(self.state),
            state_store=self.state,
            crypto=self.crypto,
            evidence=self.evidence,
            scheduler=self.scheduler,
            canonical_events=ingestion,
            service_actor=self.actor,
            destinations=(LocalBackupDestination(root / "backups"),),
            clock=self.clock,
        )
        self.service.configure(
            RecoveryPolicy(
                backup_key_id=self.key.id,
                destination_id="local",
                backup_interval_seconds=60,
                restore_verification_interval_seconds=300,
                retention_count=5,
                require_audit_integrity=False,
            ),
            actor=self.actor,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_scheduler_creates_backup_then_runs_restore_verification(self):
        state = self.service.store.load()
        self.assertIsNotNone(state.backup_schedule_id)
        self.assertIsNotNone(state.verification_schedule_id)

        self.state.put("business", {"important": True})
        self.clock.value = 1061.0
        first = await self.scheduler.run_due()
        self.assertGreaterEqual(first.emitted, 1)
        backups = list(self.service.store.load().backups.values())
        self.assertTrue(backups)

        self.clock.value = 1301.0
        second = await self.scheduler.run_due()
        self.assertGreaterEqual(second.emitted, 1)
        verifications = list(
            self.service.store.load().verifications.values()
        )
        self.assertTrue(verifications)
        self.assertEqual(
            verifications[-1].status,
            RestoreVerificationStatus.PASS,
        )
        self.assertTrue(
            any(
                item.payload.source == "recovery-restore-verification"
                for item in self.evidence.items
            )
        )


if __name__ == "__main__":
    unittest.main()
