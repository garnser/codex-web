"""Capture-only qualification for an explicitly invoked local upgrade bootstrap.

This is not deployment qualification. It consumes the actual installed service's
fresh preflight, never a newly composed process's empty admission coordinator.
The caller owns authenticated transport, source/PID pins and the deploy lock.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from codex_web.artifact_evidence import EvidenceLifecycle, EvidenceResult, EvidenceType
from codex_web.canonical_json import canonical_json_bytes
from codex_web.services.recovery_reconciliation import require_operator
from codex_web.services.upgrades import UpgradeConflictError, UpgradePreflightError, UpgradeService


_CAPTURE_RECOVERY_BLOCKERS = frozenset({
    'recovery_not_qualified', 'recovery:rpo_not_satisfied',
    'recovery:restore_verification_not_fresh', 'recovery:rto_not_satisfied',
})


def _plan_context(plan):
    # A fresh real-service preflight may update only its own observations and
    # resulting readiness status. It cannot silently change the frozen plan.
    return hashlib.sha256(canonical_json_bytes(plan.model_dump(mode='json',
        exclude={'preflight', 'updated_at', 'status'}))).hexdigest()


@dataclass(frozen=True)
class BackupCaptureBinding:
    plan_id: str
    source_revision: str
    target_revision: str
    service_instance_id: str
    admission_epoch: int
    context_sha256: str

    def validate(self, plan):
        if (plan.id != self.plan_id or plan.pre_upgrade_backup_id is not None
                or _plan_context(plan) != self.context_sha256
                or plan.status not in {'draining', 'ready'}):
            raise UpgradeConflictError('Preinstall backup plan changed; preserve any captured manifest')


def qualify_capture(upgrades, observed_plan, *, actor, source_revision,
                    target_revision, service_instance_id, now, binding=None, offline=None):
    """Require canonical authority and a fresh *running-service* drain snapshot.

    ``observed_plan`` must come from the authenticated installed preflight API.
    No counter from ``upgrades.admission`` is used as process-wide evidence.
    """
    if offline is not None:
        offline.validate(observed_plan, now=now)
    upgrades.require_operator(actor)
    require_operator(actor)
    upgrades._require_service_drain_authority(actor)
    plan = upgrades.get(observed_plan.id, actor=actor)
    if plan.model_dump(mode='json') != observed_plan.model_dump(mode='json'):
        raise UpgradePreflightError('Installed preflight and canonical plan differ')
    if (plan.compatibility.deployment_mode != 'local'
            or not plan.require_drain or not plan.require_recovery_qualification
            or not plan.maintenance_mode or plan.maintenance_scope != 'service'
            or plan.status not in {'draining', 'ready'}
            or plan.completed_at is not None or plan.irreversible_boundary_crossed
            or plan.definition_migrations or any(step.attempts for step in plan.steps)
            or plan.pre_upgrade_backup_id is not None
            or upgrades.service_instance_id != service_instance_id
            or plan.service_instance_id != service_instance_id
            or plan.deployment_source_revision != source_revision
            or plan.deployment_target_revision != target_revision
            or plan.compatibility.source_state_schema_version
                != plan.compatibility.target_state_schema_version):
        raise UpgradePreflightError('Preinstall backup requires an unexecuted local service-drain plan')
    release = upgrades.releases.get(plan.release_id, actor=actor)
    if (release.build.source_revision != target_revision
            or release.version != plan.target_app_version
            or release.status in {'blocked', 'superseded', 'rolled_back'}):
        raise UpgradePreflightError('Preinstall backup Release binding changed')
    pf = plan.preflight
    if (pf is None or not pf.evidence_id
            or pf.service_instance_id != service_instance_id
            or pf.admission_epoch != plan.admission_epoch
            or pf.maintenance_scope != 'service'
            or plan.drain_started_at is None
            or not plan.drain_started_at <= pf.evaluated_at <= now
            or (offline is None and now - pf.evaluated_at > 30)
            or pf.active_action_intents or pf.active_worker_assignments
            or pf.active_native_admissions or pf.active_fenced_admissions
            or set(pf.blockers) - _CAPTURE_RECOVERY_BLOCKERS
            or pf.satisfied != (not pf.blockers)
            or pf.incompatible_worker_ids or pf.incompatible_extension_ids
            or pf.incompatible_definition_record_ids
            or pf.state_schema_version != plan.compatibility.source_state_schema_version):
        raise UpgradePreflightError('Fresh installed-service capture preflight is unavailable')
    fingerprint = upgrades._scope_fingerprint(upgrades._hosted_scopes())
    if (fingerprint != plan.hosted_scope_fingerprint
            or fingerprint != pf.hosted_scope_fingerprint
            or tuple(plan.covered_scopes) != tuple(pf.covered_scopes)
            or upgrades._active_action_count(actor, service_wide=True)
            or upgrades._active_assignment_count(actor, service_wide=True)):
        raise UpgradePreflightError('Current hosted scopes or active operations changed')
    evidence = upgrades.evidence.get_evidence(pf.evidence_id, actor=actor)
    expected = {'upgrade_plan_id': plan.id,
        'source_app_version': plan.current_app_version,
        'target_app_version': plan.target_app_version,
        'state_schema_version': pf.state_schema_version,
        'blocker_count': len(pf.blockers), 'active_action_intents': 0,
        'active_worker_assignments': 0, 'active_native_admissions': 0,
        'active_fenced_admissions': 0, 'maintenance_scope': 'service',
        'service_instance_id': service_instance_id,
        'hosted_scope_fingerprint': fingerprint}
    if (evidence.lifecycle != EvidenceLifecycle.VALID
            or (evidence.retention_expires_at is not None
                and evidence.retention_expires_at <= now)
            or evidence.evidence_type != EvidenceType.POLICY_EVALUATION
            or evidence.provider != 'codex-web' or evidence.source != 'upgrade-preflight'
            or evidence.result != (EvidenceResult.PASS if pf.satisfied else EvidenceResult.FAIL)
            or (evidence.organization_id, evidence.workspace_id)
                != (actor.organization_id, actor.workspace_id)
            or any(type(evidence.metadata.get(k)) is not type(v)
                or evidence.metadata.get(k) != v for k, v in expected.items())):
        raise UpgradePreflightError('Canonical installed preflight Evidence changed')
    current = BackupCaptureBinding(plan.id, source_revision, target_revision,
        service_instance_id, plan.admission_epoch, _plan_context(plan))
    if binding is not None:
        binding.validate(plan)
        if current != binding:
            raise UpgradeConflictError('Preinstall backup source context changed')
    return current


@dataclass(frozen=True)
class OfflineCaptureProof:
    """Historical pre-stop admission, never a fresh post-stop preflight.

    The executable creates this ONLY after qualifying a genuine fresh running
    service response and a clean stop of that pinned service. Every consumer
    proves the service remains stopped and the reviewed lock/source unchanged.
    Actual current time still governs credential/Evidence expiry and the
    separate finite offline window.
    """
    pre_stop_plan_sha256: str
    prepared_at: float
    deadline_at: float
    check_stopped: object

    def validate(self, observed_plan, *, now):
        pf = observed_plan.preflight
        if (pf is None or not pf.evaluated_at <= self.prepared_at
                or self.prepared_at - pf.evaluated_at > 30
                or not self.prepared_at <= now <= self.deadline_at
                or not 0 < self.deadline_at - self.prepared_at <= 600
                or hashlib.sha256(canonical_json_bytes(observed_plan.model_dump(mode='json'))).hexdigest()
                    != self.pre_stop_plan_sha256):
            raise UpgradePreflightError('Offline capture historical admission changed or expired')
        self.check_stopped()


@dataclass
class PreinstallBackupRuntime:
    """One owned canonical capture, with live-service guards at both edges.

    ``refresh`` is the authenticated installed-service preflight operation,
    not ``upgrades.preflight`` on this process. ``check_runtime`` verifies the
    caller's held deploy lock and exact reviewed source/installed PID pins.
    No supplied callback can grant actor authority: canonical services recheck
    it. This object does not install, qualify restore, or retry a capture.
    """
    upgrades: object
    identity: object
    authenticate: object
    refresh: object
    check_runtime: object
    journal: object
    source_revision: str
    target_revision: str
    service_instance_id: str
    clock: object
    offline: OfflineCaptureProof | None = None
    used: bool = False

    async def capture_once(self):
        if self.used:
            raise UpgradeConflictError('Preinstall backup runtime already used; inspect canonical outcome')
        self.used = True
        # run_manual owns and joins the real writer even on cancellation.
        def capture(actor):
            def qualify(binding=None):
                self.check_runtime()
                fresh_actor = self.authenticate()
                if not fresh_actor.has_same_authority(actor):
                    raise UpgradeConflictError('Authenticated operator changed during capture')
                observed = self.refresh()
                self.check_runtime()
                return qualify_capture(self.upgrades, observed, actor=fresh_actor,
                    source_revision=self.source_revision,
                    target_revision=self.target_revision,
                    service_instance_id=self.service_instance_id,
                    now=float(self.clock()), binding=binding, offline=self.offline)
            binding = qualify()
            self.journal('capture-started', {'plan_id': binding.plan_id})
            def captured(manifest):
                # Persist a known manifest before any post-capture guard can
                # refuse association. Failure is never permission to recapture.
                self.journal('manifest-created', {'plan_id': binding.plan_id,
                    'backup_id': manifest.id})
                qualify(binding)
            plan = self.upgrades.capture_pre_upgrade_backup(binding.plan_id,
                actor=actor, capture_context=binding, after_capture=captured,
                identity_guard=(self.identity, self.authenticate))
            self.journal('associated', {'plan_id': plan.id,
                'backup_id': plan.pre_upgrade_backup_id})
            return plan
        return await self.upgrades.recovery.run_manual(capture,
            authenticate=self.authenticate, identity=self.identity)


class _Unavailable:
    def __getattr__(self, name):
        raise UpgradeConflictError('Operation requires the installed application')


def compose_capture_runtime(state_store, *, operator_token, key_material_dir,
                            backup_dir, refresh, check_runtime, journal,
                            source_revision, target_revision, service_instance_id):
    """Compose existing canonical services, without application startup.

    The token is an EXISTING credential supplied by the operator, never minted
    by this tool. It is used only at the identity boundary. No identity,
    definition, worker, or provider bootstrap is performed here.
    """
    from types import SimpleNamespace
    from codex_web.services.identity import AuthenticationError, IdentityService
    from codex_web.storage.identity_state import IdentityStateStore
    identity = IdentityService(IdentityStateStore(state_store))
    def authenticate():
        try:
            actor = identity.authenticate_session(operator_token, require_csrf=False,
                touch=False, retire_expired=False).actor
        except AuthenticationError:
            actor = identity.authenticate_service_token(operator_token, touch=False)
        UpgradeService.require_operator(actor)
        require_operator(actor)
        return actor
    # Authority comes before any destination/backend constructor which might
    # create a directory. The enclosing operator also verifies actual locks.
    authenticate()
    check_runtime()
    if not key_material_dir.is_dir() or not backup_dir.is_dir():
        raise UpgradeConflictError('Canonical key and backup directories must already exist')
    from codex_web.key_backends import LocalFileKeyBackend
    from codex_web.services.artifact_evidence import ArtifactEvidenceService
    from codex_web.services.autonomy_audit import AutonomyAuditService
    from codex_web.services.crypto_keys import CryptoKeyService
    from codex_web.services.recovery import LocalBackupDestination, RecoveryService
    from codex_web.services.releases import ReleaseService
    from codex_web.storage.action_intents import ActionIntentStore
    from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
    from codex_web.storage.autonomy_audit import AutonomyAuditStore
    from codex_web.storage.crypto_keys import CryptoKeyStore
    from codex_web.storage.execution_workers import ExecutionWorkerStore
    from codex_web.storage.recovery import RecoveryStore
    from codex_web.storage.releases import ReleaseStore
    from codex_web.storage.upgrades import UpgradeStore
    # Missing state must not silently initialize a separate Recovery domain.
    for namespace in (IdentityStateStore.namespace, RecoveryStore.namespace,
                      CryptoKeyStore.namespace, UpgradeStore.namespace):
        if state_store.get(namespace) is None:
            raise UpgradeConflictError('Canonical bootstrap state is unavailable')
    # These stores have lazy legacy migration/initialization. A one-shot
    # capture must refuse missing/legacy state before calling their load()
    # readers, rather than create empty collections and infer zero writers.
    collections = (ActionIntentStore.records_namespace,
        ExecutionWorkerStore.worker_namespace, ExecutionWorkerStore.assignment_namespace,
        ExecutionWorkerStore.event_namespace, ExecutionWorkerStore.enrollment_namespace)
    if (not all(state_store.record_collections_exist(collections).values())
            or state_store.get(ExecutionWorkerStore.namespace) is not None
            or state_store.record_get(ActionIntentStore.records_namespace, ActionIntentStore._meta_key)
                not in ({'schema_version': '1.3'}, {'schema_version': '1.4'})):
        raise UpgradeConflictError('Canonical worker/action records require installed-service migration')
    if not state_store.record_collection_exists(AutonomyAuditStore.namespace):
        raise UpgradeConflictError('Canonical audit records require installed-service migration')
    AutonomyAuditStore._validate_meta(state_store.record_get(
        AutonomyAuditStore.namespace, AutonomyAuditStore.meta_key))
    unavailable = _Unavailable()
    evidence = ArtifactEvidenceService(ArtifactEvidenceStore(state_store))
    crypto = CryptoKeyService(CryptoKeyStore(state_store),
        {'local': LocalFileKeyBackend(key_material_dir)})
    recovery = RecoveryService(RecoveryStore(state_store), state_store=state_store,
        crypto=crypto, evidence=evidence,
        audit=AutonomyAuditService(AutonomyAuditStore(state_store)),
        destinations=(LocalBackupDestination(backup_dir),))
    releases = ReleaseService(ReleaseStore(state_store), artifacts=evidence,
        resources=unavailable, approvals=unavailable, action_intents=unavailable)
    upgrades = UpgradeService(UpgradeStore(state_store), state_store=state_store,
        definitions=unavailable, workers=SimpleNamespace(store=ExecutionWorkerStore(state_store)),
        extensions=unavailable, recovery=recovery, releases=releases,
        action_intents=SimpleNamespace(store=ActionIntentStore(state_store)),
        approvals=unavailable, evidence=evidence, identity=identity,
        service_instance_id=service_instance_id)
    return PreinstallBackupRuntime(upgrades, identity, authenticate, refresh,
        check_runtime, journal, source_revision, target_revision,
        service_instance_id, time.time)
