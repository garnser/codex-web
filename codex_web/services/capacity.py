from __future__ import annotations

import time

from codex_web.artifact_evidence import EvidenceCreate, EvidenceResult, EvidenceType
from codex_web.capacity import (
    CapacityAdmissionEvent,
    CapacityHealth,
    CapacityLease,
    CapacityPolicy,
    CapacityQualification,
    CapacityQualificationReport,
    CircuitRecord,
    CircuitStatus,
    WorkloadKind,
    WorkloadPriority,
)
from codex_web.identity import AuthenticationActor
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.storage.capacity import CapacityStore


class CapacityError(RuntimeError):
    pass


class CapacityDeferredError(CapacityError):
    def __init__(
        self,
        reason: str,
        *,
        retry_at: float | None = None,
    ) -> None:
        self.reason = reason
        self.retry_at = retry_at
        super().__init__(reason)


class CapacityService:
    def __init__(
        self,
        store: CapacityStore,
        *,
        evidence: ArtifactEvidenceService | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.evidence = evidence
        self.clock = clock

    def policy(self) -> CapacityPolicy:
        return self.store.load().policy

    def set_policy(self, policy: CapacityPolicy) -> CapacityPolicy:
        self.store.update(
            lambda state: state.model_copy(update={"policy": policy})
        )
        return policy

    @staticmethod
    def _scope_key(
        organization_id: str,
        workspace_id: str,
        component_key: str,
    ) -> str:
        return f"{organization_id}\x00{workspace_id}\x00{component_key}"

    @staticmethod
    def _active(lease: CapacityLease, now: float) -> bool:
        return lease.expires_at > now

    def _record_event(
        self,
        state,
        *,
        organization_id: str,
        workspace_id: str,
        workload: WorkloadKind,
        priority: WorkloadPriority,
        outcome: str,
        reason: str | None,
        now: float,
    ) -> None:
        state.history.append(
            CapacityAdmissionEvent(
                organization_id=organization_id,
                workspace_id=workspace_id,
                workload=workload,
                priority=priority,
                outcome=outcome,
                reason=reason,
                observed_at=now,
            )
        )
        state.history = state.history[-state.policy.max_history :]

    def _circuit(
        self,
        state,
        *,
        organization_id: str,
        workspace_id: str,
        component_key: str,
        now: float,
    ) -> CircuitRecord | None:
        key = self._scope_key(
            organization_id,
            workspace_id,
            component_key,
        )
        circuit = state.circuits.get(key)
        if circuit is None:
            return None
        if (
            circuit.status == CircuitStatus.OPEN
            and circuit.retry_at is not None
            and circuit.retry_at <= now
        ):
            circuit = circuit.model_copy(
                update={
                    "status": CircuitStatus.HALF_OPEN,
                    "updated_at": now,
                }
            )
            state.circuits[key] = circuit
        return circuit

    def acquire(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        workload: WorkloadKind,
        priority: WorkloadPriority,
        owner_ref: str,
        component_key: str | None = None,
        lease_seconds: float | None = None,
    ) -> CapacityLease:
        now = float(self.clock())
        admitted: list[CapacityLease] = []
        deferred: list[tuple[str, float | None]] = []

        def apply(state):
            policy = state.policy
            state.leases = {
                key: item
                for key, item in state.leases.items()
                if self._active(item, now)
            }

            circuit = None
            if component_key:
                circuit = self._circuit(
                    state,
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                    component_key=component_key,
                    now=now,
                )
                if circuit is not None and circuit.status == CircuitStatus.OPEN:
                    reason = f"circuit_open:{component_key}"
                    self._record_event(
                        state,
                        organization_id=organization_id,
                        workspace_id=workspace_id,
                        workload=workload,
                        priority=priority,
                        outcome="deferred",
                        reason=reason,
                        now=now,
                    )
                    deferred.append((reason, circuit.retry_at))
                    return state
                if circuit is not None and circuit.status == CircuitStatus.HALF_OPEN:
                    probing = any(
                        item.component_key == component_key
                        and item.organization_id == organization_id
                        and item.workspace_id == workspace_id
                        for item in state.leases.values()
                    )
                    if probing:
                        reason = f"circuit_half_open_probe_in_progress:{component_key}"
                        self._record_event(
                            state,
                            organization_id=organization_id,
                            workspace_id=workspace_id,
                            workload=workload,
                            priority=priority,
                            outcome="deferred",
                            reason=reason,
                            now=now,
                        )
                        deferred.append((reason, circuit.retry_at))
                        return state

            active = list(state.leases.values())
            global_count = len(active)
            tenant_count = sum(
                item.organization_id == organization_id
                and item.workspace_id == workspace_id
                for item in active
            )
            workload_count = sum(
                item.workload == workload
                for item in active
            )
            global_util = global_count / policy.max_global_inflight
            tenant_util = tenant_count / policy.max_tenant_inflight

            reason = None
            if global_count >= policy.max_global_inflight:
                reason = "global_hard_capacity_exhausted"
            elif tenant_count >= policy.max_tenant_inflight:
                reason = "tenant_hard_capacity_exhausted"
            else:
                workload_limit = policy.workload_limits.get(workload)
                if (
                    workload_limit is not None
                    and workload_count >= workload_limit
                ):
                    reason = f"workload_bulkhead_exhausted:{workload.value}"

            if reason is None and priority < WorkloadPriority.CRITICAL:
                noncritical_global = (
                    policy.max_global_inflight
                    - policy.critical_global_reserve
                )
                noncritical_tenant = (
                    policy.max_tenant_inflight
                    - policy.critical_tenant_reserve
                )
                if global_count >= noncritical_global:
                    reason = "critical_global_reserve_active"
                elif tenant_count >= noncritical_tenant:
                    reason = "critical_tenant_reserve_active"
                elif (
                    priority == WorkloadPriority.LOW
                    and (
                        global_util >= policy.low_priority_shed_threshold
                        or tenant_util >= policy.low_priority_shed_threshold
                    )
                ):
                    reason = "low_priority_load_shed"
                elif (
                    priority == WorkloadPriority.NORMAL
                    and (
                        global_util >= policy.load_shed_threshold
                        or tenant_util >= policy.load_shed_threshold
                    )
                ):
                    reason = "normal_priority_load_shed"

            if reason is None and workload == WorkloadKind.RECOVERY:
                recent = sum(
                    item.organization_id == organization_id
                    and item.workspace_id == workspace_id
                    and item.workload == WorkloadKind.RECOVERY
                    and item.outcome == "admitted"
                    and now - item.observed_at < 60.0
                    for item in state.history
                )
                if recent >= policy.recovery_admissions_per_minute:
                    reason = "recovery_storm_rate_limited"

            if reason is not None:
                retry_at = (
                    min(
                        (
                            item.expires_at
                            for item in active
                            if item.organization_id == organization_id
                            and item.workspace_id == workspace_id
                        ),
                        default=now + 1.0,
                    )
                )
                self._record_event(
                    state,
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                    workload=workload,
                    priority=priority,
                    outcome="deferred",
                    reason=reason,
                    now=now,
                )
                deferred.append((reason, retry_at))
                return state

            ttl = (
                policy.default_lease_seconds
                if lease_seconds is None
                else max(1.0, float(lease_seconds))
            )
            lease = CapacityLease(
                organization_id=organization_id,
                workspace_id=workspace_id,
                workload=workload,
                priority=priority,
                owner_ref=owner_ref,
                component_key=component_key,
                acquired_at=now,
                expires_at=now + ttl,
            )
            state.leases[lease.id] = lease
            self._record_event(
                state,
                organization_id=organization_id,
                workspace_id=workspace_id,
                workload=workload,
                priority=priority,
                outcome="admitted",
                reason=None,
                now=now,
            )
            admitted.append(lease)
            return state

        self.store.update(apply)
        if admitted:
            return admitted[0]
        reason, retry_at = deferred[0]
        raise CapacityDeferredError(reason, retry_at=retry_at)

    def release(self, lease_id: str) -> bool:
        released: list[bool] = []

        def apply(state):
            released.append(state.leases.pop(lease_id, None) is not None)
            return state

        self.store.update(apply)
        return released[0]

    def renew(
        self,
        lease_id: str,
        *,
        lease_seconds: float | None = None,
    ) -> CapacityLease:
        now = float(self.clock())
        updated: list[CapacityLease] = []

        def apply(state):
            current = state.leases.get(lease_id)
            if current is None or not self._active(current, now):
                raise CapacityDeferredError("capacity_lease_expired")
            ttl = (
                state.policy.default_lease_seconds
                if lease_seconds is None
                else max(1.0, float(lease_seconds))
            )
            replacement = current.model_copy(
                update={"expires_at": now + ttl}
            )
            state.leases[lease_id] = replacement
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def record_failure(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        component_key: str,
        reason: str,
    ) -> CircuitRecord:
        now = float(self.clock())
        result: list[CircuitRecord] = []

        def apply(state):
            key = self._scope_key(
                organization_id,
                workspace_id,
                component_key,
            )
            current = state.circuits.get(key)
            failures = (
                current.consecutive_failures if current is not None else 0
            ) + 1
            opened = failures >= state.policy.circuit_failure_threshold
            record = CircuitRecord(
                organization_id=organization_id,
                workspace_id=workspace_id,
                component_key=component_key,
                status=(
                    CircuitStatus.OPEN
                    if opened
                    else CircuitStatus.CLOSED
                ),
                consecutive_failures=failures,
                opened_at=(
                    now
                    if opened
                    else (
                        current.opened_at if current is not None else None
                    )
                ),
                retry_at=(
                    now + state.policy.circuit_cooldown_seconds
                    if opened
                    else None
                ),
                last_failure_reason=reason[:500],
                last_success_at=(
                    current.last_success_at if current is not None else None
                ),
                updated_at=now,
            )
            state.circuits[key] = record
            result.append(record)
            return state

        self.store.update(apply)
        return result[0]

    def record_success(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        component_key: str,
    ) -> CircuitRecord:
        now = float(self.clock())
        result: list[CircuitRecord] = []

        def apply(state):
            key = self._scope_key(
                organization_id,
                workspace_id,
                component_key,
            )
            record = CircuitRecord(
                organization_id=organization_id,
                workspace_id=workspace_id,
                component_key=component_key,
                status=CircuitStatus.CLOSED,
                consecutive_failures=0,
                last_success_at=now,
                updated_at=now,
            )
            state.circuits[key] = record
            result.append(record)
            return state

        self.store.update(apply)
        return result[0]

    def health(
        self,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> CapacityHealth:
        now = float(self.clock())
        state = self.store.load()
        active = [
            item
            for item in state.leases.values()
            if self._active(item, now)
        ]
        tenant = [
            item
            for item in active
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
        ]
        global_util = len(active) / state.policy.max_global_inflight
        tenant_util = len(tenant) / state.policy.max_tenant_inflight
        circuits = tuple(
            sorted(
                item.component_key
                for item in state.circuits.values()
                if item.organization_id == organization_id
                and item.workspace_id == workspace_id
                and (
                    item.status == CircuitStatus.OPEN
                    and (item.retry_at is None or item.retry_at > now)
                )
            )
        )
        recent = [
            item
            for item in state.history
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
            and now - item.observed_at <= 300.0
        ]
        return CapacityHealth(
            active_global=len(active),
            active_tenant=len(tenant),
            global_utilization=global_util,
            tenant_utilization=tenant_util,
            load_shed_mode=(
                global_util >= state.policy.load_shed_threshold
                or tenant_util >= state.policy.load_shed_threshold
            ),
            open_circuits=circuits,
            recent_admitted=sum(item.outcome == "admitted" for item in recent),
            recent_deferred=sum(item.outcome == "deferred" for item in recent),
            recent_rejected=sum(item.outcome == "rejected" for item in recent),
        )

    def qualify(
        self,
        report: CapacityQualificationReport,
        *,
        actor: AuthenticationActor,
        publish_evidence: bool = True,
    ) -> CapacityQualification:
        policy = self.policy()
        blockers: list[str] = []
        if report.lost_work_count:
            blockers.append("lost_work_observed")
        if report.error_rate > 0.01:
            blockers.append("error_rate_above_1_percent")
        if report.retry_amplification > 1.25:
            blockers.append("retry_amplification_above_1_25")
        if report.p95_latency_seconds > 30.0:
            blockers.append("p95_latency_above_30_seconds")
        if report.max_tenant_share > 0.90:
            blockers.append("tenant_fairness_above_90_percent")
        if report.target_concurrency > policy.max_global_inflight:
            blockers.append("tested_concurrency_exceeds_configured_hard_limit")
        qualification = CapacityQualification(
            report=report,
            passed=not blockers,
            blockers=tuple(blockers),
            evaluated_at=float(self.clock()),
        )

        if publish_evidence and self.evidence is not None:
            evidence = self.evidence.create_evidence(
                EvidenceCreate(
                    evidence_type=EvidenceType.POLICY_EVALUATION,
                    provider="codex-web",
                    source="capacity-resilience-qualification",
                    result=(
                        EvidenceResult.PASS
                        if qualification.passed
                        else EvidenceResult.FAIL
                    ),
                    summary=(
                        f"Capacity qualification {'passed' if qualification.passed else 'failed'} "
                        f"for {report.deployment_mode}/{report.workload.value}; "
                        f"concurrency={report.target_concurrency}; "
                        f"error_rate={report.error_rate:.4f}; "
                        f"retry_amplification={report.retry_amplification:.3f}"
                    ),
                    metadata={
                        "deployment_mode": report.deployment_mode,
                        "workload": report.workload.value,
                        "target_concurrency": report.target_concurrency,
                        "burst_size": report.burst_size,
                        "p95_latency_seconds": report.p95_latency_seconds,
                        "error_rate": report.error_rate,
                        "lost_work_count": report.lost_work_count,
                        "retry_amplification": report.retry_amplification,
                        "max_tenant_share": report.max_tenant_share,
                        "saturation_observed": report.queue_or_admission_saturation_observed,
                    },
                ),
                actor=actor,
            )
            qualification = qualification.model_copy(
                update={"evidence_id": evidence.id}
            )

        self.store.update(
            lambda state: self._persist_qualification(
                state,
                qualification,
            )
        )
        return qualification

    @staticmethod
    def _persist_qualification(state, qualification):
        state.qualifications[qualification.id] = qualification
        return state
