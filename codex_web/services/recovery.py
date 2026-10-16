from __future__ import annotations

import contextvars

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from codex_web.artifact_evidence import (
    EvidenceCreate,
    EvidenceResult,
    EvidenceType,
)
from codex_web.autonomy_audit import AuditIntegrityStatus, AutonomyAuditState
from codex_web.storage.autonomy_audit import AutonomyAuditStore
from codex_web.canonical_events import CanonicalEventType
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.canonical_json import canonical_json_bytes
from codex_web.crypto import (
    CryptoKeyState,
    EncryptedEnvelope,
    EncryptionContext,
    KeyManifestEntry,
    KeyPurpose,
    KeyStatus,
    KeyVersionStatus,
)
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
from codex_web.storage.action_intents import ActionIntentStore
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
        from codex_web.services.recovery_executor import RecoveryExecutor
        from codex_web.services.recovery_schedules import RecoveryScheduleAdmission
        self._manual_context = contextvars.ContextVar('recovery_manual_authentication', default=None)
        self._scheduled_executor = RecoveryExecutor()
        self._scheduled_admission = RecoveryScheduleAdmission(self, f"recovery-{uuid.uuid4().hex}")
        self._scheduled_stopped = False
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

    def _write_key_bound(self, updater, *, actor, requirements, active_key=None, scheduler_update=None, _firing=None, evidence_update=None, _activate_reconciliation=False):
        if self.crypto.store.store is not self.store.store:
            raise RecoveryConflictError('Recovery and keys require the same canonical transactional store')
        key_namespace = self.crypto.store.namespace

        manual = self._manual_context.get()
        def apply(documents):
            if manual is not None:
                identity, authenticate, original = manual
                from codex_web.services.recovery_reconciliation import require_operator
                current_actor = authenticate()
                require_operator(current_actor)
                if current_actor.identity_id != original.identity_id or current_actor.tenant != original.tenant:
                    raise RecoveryConflictError('Manual Recovery operator changed')
            keys = self.crypto.store._decode(documents[key_namespace])
            for entry in requirements:
                key = self.crypto._key(keys, entry.key_id, actor)
                if key.status == KeyStatus.REVOKED or any(
                    self.crypto._version(key, version).status == KeyVersionStatus.REVOKED
                    for version in entry.versions
                ):
                    raise RecoveryConflictError('A required key version is revoked; recovery reference rejected')
                if entry.key_id == active_key and (key.status != KeyStatus.ACTIVE or key.purpose != KeyPurpose.BACKUP):
                    raise RecoveryConflictError('Backup policy requires an active backup key')
            current = RecoveryStore._decode(documents[self.store.namespace])
            if _firing is not None:
                self._scheduled_admission.validate_policy(current, keys, _firing)
                schedules = self.scheduler.store._decode(documents[self.scheduler.store.namespace])
                self._scheduled_admission.validate_schedule(current, schedules, _firing)
            if evidence_update is not None:
                evidence_store = self.evidence.store
                documents[evidence_store.namespace] = evidence_update(
                    evidence_store._decode(documents[evidence_store.namespace])
                ).model_dump(mode="json")
            previous_version = current.schema_version
            updated = updater(current, keys)
            if previous_version == "1.2" and updated.schema_version != "1.2" and (
                    not _activate_reconciliation or not updated.reconciliations):
                raise RecoveryConflictError("First1.3 write requires proof-bound reconciliation")
            if scheduler_update is not None:
                schedules = self.scheduler.store._decode(documents[self.scheduler.store.namespace])
                scheduler_update(updated, schedules)
                documents[self.scheduler.store.namespace] = schedules.model_dump(mode='json')
            documents[self.store.namespace] = RecoveryStore._encode(updated)
            return documents

        defaults = {
            key_namespace: CryptoKeyState().model_dump(mode='json'),
            self.store.namespace: RecoveryStore._encode(RecoveryState()),
        }
        if scheduler_update is not None or _firing is not None:
            from codex_web.scheduler import SchedulerState
            if self.scheduler.store.store is not self.store.store:
                raise RecoveryConflictError('Recovery schedules require the same canonical transactional store')
            defaults[self.scheduler.store.namespace] = SchedulerState().model_dump(mode='json')
        if evidence_update is not None:
            from codex_web.artifact_evidence import ArtifactEvidenceState
            if self.evidence.store.store is not self.store.store:
                raise RecoveryConflictError("Evidence and recovery require the same canonical store")
            defaults[self.evidence.store.namespace] = ArtifactEvidenceState().model_dump(mode="json")
        if manual is not None:
            identity, _, _ = manual
            if identity.store.store is not self.store.store:
                raise RecoveryConflictError('Manual Recovery and identity require one canonical transactional store')
            # Lock the existing identity document alongside Recovery/keys so a
            # credential or membership change cannot race successful settlement.
            # Its complete payload is returned unchanged, not normalized.
            defaults[identity.store.namespace] = identity.store._empty()
        result = self.store.store.update_many(defaults, apply)
        return RecoveryStore._decode(result[self.store.namespace])

    def configure(self, policy: RecoveryPolicy, *, actor: AuthenticationActor,
                  expected_fingerprint: str | None = None, restored_from_id: str | None = None) -> RecoveryPolicy:
        from codex_web.services.recovery_policy import RecoveryPolicyControl
        return RecoveryPolicyControl(self).configure(policy, actor=actor,
            expected_fingerprint=expected_fingerprint, restored_from_id=restored_from_id)

    @staticmethod
    def _canonical_bytes(value: Any) -> bytes:
        return canonical_json_bytes(value)

    def _snapshot_documents(self) -> dict[str, Any]:
        documents = self.state_store.documents()
        if RecoveryStore.namespace in documents:
            # Preserve policy provenance without recursively embedding backup
            # and verification manifests in subsequent backups.
            recovery = RecoveryStore._decode(documents[RecoveryStore.namespace])
            documents[RecoveryStore.namespace] = RecoveryStore._encode(recovery.model_copy(update={
                'backups': {}, 'verifications': {},
            }))
        return documents

    def _snapshot_key_manifest(self, documents, actor):
        keys = self.crypto.store._decode(documents.get(self.crypto.store.namespace))
        return tuple(KeyManifestEntry(key_id=key.id, versions=versions)
            for key in keys.keys
            if self.crypto._same_scope(key, actor) and key.status != KeyStatus.REVOKED
            if (versions := tuple(version.version for version in key.versions
                                  if version.status != KeyVersionStatus.REVOKED)))

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
        _firing=None,
    ) -> BackupManifest:
        if _firing is not None:
            self._scheduled_admission.checkpoint(_firing)
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
        backup_material = (
            f"{actor.organization_id}:{actor.workspace_id}:{now}:{checksum}"
        ).encode("utf-8")
        backup_id = (
            f"backup-{hashlib.sha256(backup_material).hexdigest()[:32]}"
        )
        if _firing is not None:
            backup_id = _firing.effect_id
        key_manifest = self._snapshot_key_manifest(documents, actor)
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
        if _firing is not None:
            self._scheduled_admission.checkpoint(_firing)
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

        def apply(current: RecoveryState, _keys: CryptoKeyState) -> RecoveryState:
            current.backups[manifest.id] = manifest
            return current

        try:
            self._write_key_bound(apply, actor=actor, requirements=(
                *manifest.key_manifest,
                KeyManifestEntry(key_id=manifest.key_id, versions=(manifest.key_version,)),
            ), _firing=_firing)
        except Exception:
            try:
                destination.delete(destination_ref)
            except Exception:
                raise RecoveryConflictError('Key validation failed and encrypted candidate cleanup failed') from None
            raise
        self._enforce_retention(policy, actor=actor, _firing=_firing)
        return manifest

    def _enforce_retention(
        self,
        policy: RecoveryPolicy,
        *,
        actor: AuthenticationActor,
        _firing=None,
    ) -> None:
        if _firing is not None or self._manual_context.get() is not None:
            def retain(current, keys):
                rows = sorted((item for item in current.backups.values()
                    if (item.organization_id, item.workspace_id) == (actor.organization_id, actor.workspace_id)),
                    key=lambda item: (item.created_at, item.id), reverse=True)
                for item in rows[policy.retention_count:]:
                    destination = self.destinations.get(item.destination_id)
                    if destination is not None:
                        destination.delete(item.destination_ref)
                    current.backups.pop(item.id)
                return current
            # Policy/schedule/key admission and retention share the canonical
            # transaction. A superseded policy cannot sweep newer backups.
            self._write_key_bound(retain, actor=actor, requirements=(), _firing=_firing)
            return
        state = self.store.load()
        rows = sorted(
            (item for item in state.backups.values()
             if (item.organization_id, item.workspace_id) == (actor.organization_id, actor.workspace_id)),
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
        _requirements=None,
    ) -> BackupSnapshot:
        destination = self._destination(manifest.destination_id)
        encrypted = destination.get(manifest.destination_ref)
        if hashlib.sha256(encrypted).hexdigest() != manifest.envelope_sha256:
            raise RecoveryConflictError("encrypted backup checksum mismatch")
        envelope = EncryptedEnvelope.model_validate(json.loads(encrypted))
        if _requirements is not None:
            _requirements.append(KeyManifestEntry(key_id=envelope.key_id, versions=(envelope.key_version,)))
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
            state = AutonomyAuditStore._decode(raw)
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
        _firing=None,
    ) -> RestoreVerification:
        if _firing is not None:
            self._scheduled_admission.checkpoint(_firing)
        state = self.store.load()
        manifest = state.backups.get(backup_id)
        if manifest is None or (
            manifest.organization_id != actor.organization_id
            or manifest.workspace_id != actor.workspace_id
        ):
            raise RecoveryError("backup not found")
        frozen_requirements = list(manifest.key_manifest)
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
            snapshot = self._snapshot_from_backup(manifest, actor=actor, _requirements=frozen_requirements)
            frozen_requirements.extend(snapshot.key_manifest)
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
            **({"id": _firing.effect_id} if _firing is not None else {}),
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

        def settle(evidence_update=None):
            def persist(current, keys):
                self._persist_verification(current, verification)
                if _firing is not None and verification.status == RestoreVerificationStatus.PASS:
                    current.scheduled_firings[_firing.event_id] = _firing.model_copy(update={
                        "status": "succeeded", "finished_at": float(self.clock()), "error_code": None})
                return current
            self._write_key_bound(persist, actor=actor,
                requirements=tuple(frozen_requirements) if verification.status == RestoreVerificationStatus.PASS else (),
                _firing=_firing, evidence_update=evidence_update)

        atomic_evidence = publish_evidence and hasattr(self.evidence, "store")
        def commit_evidence(apply):
            nonlocal verification
            def prepare(current):
                nonlocal verification
                updated = apply(current)
                verification = verification.model_copy(update={"evidence_id": updated.evidence[-1].id})
                return updated
            settle(prepare)

        if publish_evidence:
            if _firing is not None:
                self._scheduled_admission.checkpoint(_firing)
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
                        "verification_id": verification.id,
                        **({"canonical_event_id": _firing.event_id} if _firing is not None else {}),
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
                **({"_commit": commit_evidence} if atomic_evidence else {}),
            )
            verification = verification.model_copy(
                update={"evidence_id": evidence.id}
            )

        if not atomic_evidence:
            settle()
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
        intent_records = restored.get(ActionIntentStore.records_namespace)
        candidates = list(intents.get("intents") or []) if isinstance(intents, dict) else []
        if isinstance(intent_records, dict):
            candidates.extend(raw for key, raw in intent_records.items() if key.startswith("intents:"))
            # Restored execution is suspended; readiness/lease aliases cannot
            # make historical work claimable before canonical reconciliation.
            for key in list(intent_records):
                if key.startswith(("pending:", "lease:")):
                    intent_records.pop(key)
        if candidates:
            for item in candidates:
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
        rto_satisfied = bool(
            latest_verify is not None
            and canonical_objective is not None
            and latest_verify.status == RestoreVerificationStatus.PASS
            and latest_verify.duration_seconds <= canonical_objective.rto_seconds
        )
        blockers = []
        scoped_firings = [item for item in state.scheduled_firings.values()
                          if (item.organization_id, item.workspace_id) == (actor.organization_id, actor.workspace_id)]
        from codex_web.services.recovery_reconciliation import unresolved_unknown
        if unresolved_unknown(self, state, actor, latest_backup, latest_verify):
            blockers.append("scheduled_recovery_requires_reconciliation")
        if any(item.status == "running" and (item.owner_id != self._scheduled_admission.owner_id
                or not self._scheduled_executor.status()["inflight_work"]) for item in scoped_firings):
            blockers.append("scheduled_recovery_owner_unresolved")
        if not rpo_satisfied:
            blockers.append("rpo_not_satisfied")
        if not verify_fresh:
            blockers.append("restore_verification_not_fresh")
        if not rto_satisfied:
            blockers.append("rto_not_satisfied")
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
            rto_satisfied=rto_satisfied,
            recovery_qualified=not blockers,
            blockers=tuple(blockers),
        )

    async def run_manual(self, operation, *, authenticate, identity):
        """Existing bounded executor owns the ACTUAL manual writer to terminal.

        No request cancellation, lost HTTP response or local queue observation
        establishes an effect outcome or authorizes a replay.
        """
        from codex_web.services.recovery_reconciliation import require_operator
        def work():
            actor = authenticate()
            require_operator(actor)
            token = self._manual_context.set((identity, authenticate, actor))
            try:
                return operation(actor)
            finally:
                self._manual_context.reset(token)
        return await self._scheduled_executor.run('manual:' + uuid.uuid4().hex, work)

    async def _handle_schedule_event(self, event) -> None:
        if self.service_actor is None or not isinstance(event, CanonicalEventEnvelope):
            return
        await self._scheduled_executor.run(event.event_id, lambda: self._run_scheduled_firing(event))

    def _run_scheduled_firing(self, event):
        firing = self._scheduled_admission.admit(event)
        if firing is None:
            return
        try:
            if firing.trigger_type == self.BACKUP_TRIGGER:
                self.create_backup(actor=self.service_actor, _firing=firing)
            else:
                self.verify_restore(firing.backup_id, actor=self.service_actor,
                                    publish_evidence=True, _firing=firing)
        except BaseException as exc:
            self._scheduled_admission.finish(firing, succeeded=False, error_code=type(exc).__name__)
            raise
        self._scheduled_admission.finish(firing, succeeded=True)

    def close_scheduled_admission(self):
        self._scheduled_executor.close_admission()

    def start(self):
        if self._scheduled_executor.status()["accepting"]:
            return
        if not self._scheduled_stopped or self._scheduled_executor.status()["waiting_or_running"]:
            raise RecoveryConflictError("Scheduled recovery must finish stopping before restart")
        from codex_web.services.recovery_executor import RecoveryExecutor
        from codex_web.services.recovery_schedules import RecoveryScheduleAdmission
        self._scheduled_executor = RecoveryExecutor()
        self._scheduled_admission = RecoveryScheduleAdmission(self, f"recovery-{uuid.uuid4().hex}")
        self._scheduled_stopped = False

    async def stop(self):
        try:
            await self._scheduled_executor.stop()
        finally:
            self._scheduled_stopped = not self._scheduled_executor.status()["inflight_work"]
