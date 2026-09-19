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
from codex_web.autonomy import (
    AutonomyCycleOutcome,
    AutonomyCycleRecord,
    AutonomyMode,
)
from codex_web.autonomy_audit import (
    AUDIT_HASH_ALGORITHM,
    CHECKPOINT_HASH_ALGORITHM,
    GENESIS_HASH,
    AuditCheckpointExporter,
    AuditCheckpointSigner,
    AuditIntegrityResult,
    AuditIntegrityStatus,
    AuditSignature,
    AutonomyAuditCheckpoint,
    AutonomyAuditKind,
    AutonomyAuditMetrics,
    AutonomyAuditPayload,
    AutonomyAuditRecord,
    AutonomyReliabilityPolicy,
    AutonomySafetySignal,
    AutonomySafetySignalCreate,
)
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.identity import AuthenticationActor
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.autonomy_audit import AutonomyAuditStore


class FilesystemAuditCheckpointExporter:
    """Create-only checkpoint export suitable for an externally protected mount.

    The exporter never overwrites an existing checkpoint object. Deployments
    requiring WORM/object-lock semantics should mount an immutable/object-lock
    destination or provide another AuditCheckpointExporter implementation.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)

    def export(self, checkpoint: AutonomyAuditCheckpoint) -> str:
        path = self.directory / f"{checkpoint.id}.json"
        payload = json.dumps(
            checkpoint.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
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


class AutonomyAuditError(RuntimeError):
    pass


class AutonomyAuditIntegrityError(AutonomyAuditError):
    pass


class AutonomyAuditService:
    """Tamper-evident audit and deterministic reliability derivation."""

    def __init__(
        self,
        store: AutonomyAuditStore,
        *,
        autonomy_store: AutonomyStateStore | None = None,
        action_intents: ActionIntentService | None = None,
        evidence: ArtifactEvidenceService | None = None,
        signer: AuditCheckpointSigner | None = None,
        exporters: tuple[AuditCheckpointExporter, ...] = (),
        clock=time.time,
    ) -> None:
        self.store = store
        self.autonomy_store = autonomy_store
        self.action_intents = action_intents
        self.evidence = evidence
        self.signer = signer
        self.exporters = tuple(exporters)
        self.clock = clock

    @staticmethod
    def _scope(
        actor: AuthenticationActor | None,
        event: CanonicalEventEnvelope,
    ) -> tuple[str, str]:
        organization_id = (
            (event.tenant_id or "").strip()
            or (actor.organization_id if actor is not None else "")
        )
        workspace_id = (
            (event.workspace_id or "").strip()
            or (actor.workspace_id if actor is not None else "")
        )
        if not organization_id or not workspace_id:
            raise AutonomyAuditError(
                "autonomy audit requires canonical organization/workspace scope"
            )
        return organization_id, workspace_id

    def _intent_context(
        self,
        cycle: AutonomyCycleRecord,
        *,
        actor: AuthenticationActor | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "resource_ids": [],
            "goal_ids": [],
            "decision_ids": [],
            "provider_receipt_ids": [],
            "evidence_ids": [],
            "verification_ids": [],
            "providers": [],
            "authority_decisions": [],
            "policy_decisions": [],
            "intent_statuses": [],
        }
        if self.action_intents is None or actor is None:
            return result

        for intent_id in cycle.action_intent_ids:
            try:
                history = self.action_intents.history(intent_id, actor)
            except Exception:
                continue
            intent = history.get("intent") or {}
            result["resource_ids"].extend(intent.get("resource_ids") or [])
            if intent.get("goal_id"):
                result["goal_ids"].append(intent["goal_id"])
            if intent.get("decision_id"):
                result["decision_ids"].append(intent["decision_id"])
            if intent.get("provider_type") or intent.get("provider_instance"):
                result["providers"].append(
                    f"{intent.get('provider_type') or ''}:{intent.get('provider_instance') or ''}"
                )
            if intent.get("status"):
                result["intent_statuses"].append(
                    f"{intent_id}:{intent.get('status')}"
                )
            authority = intent.get("authority_recheck") or intent.get("authority_decision")
            if isinstance(authority, dict):
                result["authority_decisions"].append(
                    f"{intent_id}:{authority.get('decision_id') or 'none'}:"
                    f"{authority.get('outcome') or 'unknown'}:{authority.get('source') or 'unknown'}"
                )
            policy = intent.get("policy_decision")
            if isinstance(policy, dict):
                result["policy_decisions"].append(
                    f"{intent_id}:{policy.get('decision_id') or 'none'}:"
                    f"{policy.get('outcome') or 'unknown'}:{policy.get('source') or 'unknown'}"
                )
            for receipt in history.get("receipts") or []:
                if receipt.get("id"):
                    result["provider_receipt_ids"].append(receipt["id"])
                for evidence in receipt.get("evidence") or []:
                    if isinstance(evidence, dict) and evidence.get("reference"):
                        result["evidence_ids"].append(evidence["reference"])
            for verification in history.get("verifications") or []:
                if verification.get("id"):
                    result["verification_ids"].append(verification["id"])
                for evidence in verification.get("evidence") or []:
                    if isinstance(evidence, dict) and evidence.get("reference"):
                        result["evidence_ids"].append(evidence["reference"])

        for key in (
            "resource_ids",
            "goal_ids",
            "decision_ids",
            "provider_receipt_ids",
            "evidence_ids",
            "verification_ids",
            "providers",
            "authority_decisions",
            "policy_decisions",
            "intent_statuses",
        ):
            result[key] = list(dict.fromkeys(result[key]))
        return result

    def record_cycle(
        self,
        cycle: AutonomyCycleRecord,
        event: CanonicalEventEnvelope,
        *,
        actor: AuthenticationActor | None = None,
    ) -> AutonomyAuditRecord:
        organization_id, workspace_id = self._scope(actor, event)
        context = self._intent_context(cycle, actor=actor)
        details: dict[str, str | int | float | bool | None] = {}
        if context["providers"]:
            details["providers"] = ",".join(context["providers"])[:500]
        if context["authority_decisions"]:
            details["authority_decisions"] = "|".join(
                context["authority_decisions"]
            )[:500]
        if context["policy_decisions"]:
            details["policy_decisions"] = "|".join(
                context["policy_decisions"]
            )[:500]
        if context["intent_statuses"]:
            details["intent_statuses"] = "|".join(
                context["intent_statuses"]
            )[:500]
        if cycle.last_error:
            # Only the bounded exception class/reason code is persisted by callers;
            # never persist prompts, provider payloads or secret material here.
            details["last_error"] = cycle.last_error[:500]

        payload = AutonomyAuditPayload(
            kind=AutonomyAuditKind.CYCLE,
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_id=(
                event.payload.get("project_id")
                if isinstance(event.payload, dict)
                and isinstance(event.payload.get("project_id"), str)
                else None
            ),
            occurred_at=cycle.completed_at,
            cycle_id=cycle.id,
            event_id=cycle.event_id,
            event_type=cycle.event_type,
            source=cycle.source,
            correlation_id=cycle.correlation_id,
            causation_id=cycle.causation_id,
            actor_identity_id=actor.identity_id if actor is not None else None,
            actor_principal_kind=(
                actor.principal_kind.value if actor is not None else None
            ),
            actor_assurance=actor.assurance.value if actor is not None else None,
            autonomy_level=(
                cycle.autonomy_level.value
                if cycle.autonomy_level is not None
                else None
            ),
            policy_fingerprint=cycle.policy_fingerprint,
            break_glass_grant_id=cycle.break_glass_grant_id,
            approval_request_ids=cycle.approval_request_ids,
            outcome=cycle.outcome.value,
            reason_code=cycle.reason,
            reasoning_invoked=cycle.reasoning_invoked,
            reasoning_attempts=cycle.reasoning_attempts,
            model_provider_id=getattr(cycle, "model_provider_id", None),
            model_id=getattr(cycle, "model_id", None),
            model_revision=getattr(cycle, "model_revision", None),
            prompt_template_id=getattr(cycle, "prompt_template_id", None),
            model_routing_reason=getattr(cycle, "model_routing_reason", None),
            model_invocation_ids=getattr(cycle, "model_invocation_ids", ()),
            action_intent_ids=cycle.action_intent_ids,
            resource_ids=tuple(context["resource_ids"]),
            goal_ids=tuple(context["goal_ids"]),
            decision_ids=tuple(context["decision_ids"]),
            provider_receipt_ids=tuple(context["provider_receipt_ids"]),
            evidence_ids=tuple(context["evidence_ids"]),
            verification_ids=tuple(context["verification_ids"]),
            model_tokens=cycle.budget_usage.model_tokens,
            model_cost_usd=cycle.budget_usage.model_cost_usd,
            monetary_impact_usd=cycle.budget_usage.monetary_impact_usd,
            cloud_spend_usd=cycle.budget_usage.cloud_spend_usd,
            production_changes=cycle.budget_usage.production_changes,
            details=details,
        )
        record = self.store.append(payload)
        self._auto_suspend_if_required(
            organization_id,
            workspace_id,
            actor=actor,
        )
        return record

    def list_records(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        limit: int = 200,
    ) -> tuple[AutonomyAuditRecord, ...]:
        partition = AutonomyAuditRecord.partition_for(
            organization_id,
            workspace_id,
        )
        rows = [
            item
            for item in self.store.load().records
            if item.partition_id == partition
        ]
        rows.sort(key=lambda item: item.sequence, reverse=True)
        return tuple(rows[: max(1, min(limit, 1000))])

    def list_checkpoints(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        limit: int = 100,
    ) -> tuple[AutonomyAuditCheckpoint, ...]:
        partition = AutonomyAuditRecord.partition_for(
            organization_id,
            workspace_id,
        )
        rows = [
            item
            for item in self.store.load().checkpoints
            if item.partition_id == partition
        ]
        rows.sort(key=lambda item: (item.sequence, item.created_at), reverse=True)
        return tuple(rows[: max(1, min(limit, 500))])

    def list_signals(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        active_only: bool = False,
    ) -> tuple[AutonomySafetySignal, ...]:
        rows = [
            item
            for item in self.store.load().signals
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
            and (not active_only or item.active)
        ]
        rows.sort(key=lambda item: (item.observed_at, item.id), reverse=True)
        return tuple(rows)

    def get(
        self,
        record_id: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AutonomyAuditRecord:
        partition = AutonomyAuditRecord.partition_for(
            organization_id,
            workspace_id,
        )
        item = next(
            (
                row
                for row in self.store.load().records
                if row.id == record_id and row.partition_id == partition
            ),
            None,
        )
        if item is None:
            raise AutonomyAuditError("autonomy audit record not found")
        return item

    @staticmethod
    def _expected_record_hash(record: AutonomyAuditRecord) -> tuple[str, str]:
        payload_hash = hashlib.sha256(
            record.payload.canonical_bytes()
        ).hexdigest()
        digest_input = (
            f"{AUDIT_HASH_ALGORITHM}\x00{record.partition_id}\x00"
            f"{record.sequence}\x00{record.previous_hash}\x00{payload_hash}"
        ).encode("utf-8")
        return payload_hash, hashlib.sha256(digest_input).hexdigest()

    def verify(
        self,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AuditIntegrityResult:
        partition = AutonomyAuditRecord.partition_for(
            organization_id,
            workspace_id,
        )
        rows = sorted(
            (
                item
                for item in self.store.load().records
                if item.partition_id == partition
            ),
            key=lambda item: item.sequence,
        )
        if not rows:
            return AuditIntegrityResult(
                status=AuditIntegrityStatus.EMPTY,
                partition_id=partition,
                records_checked=0,
            )

        previous_hash = GENESIS_HASH
        expected_sequence = 1
        for item in rows:
            payload_hash, record_hash = self._expected_record_hash(item)
            if item.sequence != expected_sequence:
                return AuditIntegrityResult(
                    status=AuditIntegrityStatus.FAILED,
                    partition_id=partition,
                    records_checked=expected_sequence - 1,
                    first_sequence=rows[0].sequence,
                    last_sequence=item.sequence,
                    root_hash=previous_hash,
                    failed_record_id=item.id,
                    reason="audit_sequence_gap",
                )
            if item.previous_hash != previous_hash:
                return AuditIntegrityResult(
                    status=AuditIntegrityStatus.FAILED,
                    partition_id=partition,
                    records_checked=expected_sequence - 1,
                    first_sequence=rows[0].sequence,
                    last_sequence=item.sequence,
                    root_hash=previous_hash,
                    failed_record_id=item.id,
                    reason="audit_previous_hash_mismatch",
                )
            if item.payload_hash != payload_hash:
                return AuditIntegrityResult(
                    status=AuditIntegrityStatus.FAILED,
                    partition_id=partition,
                    records_checked=expected_sequence - 1,
                    first_sequence=rows[0].sequence,
                    last_sequence=item.sequence,
                    root_hash=previous_hash,
                    failed_record_id=item.id,
                    reason="audit_payload_hash_mismatch",
                )
            if item.record_hash != record_hash:
                return AuditIntegrityResult(
                    status=AuditIntegrityStatus.FAILED,
                    partition_id=partition,
                    records_checked=expected_sequence - 1,
                    first_sequence=rows[0].sequence,
                    last_sequence=item.sequence,
                    root_hash=previous_hash,
                    failed_record_id=item.id,
                    reason="audit_record_hash_mismatch",
                )
            previous_hash = item.record_hash
            expected_sequence += 1

        checkpoint = self._latest_checkpoint(partition)
        if checkpoint is not None:
            checkpoint_row = next(
                (item for item in rows if item.sequence == checkpoint.sequence),
                None,
            )
            if (
                checkpoint_row is None
                or checkpoint_row.record_hash != checkpoint.root_hash
            ):
                return AuditIntegrityResult(
                    status=AuditIntegrityStatus.FAILED,
                    partition_id=partition,
                    records_checked=len(rows),
                    first_sequence=rows[0].sequence,
                    last_sequence=rows[-1].sequence,
                    root_hash=rows[-1].record_hash,
                    checkpoint_id=checkpoint.id,
                    reason="audit_checkpoint_root_mismatch",
                )
            if checkpoint.signature is not None:
                if self.signer is None:
                    return AuditIntegrityResult(
                        status=AuditIntegrityStatus.FAILED,
                        partition_id=partition,
                        records_checked=len(rows),
                        first_sequence=rows[0].sequence,
                        last_sequence=rows[-1].sequence,
                        root_hash=rows[-1].record_hash,
                        checkpoint_id=checkpoint.id,
                        reason="audit_checkpoint_signature_verifier_unavailable",
                    )
                signature = AuditSignature(
                    algorithm=checkpoint.signature_algorithm or "unknown",
                    key_ref=checkpoint.signing_key_ref or "unknown",
                    signature=checkpoint.signature,
                )
                if not self.signer.verify(
                    checkpoint.signing_payload(),
                    signature,
                ):
                    return AuditIntegrityResult(
                        status=AuditIntegrityStatus.FAILED,
                        partition_id=partition,
                        records_checked=len(rows),
                        first_sequence=rows[0].sequence,
                        last_sequence=rows[-1].sequence,
                        root_hash=rows[-1].record_hash,
                        checkpoint_id=checkpoint.id,
                        reason="audit_checkpoint_signature_invalid",
                    )

        return AuditIntegrityResult(
            status=AuditIntegrityStatus.VERIFIED,
            partition_id=partition,
            records_checked=len(rows),
            first_sequence=rows[0].sequence,
            last_sequence=rows[-1].sequence,
            root_hash=rows[-1].record_hash,
            checkpoint_id=checkpoint.id if checkpoint is not None else None,
        )

    def _latest_checkpoint(
        self,
        partition_id: str,
    ) -> AutonomyAuditCheckpoint | None:
        rows = [
            item
            for item in self.store.load().checkpoints
            if item.partition_id == partition_id
        ]
        return max(rows, key=lambda item: (item.sequence, item.created_at)) if rows else None

    def checkpoint(
        self,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AutonomyAuditCheckpoint:
        integrity = self.verify(
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        if integrity.status == AuditIntegrityStatus.FAILED:
            raise AutonomyAuditIntegrityError(
                integrity.reason or "audit integrity verification failed"
            )
        partition = integrity.partition_id
        previous = self._latest_checkpoint(partition)
        checkpoint = AutonomyAuditCheckpoint.unsigned(
            partition_id=partition,
            sequence=integrity.last_sequence or 0,
            root_hash=integrity.root_hash,
            previous_checkpoint_hash=(
                previous.checkpoint_hash if previous is not None else GENESIS_HASH
            ),
            created_at=float(self.clock()),
        )
        if self.signer is not None:
            signature = self.signer.sign(checkpoint.signing_payload())
            checkpoint = checkpoint.model_copy(
                update={
                    "signature_algorithm": signature.algorithm,
                    "signature": signature.signature,
                    "signing_key_ref": signature.key_ref,
                }
            )
        refs = tuple(
            exporter.export(checkpoint)
            for exporter in self.exporters
        )
        if refs:
            checkpoint = checkpoint.model_copy(update={"export_refs": refs})
        return self.store.add_checkpoint(checkpoint)

    def redact(
        self,
        record_id: str,
        reason: str,
        *,
        actor: AuthenticationActor,
    ) -> AutonomyAuditRecord:
        target = self.get(
            record_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        return self.store.append(
            AutonomyAuditPayload(
                kind=AutonomyAuditKind.REDACTION,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                occurred_at=float(self.clock()),
                actor_identity_id=actor.identity_id,
                actor_principal_kind=actor.principal_kind.value,
                actor_assurance=actor.assurance.value,
                target_audit_record_id=target.id,
                reason_code="governed_redaction",
                details={"reason": reason[:500]},
            )
        )

    def public_record(
        self,
        record: AutonomyAuditRecord,
    ) -> dict[str, Any]:
        state = self.store.load()
        redacted = any(
            item.payload.kind == AutonomyAuditKind.REDACTION
            and item.payload.target_audit_record_id == record.id
            for item in state.records
        )
        payload = record.model_dump(mode="json")
        if redacted and record.payload.kind != AutonomyAuditKind.REDACTION:
            minimized = record.payload.model_copy(
                update={
                    "actor_identity_id": None,
                    "details": {"redacted": True},
                }
            )
            payload["payload"] = minimized.model_dump(mode="json")
            payload["redacted"] = True
        else:
            payload["redacted"] = False
        return payload

    def metrics(
        self,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AutonomyAuditMetrics:
        partition = AutonomyAuditRecord.partition_for(
            organization_id,
            workspace_id,
        )
        rows = [
            item
            for item in self.store.load().records
            if item.partition_id == partition
            and item.payload.kind == AutonomyAuditKind.CYCLE
        ]
        rows.sort(key=lambda item: item.sequence)
        sample_count = len(rows)
        completed = sum(
            item.payload.outcome == AutonomyCycleOutcome.COMPLETED.value
            for item in rows
        )
        failed = sum(
            item.payload.outcome == AutonomyCycleOutcome.FAILED.value
            for item in rows
        )
        blocked = sum(
            item.payload.outcome == AutonomyCycleOutcome.BLOCKED.value
            for item in rows
        )
        interventions = sum(
            bool(item.payload.approval_request_ids) for item in rows
        )
        tokens = sum(item.payload.model_tokens for item in rows)
        cost = sum(item.payload.model_cost_usd for item in rows)
        rollbacks = sum(
            "rolled_back" in str(item.payload.details.get("intent_statuses") or "")
            for item in rows
        )
        recoveries = sum(
            "recover" in (item.payload.reason_code or "").casefold()
            and item.payload.outcome == AutonomyCycleOutcome.COMPLETED.value
            for item in rows
        )
        incidents = sum(
            "incident" in (item.payload.event_type or "").casefold()
            for item in rows
        )
        consecutive_failures = 0
        for item in reversed(rows):
            if item.payload.outcome != AutonomyCycleOutcome.FAILED.value:
                break
            consecutive_failures += 1
        failure_rate = failed / sample_count if sample_count else 0.0
        intervention_rate = interventions / sample_count if sample_count else 0.0
        integrity = self.verify(
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        policy = self.store.load().reliability_policy
        reasons: list[str] = []
        if sample_count >= policy.minimum_samples:
            if failure_rate > policy.max_failure_rate:
                reasons.append("failure_rate_exceeded")
            if consecutive_failures >= policy.max_consecutive_failures:
                reasons.append("consecutive_failures_exceeded")
            if (
                policy.max_human_intervention_rate is not None
                and intervention_rate > policy.max_human_intervention_rate
            ):
                reasons.append("human_intervention_rate_exceeded")
            if (
                completed > 0
                and policy.max_tokens_per_success is not None
                and tokens / completed > policy.max_tokens_per_success
            ):
                reasons.append("tokens_per_success_exceeded")
            if (
                completed > 0
                and policy.max_cost_usd_per_success is not None
                and cost / completed > policy.max_cost_usd_per_success
            ):
                reasons.append("cost_per_success_exceeded")
        if integrity.status == AuditIntegrityStatus.FAILED:
            reasons.append("audit_integrity_failed")
        active_signals = [
            item
            for item in self.store.load().signals
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
            and item.active
            and item.kind in set(policy.suspension_signal_kinds)
        ]
        reasons.extend(
            f"signal:{item.kind.value}:{item.id}" for item in active_signals
        )
        return AutonomyAuditMetrics(
            sample_count=sample_count,
            completed=completed,
            failed=failed,
            blocked=blocked,
            human_interventions=interventions,
            rollbacks=rollbacks,
            recoveries=recoveries,
            incidents=incidents,
            total_model_tokens=tokens,
            total_model_cost_usd=cost,
            failure_rate=failure_rate,
            human_intervention_rate=intervention_rate,
            tokens_per_success=(tokens / completed if completed else None),
            cost_usd_per_success=(cost / completed if completed else None),
            consecutive_failures=consecutive_failures,
            successful_outcomes=completed,
            integrity_status=integrity.status,
            should_suspend=bool(reasons),
            suspension_reasons=tuple(dict.fromkeys(reasons)),
        )

    def set_reliability_policy(
        self,
        policy: AutonomyReliabilityPolicy,
    ) -> AutonomyReliabilityPolicy:
        return self.store.set_reliability_policy(policy)

    def add_signal(
        self,
        payload: AutonomySafetySignalCreate,
        *,
        actor: AuthenticationActor,
    ) -> AutonomySafetySignal:
        signal = AutonomySafetySignal(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            kind=payload.kind,
            source=payload.source,
            reason=payload.reason,
            evidence_ids=tuple(dict.fromkeys(payload.evidence_ids)),
            observed_at=float(self.clock()),
        )
        self.store.add_signal(signal)
        self._auto_suspend_if_required(
            actor.organization_id,
            actor.workspace_id,
            actor=actor,
        )
        return signal

    def clear_signal(
        self,
        signal_id: str,
    ) -> AutonomySafetySignal:
        try:
            return self.store.clear_signal(
                signal_id,
                cleared_at=float(self.clock()),
            )
        except KeyError as exc:
            raise AutonomyAuditError("autonomy safety signal not found") from exc

    def _auto_suspend_if_required(
        self,
        organization_id: str,
        workspace_id: str,
        *,
        actor: AuthenticationActor | None,
    ) -> None:
        if self.autonomy_store is None:
            return
        policy = self.store.load().reliability_policy
        if not policy.auto_suspend:
            return
        metrics = self.metrics(
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        if not metrics.should_suspend:
            return
        current = self.autonomy_store.load().control
        if current.mode == AutonomyMode.PAUSED:
            return
        self.autonomy_store.set_control(
            current.model_copy(update={"mode": AutonomyMode.PAUSED}),
            actor_id="autonomy-audit-watchdog",
        )
        self.store.append(
            AutonomyAuditPayload(
                kind=AutonomyAuditKind.AUTO_SUSPENSION,
                organization_id=organization_id,
                workspace_id=workspace_id,
                occurred_at=float(self.clock()),
                actor_identity_id=actor.identity_id if actor is not None else None,
                actor_principal_kind=(
                    actor.principal_kind.value if actor is not None else None
                ),
                actor_assurance=(
                    actor.assurance.value if actor is not None else None
                ),
                outcome="paused",
                reason_code="reliability_policy_auto_suspension",
                details={
                    "reasons": ",".join(metrics.suspension_reasons)[:500],
                },
            )
        )

    def publish_integrity_evidence(
        self,
        *,
        actor: AuthenticationActor,
    ):
        if self.evidence is None:
            raise AutonomyAuditError("artifact/evidence service is unavailable")
        result = self.verify(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        evidence = self.evidence.create_evidence(
            EvidenceCreate(
                evidence_type=EvidenceType.POLICY_EVALUATION,
                provider="codex-web",
                source="autonomy-audit-integrity",
                result=(
                    EvidenceResult.PASS
                    if result.status in {
                        AuditIntegrityStatus.VERIFIED,
                        AuditIntegrityStatus.EMPTY,
                    }
                    else EvidenceResult.FAIL
                ),
                summary=(
                    f"Autonomy audit integrity {result.status.value}; "
                    f"records={result.records_checked}; root={result.root_hash}"
                ),
                metadata={
                    "partition_id": result.partition_id,
                    "records_checked": result.records_checked,
                    "root_hash": result.root_hash,
                    "checkpoint_id": result.checkpoint_id or "",
                    "reason": result.reason or "",
                },
            ),
            actor=actor,
        )
        self.store.append(
            AutonomyAuditPayload(
                kind=AutonomyAuditKind.INTEGRITY_VERIFICATION,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                occurred_at=float(self.clock()),
                actor_identity_id=actor.identity_id,
                actor_principal_kind=actor.principal_kind.value,
                actor_assurance=actor.assurance.value,
                outcome=result.status.value,
                reason_code=result.reason or "integrity_verified",
                evidence_ids=(evidence.id,),
                details={
                    "root_hash": result.root_hash,
                    "records_checked": result.records_checked,
                },
            )
        )
        if result.status == AuditIntegrityStatus.FAILED:
            self.add_signal(
                AutonomySafetySignalCreate(
                    kind="audit_integrity",
                    source="autonomy-audit-verifier",
                    reason=result.reason or "audit integrity verification failed",
                    evidence_ids=(evidence.id,),
                ),
                actor=actor,
            )
        return evidence, result
