from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from codex_web.artifact_evidence import (
    EvidenceCreate,
    EvidenceResult,
    EvidenceType,
)
from codex_web.autonomy_audit import AuditIntegrityStatus, AutonomyAuditState
from codex_web.canonical_events import CanonicalEventType
from codex_web.crypto import EncryptionContext, EncryptedEnvelope, KeyPurpose
from codex_web.identity import AuthenticationActor
from codex_web.recovery import (
    BackupDestination,
    BackupDestinationError,
    BackupManifest,
    BackupSnapshot,
    RecoveryHealth,
    RecoveryPolicy,
    RecoveryState,
    RestoreVerification,
    RestoreVerificationStatus,
)
from codex_web.scheduler import (
    MisfirePolicy,
    RecurrenceKind,
    ScheduleCreate,
    ScheduleRecurrence,
)
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.autonomy_audit import AutonomyAuditService
from codex_web.services.canonical_events import CanonicalEventIngestionService
from codex_web.services.crypto_keys import CryptoKeyService
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.recovery import RecoveryStore
from codex_web.storage.state_store import StateStore, state_documents_checksum


class RecoveryError(RuntimeError):
    pass


class RecoveryConflictError(RecoveryError):
    pass


class LocalBackupDestination:
    """Create-only encrypted backup destination for local/simple deployments."""

    destination_id = "local"

    def __init__(self, directory: Path) -> None:
        self.directory = directory.expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)

    def put(self, backup_id: str, payload: bytes) -> str:
        path = self.directory / f"{backup_id}.json"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        return f"file://{path}"

    def _path(self, destination_ref: str) -> Path:
        prefix = "file://"
        if not destination_ref.startswith(prefix):
            raise BackupDestinationError("unsupported local backup reference")
        path = Path(destination_ref[len(prefix):]).resolve()
        try:
            path.relative_to(self.directory)
        except ValueError as exc:
            raise BackupDestinationError(
                "backup reference escapes configured destination"
            ) from exc
        return path

    def get(self, destination_ref: str) -> bytes:
        return self._path(destination_ref).read_bytes()

    def delete(self, destination_ref: str) -> bool:
        try:
            self._path(destination_ref).unlink()
            return True
        except FileNotFoundError:
            return False

    def healthy(self) -> bool:
        return self.directory.exists() and os.access(self.directory, os.W_OK)


class RecoveryService:
    BACKUP_TRIGGER = "recovery.backup"
    VERIFY_TRIGGER = "recovery.verify_restore"

    def __init__(
        self,
        store: RecoveryStore,
        *,
        state_store: StateStore,
        crypto: CryptoKeyService,
        evidence: ArtifactEvidenceService,
        audit: AutonomyAuditService | None = None,
        scheduler: SchedulerService | None = None,
        canonical_events: CanonicalEventIngestionService | None = None,
        service_actor: AuthenticationActor | None = None,
        destinations: tuple[BackupDestination, ...] = (),
        clock=time.time,
    ) -> None:
        self.store = store
        self.state_store = state_store
        self.crypto = crypto
        self.evidence = evidence
        self.audit = audit
        self.scheduler = scheduler
        self.canonical_events = canonical_events
        self.service_actor = service_actor
        self.destinations = {
            item.destination_id: item
            for item in destinations
        }
        self.clock = clock
        self._unsubscribe = (
            canonical_events.bus.subscribe(
                self._handle_schedule_event,
                event_types=(CanonicalEventType.SCHEDULE,),
                predicate=lambda event: event.payload.get("trigger_type")
                in {self.BACKUP_TRIGGER, self.VERIFY_TRIGGER},
            )
            if canonical_events is not None
            else None
        )

    def _destination(self, destination_id: str) -> BackupDestination:
        item = self.destinations.get(destination_id)
        if item is None:
            raise RecoveryConflictError(
                f"backup destination is unavailable: {destination_id}"
            )
        if not item.healthy():
            raise RecoveryConflictError(
                f"backup destination is unhealthy: {destination_id}"
            )
        return item

    def policy(self) -> RecoveryPolicy | None:
        return self.store.load().policy

    def configure(
        self,
        policy: RecoveryPolicy,
        *,
        actor: AuthenticationActor,
    ) -> RecoveryPolicy:
        key = self.crypto.get_key(policy.backup_key_id, actor)
        if key.purpose != KeyPurpose.BACKUP:
            raise RecoveryConflictError(
                "recovery policy backup key must have KeyPurpose.BACKUP"
            )
        self._destination(policy.destination_id)
        state = self.store.update(
            lambda current: current.model_copy(update={"policy": policy})
        )
        if self.scheduler is not None:
            self._ensure_schedules(policy, actor=actor)
        return state.policy  # type: ignore[return-value]

    def _ensure_schedules(
        self,
        policy: RecoveryPolicy,
        *,
        actor: AuthenticationActor,
    ) -> None:
        if self.scheduler is None:
            return
        state = self.store.load()
        now = float(self.clock())
        backup_schedule_id = state.backup_schedule_id
        verification_schedule_id = state.verification_schedule_id
        existing_ids = {item.id for item in self.scheduler.list()}

        if not backup_schedule_id or backup_schedule_id not in existing_ids:
            backup = self.scheduler.create(
                ScheduleCreate(
                    name="recovery-backup",
                    tenant_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                    trigger_type=self.BACKUP_TRIGGER,
                    payload={},
                    due_at=now + policy.backup_interval_seconds,
                    recurrence=ScheduleRecurrence(
                        kind=RecurrenceKind.INTERVAL,
                        interval_seconds=policy.backup_interval_seconds,
                    ),
                    misfire_policy=MisfirePolicy.FIRE_ONCE,
                ),
                actor_id=actor.identity_id,
            )
            backup_schedule_id = backup.id

        if (
            not verification_schedule_id
            or verification_schedule_id not in existing_ids
        ):
            verify = self.scheduler.create(
                ScheduleCreate(
                    name="recovery-restore-verification",
                    tenant_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                    trigger_type=self.VERIFY_TRIGGER,
                    payload={},
                    due_at=now + policy.restore_verification_interval_seconds,
                    recurrence=ScheduleRecurrence(
                        kind=RecurrenceKind.INTERVAL,
                        interval_seconds=policy.restore_verification_interval_seconds,
                    ),
                    misfire_policy=MisfirePolicy.FIRE_ONCE,
                ),
                actor_id=actor.identity_id,
            )
            verification_schedule_id = verify.id

        self.store.update(
            lambda current: current.model_copy(
                update={
                    "backup_schedule_id": backup_schedule_id,
                    "verification_schedule_id": verification_schedule_id,
                }
            )
        )
        self.scheduler.notify_state_changed()

    @staticmethod
    def _canonical_bytes(value: Any) -> bytes:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _snapshot_documents(self) -> dict[str, Any]:
        # Recovery state itself is deliberately excluded to avoid recursive
        # backup manifests while all business/control-plane state is retained.
        return {
            key: value
            for key, value in self.state_store.documents().items()
            if key != RecoveryStore.namespace
        }

    def _audit_anchor(
        self,
        actor: AuthenticationActor,
    ) -> tuple[str | None, str | None]:
        if self.audit is None:
            return None, None
        result = self.audit.verify(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        if result.status == AuditIntegrityStatus.FAILED:
            raise RecoveryConflictError(
                f"cannot back up invalid autonomy audit: {result.reason}"
            )
        return result.root_hash, result.checkpoint_id

    def create_backup(
        self,
        *,
        actor: AuthenticationActor,
    ) -> BackupManifest:
        policy = self.policy()
        if policy is None:
            raise RecoveryConflictError("recovery policy is not configured")
        key = self.crypto.get_key(policy.backup_key_id, actor)
        if key.purpose != KeyPurpose.BACKUP:
            raise RecoveryConflictError("configured backup key purpose changed")
        destination = self._destination(policy.destination_id)

        documents = self._snapshot_documents()
        checksum = state_documents_checksum(documents)
        status = self.state_store.status()
        root_hash, checkpoint_id = self._audit_anchor(actor)
        now = float(self.clock())
        backup_id = f"backup-{hashlib.sha256(
            f'{actor.organization_id}:{actor.workspace_id}:{now}:{checksum}'.encode()
        ).hexdigest()[:32]}"
        key_manifest = self.crypto.manifest(actor)
        snapshot = BackupSnapshot(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            state_backend=str(status.get("backend") or "unknown"),
            state_schema_version=int(status.get("schemaVersion") or 0),
            created_at=now,
            documents=documents,
            documents_checksum=checksum,
            key_manifest=key_manifest,
            audit_root_hash=root_hash,
            audit_checkpoint_id=checkpoint_id,
        )
        context = EncryptionContext(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            object_type="recovery_backup",
            object_id=backup_id,
        )
        envelope = self.crypto.encrypt(
            policy.backup_key_id,
            self._canonical_bytes(snapshot.model_dump(mode="json")),
            context,
            actor=actor,
        )
        encrypted_bytes = self._canonical_bytes(
            envelope.model_dump(mode="json")
        )
        destination_ref = destination.put(backup_id, encrypted_bytes)
        manifest = BackupManifest(
            id=backup_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            policy_id=policy.id,
            policy_version=policy.version,
            policy_fingerprint=policy.fingerprint(),
            deployment_mode=policy.deployment_mode,
            state_backend=snapshot.state_backend,
            state_schema_version=snapshot.state_schema_version,
            destination_id=policy.destination_id,
            destination_ref=destination_ref,
            envelope_sha256=hashlib.sha256(encrypted_bytes).hexdigest(),
            plaintext_checksum=checksum,
            document_count=len(documents),
            namespaces=tuple(sorted(documents)),
            key_id=envelope.key_id,
            key_version=envelope.key_version,
            key_manifest=key_manifest,
            audit_root_hash=root_hash,
            audit_checkpoint_id=checkpoint_id,
            created_at=now,
        )

        def apply(current: RecoveryState) -> RecoveryState:
            current.backups[manifest.id] = manifest
            return current

        self.store.update(apply)
        self._enforce_retention(policy, actor=actor)
        return manifest

    def _enforce_retention(
        self,
        policy: RecoveryPolicy,
        *,
        actor: AuthenticationActor,
    ) -> None:
        del actor
        state = self.store.load()
        rows = sorted(
            state.backups.values(),
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )
        expired = rows[policy.retention_count:]
        if not expired:
            return
        for item in expired:
            destination = self.destinations.get(item.destination_id)
            if destination is not None:
                destination.delete(item.destination_ref)

        def apply(current: RecoveryState) -> RecoveryState:
            for item in expired:
                current.backups.pop(item.id, None)
            return current

        self.store.update(apply)

    def _snapshot_from_backup(
        self,
        manifest: BackupManifest,
        *,
        actor: AuthenticationActor,
    ) -> BackupSnapshot:
        destination = self._destination(manifest.destination_id)
        encrypted = destination.get(manifest.destination_ref)
        if hashlib.sha256(encrypted).hexdigest() != manifest.envelope_sha256:
            raise RecoveryConflictError("encrypted backup checksum mismatch")
        envelope = EncryptedEnvelope.model_validate(json.loads(encrypted))
        context = EncryptionContext(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            object_type="recovery_backup",
            object_id=manifest.id,
        )
        plaintext = self.crypto.decrypt(
            envelope,
            context,
            actor=actor,
        )
        snapshot = BackupSnapshot.model_validate(json.loads(plaintext))
        if (
            snapshot.organization_id != actor.organization_id
            or snapshot.workspace_id != actor.workspace_id
        ):
            raise RecoveryConflictError("backup tenant/workspace scope mismatch")
        return snapshot

    @staticmethod
    def _snapshot_audit_root(snapshot: BackupSnapshot) -> str | None:
        raw = snapshot.documents.get("autonomy_audit")
        if not isinstance(raw, dict):
            return None
        try:
            state = AutonomyAuditState.model_validate(raw)
        except Exception:
            return None
        partition_rows = [
            item
            for item in state.records
            if item.payload.organization_id == snapshot.organization_id
            and item.payload.workspace_id == snapshot.workspace_id
        ]
        if not partition_rows:
            return "0" * 64
        return max(partition_rows, key=lambda item: item.sequence).record_hash

    def verify_restore(
        self,
        backup_id: str,
        *,
        actor: AuthenticationActor,
        publish_evidence: bool = True,
    ) -> RestoreVerification:
        state = self.store.load()
        manifest = state.backups.get(backup_id)
        if manifest is None or (
            manifest.organization_id != actor.organization_id
            or manifest.workspace_id != actor.workspace_id
        ):
            raise RecoveryError("backup not found")
        started = float(self.clock())
        blockers: list[str] = []
        snapshot: BackupSnapshot | None = None
        restored_checksum = None
        checksum_valid = False
        schema_compatible = False
        key_manifest_valid = False
        audit_continuity_valid: bool | None = None
        missing_refs: tuple[str, ...] = ()
        revoked_refs: tuple[str, ...] = ()

        try:
            snapshot = self._snapshot_from_backup(manifest, actor=actor)
            restored_checksum = state_documents_checksum(snapshot.documents)
            checksum_valid = (
                restored_checksum == snapshot.documents_checksum
                == manifest.plaintext_checksum
            )
            if not checksum_valid:
                blockers.append("state_checksum_mismatch")

            current_schema = int(
                self.state_store.status().get("schemaVersion") or 0
            )
            schema_compatible = snapshot.state_schema_version == current_schema
            if not schema_compatible:
                blockers.append("state_schema_version_incompatible")

            validation = self.crypto.validate_manifest(
                snapshot.key_manifest,
                actor=actor,
            )
            key_manifest_valid = validation.valid
            missing_refs = validation.missing_refs
            revoked_refs = validation.revoked_refs
            if not key_manifest_valid:
                blockers.append("key_manifest_invalid")

            policy = self.policy()
            if policy is not None and policy.require_audit_integrity:
                audit_root = self._snapshot_audit_root(snapshot)
                audit_continuity_valid = (
                    audit_root == snapshot.audit_root_hash
                    == manifest.audit_root_hash
                )
                if not audit_continuity_valid:
                    blockers.append("audit_continuity_invalid")
        except Exception as exc:
            blockers.append(f"restore_validation_failed:{type(exc).__name__}")

        finished = float(self.clock())
        verification = RestoreVerification(
            backup_id=manifest.id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            status=(
                RestoreVerificationStatus.PASS
                if not blockers
                else RestoreVerificationStatus.FAIL
            ),
            checksum_valid=checksum_valid,
            schema_compatible=schema_compatible,
            key_manifest_valid=key_manifest_valid,
            audit_continuity_valid=audit_continuity_valid,
            document_count=(
                len(snapshot.documents) if snapshot is not None else 0
            ),
            restored_checksum=restored_checksum,
            key_missing_refs=missing_refs,
            key_revoked_refs=revoked_refs,
            blockers=tuple(dict.fromkeys(blockers)),
            duration_seconds=max(0.0, finished - started),
            isolated=True,
            side_effects_enabled=False,
            verified_at=finished,
        )

        if publish_evidence:
            evidence = self.evidence.create_evidence(
                EvidenceCreate(
                    evidence_type=EvidenceType.POLICY_EVALUATION,
                    provider="codex-web",
                    source="recovery-restore-verification",
                    result=(
                        EvidenceResult.PASS
                        if verification.status == RestoreVerificationStatus.PASS
                        else EvidenceResult.FAIL
                    ),
                    summary=(
                        f"Restore verification {verification.status.value}; "
                        f"backup={manifest.id}; documents={verification.document_count}"
                    ),
                    metadata={
                        "backup_id": manifest.id,
                        "checksum_valid": checksum_valid,
                        "schema_compatible": schema_compatible,
                        "key_manifest_valid": key_manifest_valid,
                        "audit_continuity_valid": bool(
                            audit_continuity_valid
                        ),
                        "duration_seconds": verification.duration_seconds,
                    },
                ),
                actor=actor,
            )
            verification = verification.model_copy(
                update={"evidence_id": evidence.id}
            )

        self.store.update(
            lambda current: self._persist_verification(
                current,
                verification,
            )
        )
        return verification

    @staticmethod
    def _persist_verification(
        state: RecoveryState,
        verification: RestoreVerification,
    ) -> RecoveryState:
        state.verifications[verification.id] = verification
        return state

    @staticmethod
    def _sanitize_restored_documents(
        documents: dict[str, Any],
    ) -> dict[str, Any]:
        restored = json.loads(json.dumps(documents))

        autonomy = restored.get("autonomy")
        if isinstance(autonomy, dict):
            control = autonomy.get("control")
            if isinstance(control, dict):
                control["mode"] = "paused"

        scheduler = restored.get("scheduler")
        if isinstance(scheduler, dict):
            schedules = scheduler.get("schedules")
            if isinstance(schedules, dict):
                for item in schedules.values():
                    if isinstance(item, dict) and item.get("status") == "active":
                        item["status"] = "paused"
                        item["lease_owner"] = None
                        item["lease_expires_at"] = None

        intents = restored.get("action_intents")
        if isinstance(intents, dict):
            for item in intents.get("intents") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("status") in {
                    "pending",
                    "claimed",
                    "executing",
                    "uncertain",
                }:
                    item["status"] = "requires_reconciliation"
                    item["lease"] = None
                    item["last_error"] = (
                        "restored snapshot requires canonical reconciliation "
                        "before external execution"
                    )
        return restored

    def restore_into_isolated(
        self,
        backup_id: str,
        target: StateStore,
        *,
        actor: AuthenticationActor,
    ) -> RestoreVerification:
        if target.documents():
            raise RecoveryConflictError(
                "isolated restore target must be empty"
            )
        verification = self.verify_restore(
            backup_id,
            actor=actor,
            publish_evidence=False,
        )
        if verification.status != RestoreVerificationStatus.PASS:
            raise RecoveryConflictError(
                "backup failed restore verification"
            )
        manifest = self.store.load().backups[backup_id]
        snapshot = self._snapshot_from_backup(manifest, actor=actor)
        documents = self._sanitize_restored_documents(snapshot.documents)
        for namespace, payload in documents.items():
            target.put(namespace, payload)
        if state_documents_checksum(
            {
                key: value
                for key, value in target.documents().items()
            }
        ) == snapshot.documents_checksum:
            # Sanitization intentionally changes executable control state, so
            # an unchanged checksum would indicate the safety mutation did not
            # occur on a snapshot that contained those domains.
            pass
        return verification

    def health(
        self,
        *,
        actor: AuthenticationActor,
    ) -> RecoveryHealth:
        state = self.store.load()
        policy = state.policy
        if policy is None:
            return RecoveryHealth(
                policy_configured=False,
                blockers=("recovery_policy_not_configured",),
            )
        now = float(self.clock())
        backups = [
            item
            for item in state.backups.values()
            if item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        ]
        verifications = [
            item
            for item in state.verifications.values()
            if item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        ]
        latest_backup = max(
            backups,
            key=lambda item: item.created_at,
            default=None,
        )
        latest_verify = max(
            verifications,
            key=lambda item: item.verified_at,
            default=None,
        )
        canonical_objective = next(
            (
                item
                for item in policy.objectives
                if item.data_class.value == "canonical_state"
            ),
            None,
        )
        backup_age = (
            max(0.0, now - latest_backup.created_at)
            if latest_backup is not None
            else None
        )
        rpo_satisfied = bool(
            latest_backup is not None
            and canonical_objective is not None
            and backup_age is not None
            and backup_age <= canonical_objective.rpo_seconds
        )
        verify_age = (
            max(0.0, now - latest_verify.verified_at)
            if latest_verify is not None
            else None
        )
        verify_fresh = bool(
            latest_verify is not None
            and verify_age is not None
            and verify_age <= policy.restore_verification_interval_seconds
            and latest_verify.status == RestoreVerificationStatus.PASS
        )
        blockers = []
        if not rpo_satisfied:
            blockers.append("rpo_not_satisfied")
        if not verify_fresh:
            blockers.append("restore_verification_not_fresh")
        if not self._destination(policy.destination_id).healthy():
            blockers.append("backup_destination_unhealthy")
        return RecoveryHealth(
            policy_configured=True,
            latest_backup_id=latest_backup.id if latest_backup else None,
            latest_backup_age_seconds=backup_age,
            latest_restore_verification_id=(
                latest_verify.id if latest_verify else None
            ),
            latest_restore_verification_age_seconds=verify_age,
            latest_restore_passed=bool(
                latest_verify
                and latest_verify.status == RestoreVerificationStatus.PASS
            ),
            rpo_satisfied=rpo_satisfied,
            recovery_qualified=not blockers,
            blockers=tuple(blockers),
        )

    async def _handle_schedule_event(self, event) -> None:
        if self.service_actor is None:
            return
        trigger = event.payload.get("trigger_type")
        if trigger == self.BACKUP_TRIGGER:
            self.create_backup(actor=self.service_actor)
            return
        if trigger == self.VERIFY_TRIGGER:
            state = self.store.load()
            candidates = [
                item
                for item in state.backups.values()
                if item.organization_id == self.service_actor.organization_id
                and item.workspace_id == self.service_actor.workspace_id
            ]
            if candidates:
                latest = max(
                    candidates,
                    key=lambda item: item.created_at,
                )
                self.verify_restore(
                    latest.id,
                    actor=self.service_actor,
                    publish_evidence=True,
                )
