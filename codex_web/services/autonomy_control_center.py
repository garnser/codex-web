from __future__ import annotations

from typing import Any

from codex_web.approval_requests import ApprovalRequestStatus
from codex_web.attention import AttentionStatus
from codex_web.incidents import IncidentStatus
from codex_web.identity import AuthenticationActor
from codex_web.releases import ReleaseStatus
from codex_web.upgrades import UpgradeStatus
from codex_web.services.approval_requests import ApprovalRequestService
from codex_web.services.attention import AttentionService
from codex_web.services.autonomy_audit import AutonomyAuditService
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.autonomy_policy import AutonomyPolicyService
from codex_web.services.capacity import CapacityService
from codex_web.services.coordination import StateStoreCoordinationBackend
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.incidents import IncidentService
from codex_web.services.orchestration_inspector import OrchestrationInspectorService
from codex_web.services.provider_capacity import ProviderCapacityService
from codex_web.services.recovery import RecoveryService
from codex_web.services.releases import ReleaseService
from codex_web.services.replicated_ownership import ReplicatedOwnershipService
from codex_web.services.upgrades import UpgradeService


class AutonomyControlCenterService:
    """Read-only M11 operator projection over canonical production-autonomy state."""

    def __init__(
        self,
        *,
        autonomy: AutonomyController,
        policy: AutonomyPolicyService,
        approvals: ApprovalRequestService,
        attention: AttentionService,
        incidents: IncidentService,
        releases: ReleaseService,
        recovery: RecoveryService,
        capacity: CapacityService,
        provider_capacity: ProviderCapacityService,
        upgrades: UpgradeService,
        workers: ExecutionWorkerService,
        audit: AutonomyAuditService,
        orchestration: OrchestrationInspectorService,
        coordination: StateStoreCoordinationBackend,
        ownership: ReplicatedOwnershipService,
    ) -> None:
        self.autonomy = autonomy
        self.policy = policy
        self.approvals = approvals
        self.attention = attention
        self.incidents = incidents
        self.releases = releases
        self.recovery = recovery
        self.capacity = capacity
        self.provider_capacity = provider_capacity
        self.upgrades = upgrades
        self.workers = workers
        self.audit = audit
        self.orchestration = orchestration
        self.coordination = coordination
        self.ownership = ownership

    @staticmethod
    def _value(value: Any) -> Any:
        return getattr(value, "value", value)

    def snapshot(self, actor: AuthenticationActor) -> dict[str, Any]:
        autonomy = self.autonomy.status()
        effective, role_ids = self.policy.effective(actor=actor)

        approvals = self.approvals.list(actor)
        pending_approvals = [
            item
            for item in approvals
            if item.status in {
                ApprovalRequestStatus.PENDING,
                ApprovalRequestStatus.PARTIALLY_APPROVED,
                ApprovalRequestStatus.APPROVED,
            }
        ]
        attention = self.attention.list(actor)
        active_attention = [
            item
            for item in attention
            if item.status not in {
                AttentionStatus.RESOLVED,
                AttentionStatus.EXPIRED,
                AttentionStatus.SUPERSEDED,
            }
        ]
        incidents = self.incidents.list(actor)
        active_incidents = [
            item
            for item in incidents
            if item.status != IncidentStatus.CLOSED
        ]
        releases = self.releases.list(actor)
        upgrades = self.upgrades.list(actor)
        active_upgrades = [
            item
            for item in upgrades
            if item.status not in {
                UpgradeStatus.COMPLETED,
                UpgradeStatus.ROLLED_BACK,
            }
        ]

        recovery = self.recovery.health(actor=actor)
        capacity = self.capacity.health(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        provider_capacity = self.provider_capacity.list(actor)
        capacity_waits = self.provider_capacity.list_waits(
            actor,
            include_terminal=False,
        )
        workers = self.workers.list_workers(actor)
        assignments = self.workers.list_assignments(actor)

        audit_integrity = self.audit.verify(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        audit_metrics = self.audit.metrics(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        checkpoints = self.audit.list_checkpoints(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            limit=10,
        )
        signals = self.audit.list_signals(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            active_only=True,
        )

        qualification_by_gate = {
            self._value(item.gate): item.model_dump(mode="json")
            for item in effective.qualifications
        }
        qualification = []
        for gate in effective.required_qualification_gates:
            gate_value = self._value(gate)
            configured = qualification_by_gate.get(gate_value)
            qualification.append(
                {
                    "gate": gate_value,
                    "configured": configured is not None,
                    "evidence": configured,
                }
            )

        blockers: list[dict[str, Any]] = []
        if not recovery.recovery_qualified:
            blockers.append(
                {
                    "domain": "recovery",
                    "severity": "high",
                    "reasons": list(recovery.blockers),
                }
            )
        if capacity.load_shed_mode:
            blockers.append(
                {
                    "domain": "capacity",
                    "severity": "high",
                    "reasons": ["load_shed_mode"],
                }
            )
        for item in active_upgrades:
            if item.preflight is not None and not item.preflight.satisfied:
                blockers.append(
                    {
                        "domain": "upgrade",
                        "severity": "high",
                        "object_id": item.id,
                        "reasons": list(item.preflight.blockers),
                    }
                )
        if self._value(audit_integrity.status) == "failed":
            blockers.append(
                {
                    "domain": "audit",
                    "severity": "critical",
                    "reasons": [audit_integrity.reason or "audit_integrity_failed"],
                }
            )
        for signal in signals:
            blockers.append(
                {
                    "domain": "safety_signal",
                    "severity": self._value(signal.severity),
                    "object_id": signal.id,
                    "reasons": [signal.reason],
                }
            )

        latest_release = releases[0] if releases else None
        release_summary = {
            "latest": latest_release.model_dump(mode="json")
            if latest_release is not None
            else None,
            "blocked": [
                item.model_dump(mode="json")
                for item in releases
                if item.status == ReleaseStatus.BLOCKED
            ][:20],
            "count": len(releases),
        }

        orchestration = self.orchestration.snapshot(
            limit=25,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )

        coordination = self.coordination.health().model_dump(mode="json")
        ownership = self.ownership.status()

        return {
            "autonomy": {
                "control": autonomy["control"],
                "effective_policy": effective.model_dump(mode="json"),
                "role_ids": list(role_ids),
                "qualification": qualification,
                "recent_cycles": autonomy["recent_cycles"][:25],
                "dead_letters": autonomy["dead_letters"][:25],
                "break_glass_grants": autonomy["break_glass_grants"][:25],
            },
            "approvals": {
                "pending": [
                    item.model_dump(mode="json")
                    for item in pending_approvals[:100]
                ],
                "count": len(pending_approvals),
            },
            "attention": {
                "active": [
                    item.model_dump(mode="json")
                    for item in active_attention[:100]
                ],
                "count": len(active_attention),
            },
            "incidents": {
                "active": [
                    item.model_dump(mode="json")
                    for item in active_incidents[:100]
                ],
                "count": len(active_incidents),
            },
            "releases": release_summary,
            "recovery": recovery.model_dump(mode="json"),
            "capacity": {
                "health": capacity.model_dump(mode="json"),
                "provider_records": [
                    item.model_dump(mode="json")
                    for item in provider_capacity
                ],
                "waits": [
                    item.model_dump(mode="json")
                    for item in capacity_waits[:100]
                ],
            },
            "upgrades": {
                "active": [
                    item.model_dump(mode="json")
                    for item in active_upgrades[:50]
                ],
                "count": len(active_upgrades),
            },
            "execution_plane": {
                "workers": [
                    item.model_dump(mode="json")
                    for item in workers[:100]
                ],
                "assignments": [
                    {
                        **item.model_dump(mode="json"),
                        "lease": (
                            {
                                **item.lease.model_dump(mode="json"),
                                "lease_token": "[redacted]",
                            }
                            if item.lease is not None
                            else None
                        ),
                    }
                    for item in assignments[:100]
                ],
            },
            "audit": {
                "integrity": audit_integrity.model_dump(mode="json"),
                "metrics": audit_metrics.model_dump(mode="json"),
                "checkpoints": [
                    item.model_dump(mode="json")
                    for item in checkpoints
                ],
                "active_signals": [
                    item.model_dump(mode="json")
                    for item in signals
                ],
            },
            "replication": {
                "coordination": coordination,
                "ownership": ownership,
            },
            "orchestration": {
                "event_count": orchestration["event_count"],
                "cycle_count": orchestration["cycle_count"],
                "timeline": orchestration["timeline"],
                "evaluations": orchestration["evaluations"],
            },
            "blockers": blockers,
        }

    def explain_action(
        self,
        intent_id: str,
        *,
        actor: AuthenticationActor,
    ) -> dict[str, Any]:
        return self.orchestration.explain_action(
            intent_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
