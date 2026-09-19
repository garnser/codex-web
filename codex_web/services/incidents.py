from __future__ import annotations

import time

from codex_web.action_intents import (
    ActionDecisionOutcome,
    ActionDecisionSnapshot,
    ActionIntentCreate,
    ActionIntentStatus,
)
from codex_web.action_providers import ActionRequest
from codex_web.artifact_evidence import (
    EvidenceLifecycle,
    EvidenceResult,
    VerificationResult,
)
from codex_web.attention import (
    AttentionItemCreate,
    AttentionSeverity,
    AttentionSource,
    EscalationPolicy,
)
from codex_web.canonical_events import CanonicalEventType
from codex_web.identity import AuthenticationActor
from codex_web.incidents import (
    IncidentActionRequest,
    IncidentDetect,
    IncidentEvidenceAttach,
    IncidentHandoff,
    IncidentPostmortem,
    IncidentPostmortemCreate,
    IncidentRecord,
    IncidentResolve,
    IncidentSeverity,
    IncidentStatus,
    IncidentTimelineEntry,
    IncidentTimelineKind,
    IncidentTriage,
)
from codex_web.organizational_memory import (
    KnowledgeCanonicalRef,
    KnowledgeCreate,
    KnowledgeObjectType,
    KnowledgeProvenance,
    KnowledgeSourceKind,
)
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.attention import AttentionService
from codex_web.services.canonical_events import CanonicalEventIngestionService
from codex_web.services.organizational_memory import OrganizationalMemoryService
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.incidents import IncidentNotFoundError, IncidentStore


class IncidentError(RuntimeError):
    pass


class IncidentConflictError(IncidentError):
    pass


class IncidentResolutionError(IncidentError):
    pass


_ALLOWED_TRANSITIONS = {
    IncidentStatus.DETECTED: {IncidentStatus.TRIAGED, IncidentStatus.ACTIVE},
    IncidentStatus.TRIAGED: {IncidentStatus.ACTIVE, IncidentStatus.CONTAINED},
    IncidentStatus.ACTIVE: {IncidentStatus.CONTAINED, IncidentStatus.MONITORING},
    IncidentStatus.CONTAINED: {IncidentStatus.MONITORING, IncidentStatus.ACTIVE},
    IncidentStatus.MONITORING: {IncidentStatus.RESOLVED, IncidentStatus.ACTIVE},
    IncidentStatus.RESOLVED: {IncidentStatus.POSTMORTEM, IncidentStatus.CLOSED},
    IncidentStatus.POSTMORTEM: {IncidentStatus.CLOSED},
    IncidentStatus.CLOSED: set(),
}


class IncidentService:
    def __init__(
        self,
        store: IncidentStore,
        *,
        attention: AttentionService,
        artifacts: ArtifactEvidenceService,
        action_intents: ActionIntentService,
        resources: ResourceCatalogService | None = None,
        memory: OrganizationalMemoryService | None = None,
        canonical_events: CanonicalEventIngestionService | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.attention = attention
        self.artifacts = artifacts
        self.action_intents = action_intents
        self.resources = resources
        self.memory = memory
        self.canonical_events = canonical_events
        self.clock = clock

    @staticmethod
    def _same_scope(item: IncidentRecord, actor: AuthenticationActor) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    def get(self, incident_id: str, *, actor: AuthenticationActor) -> IncidentRecord:
        try:
            item = self.store.get(incident_id)
        except IncidentNotFoundError as exc:
            raise IncidentError("incident not found") from exc
        if not self._same_scope(item, actor):
            raise IncidentError("incident not found")
        return item

    def list(self, actor: AuthenticationActor) -> list[IncidentRecord]:
        return self.store.list(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )

    def _update(self, incident_id: str, actor: AuthenticationActor, updater) -> IncidentRecord:
        result: list[IncidentRecord] = []

        def apply(state):
            current = state.incidents.get(incident_id)
            if current is None or not self._same_scope(current, actor):
                raise IncidentError("incident not found")
            updated = updater(current)
            state.incidents[incident_id] = updated
            result.append(updated)
            return state

        self.store.update(apply)
        return result[0]

    @staticmethod
    def _attention_severity(severity: IncidentSeverity) -> AttentionSeverity:
        return {
            IncidentSeverity.SEV1: AttentionSeverity.CRITICAL,
            IncidentSeverity.SEV2: AttentionSeverity.HIGH,
            IncidentSeverity.SEV3: AttentionSeverity.WARNING,
            IncidentSeverity.SEV4: AttentionSeverity.INFO,
        }[severity]

    async def _emit(self, incident: IncidentRecord, transition: str) -> None:
        if self.canonical_events is None:
            return
        await self.canonical_events.ingest(
            event_type=CanonicalEventType.INCIDENT,
            source="incident-domain",
            idempotency_key=(
                f"{incident.id}:{transition}:{incident.updated_at:.6f}:"
                f"{incident.occurrence_count}"
            ),
            payload={
                "incident_id": incident.id,
                "project_id": incident.project_id,
                "severity": incident.severity.value,
                "status": incident.status.value,
                "transition": transition,
                "affected_resource_ids": list(incident.affected_resource_ids),
                "action_intent_ids": list(incident.action_intent_ids),
                "evidence_ids": list(incident.evidence_ids),
            },
            tenant_id=incident.organization_id,
            workspace_id=incident.workspace_id,
        )

    async def detect(
        self,
        payload: IncidentDetect,
        *,
        actor: AuthenticationActor,
    ) -> IncidentRecord:
        dedupe_key = payload.fingerprint(
            actor.organization_id,
            actor.workspace_id,
        )
        existing = next(
            (
                item
                for item in self.store.list(
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                )
                if item.dedupe_key == dedupe_key
                and item.status != IncidentStatus.CLOSED
            ),
            None,
        )
        now = float(self.clock())
        if existing is not None:
            incident = self._update(
                existing.id,
                actor,
                lambda current: current.model_copy(
                    update={
                        "occurrence_count": current.occurrence_count + 1,
                        "severity": (
                            payload.severity
                            if list(IncidentSeverity).index(payload.severity)
                            < list(IncidentSeverity).index(current.severity)
                            else current.severity
                        ),
                        "timeline": (
                            *current.timeline,
                            IncidentTimelineEntry(
                                kind=IncidentTimelineKind.DETECTION,
                                summary=f"Correlated duplicate detection from {payload.source}",
                                actor_id=actor.identity_id,
                                resource_ids=payload.affected_resource_ids,
                                occurred_at=now,
                            ),
                        ),
                        "updated_at": now,
                    }
                ),
            )
            await self._emit(incident, "correlated")
            return incident

        if self.resources is not None:
            for resource_id in payload.affected_resource_ids:
                self.resources.get(resource_id, actor)

        incident = IncidentRecord(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=payload.project_id,
            title=payload.title,
            severity=payload.severity,
            dedupe_key=dedupe_key,
            detected_source=payload.source,
            detected_event_id=payload.source_event_id,
            owner_identity_ids=tuple(dict.fromkeys(payload.owner_identity_ids)),
            affected_resource_ids=tuple(
                dict.fromkeys(payload.affected_resource_ids)
            ),
            impact_summary=payload.impact_summary,
            customer_effect=payload.customer_effect,
            goal_ids=tuple(dict.fromkeys(payload.goal_ids)),
            decision_ids=tuple(dict.fromkeys(payload.decision_ids)),
            work_item_refs=tuple(dict.fromkeys(payload.work_item_refs)),
            release_ids=tuple(dict.fromkeys(payload.release_ids)),
            runbook_knowledge_ids=tuple(
                dict.fromkeys(payload.runbook_knowledge_ids)
            ),
            policy=payload.policy,
            timeline=(
                IncidentTimelineEntry(
                    kind=IncidentTimelineKind.DETECTION,
                    summary=f"Detected by {payload.source}",
                    actor_id=actor.identity_id,
                    resource_ids=payload.affected_resource_ids,
                    occurred_at=now,
                ),
            ),
            detected_at=now,
            updated_at=now,
        )

        def create(state):
            state.incidents[incident.id] = incident
            return state

        self.store.update(create)
        escalation_delay = {
            IncidentSeverity.SEV1: 300,
            IncidentSeverity.SEV2: 900,
            IncidentSeverity.SEV3: 3600,
            IncidentSeverity.SEV4: 0,
        }[incident.severity]
        attention = await self.attention.upsert(
            AttentionItemCreate(
                organization_id=incident.organization_id,
                workspace_id=incident.workspace_id,
                type="incident.command",
                severity=self._attention_severity(incident.severity),
                source=AttentionSource(
                    object_type="incident",
                    object_id=incident.id,
                    event_id=incident.detected_event_id,
                ),
                reason=(
                    f"{incident.severity.value.upper()} incident: {incident.title}"
                ),
                dedupe_key=f"incident:{incident.dedupe_key}",
                owner_identity_id=(
                    incident.owner_identity_ids[0]
                    if incident.owner_identity_ids
                    else None
                ),
                recipient_identity_ids=incident.owner_identity_ids,
                recipient_team_ids=payload.recipient_team_ids,
                deep_link=f"/incidents/{incident.id}",
                escalation=(
                    EscalationPolicy(
                        escalate_at=now + escalation_delay,
                        mandatory=incident.severity
                        in {IncidentSeverity.SEV1, IncidentSeverity.SEV2},
                        recipient_identity_ids=incident.owner_identity_ids,
                        recipient_team_ids=payload.recipient_team_ids,
                    )
                    if escalation_delay
                    else None
                ),
            ),
            actor_id=actor.identity_id,
        )
        incident = self._update(
            incident.id,
            actor,
            lambda current: current.model_copy(
                update={
                    "attention_item_id": attention.id,
                    "updated_at": float(self.clock()),
                }
            ),
        )
        await self._emit(incident, "detected")
        return incident

    async def triage(
        self,
        incident_id: str,
        payload: IncidentTriage,
        *,
        actor: AuthenticationActor,
    ) -> IncidentRecord:
        incident = self.get(incident_id, actor=actor)
        if incident.status not in {IncidentStatus.DETECTED, IncidentStatus.TRIAGED}:
            raise IncidentConflictError("incident is no longer in triage")
        now = float(self.clock())
        incident = self._update(
            incident_id,
            actor,
            lambda current: current.model_copy(
                update={
                    "severity": payload.severity or current.severity,
                    "impact_summary": (
                        payload.impact_summary
                        if payload.impact_summary is not None
                        else current.impact_summary
                    ),
                    "customer_effect": (
                        payload.customer_effect
                        if payload.customer_effect is not None
                        else current.customer_effect
                    ),
                    "commander_identity_id": (
                        payload.commander_identity_id
                        if payload.commander_identity_id is not None
                        else current.commander_identity_id
                    ),
                    "owner_identity_ids": (
                        tuple(dict.fromkeys(payload.owner_identity_ids))
                        if payload.owner_identity_ids is not None
                        else current.owner_identity_ids
                    ),
                    "status": IncidentStatus.TRIAGED,
                    "timeline": (
                        *current.timeline,
                        IncidentTimelineEntry(
                            kind=IncidentTimelineKind.TRIAGE,
                            summary="Incident triaged",
                            actor_id=actor.identity_id,
                            occurred_at=now,
                        ),
                    ),
                    "updated_at": now,
                }
            ),
        )
        await self._emit(incident, "triaged")
        return incident

    async def handoff(
        self,
        incident_id: str,
        payload: IncidentHandoff,
        *,
        actor: AuthenticationActor,
    ) -> IncidentRecord:
        incident = self.get(incident_id, actor=actor)
        if incident.status in {IncidentStatus.RESOLVED, IncidentStatus.CLOSED}:
            raise IncidentConflictError("resolved incident cannot change command")
        now = float(self.clock())
        previous = incident.commander_identity_id
        incident = self._update(
            incident_id,
            actor,
            lambda current: current.model_copy(
                update={
                    "commander_identity_id": payload.commander_identity_id,
                    "timeline": (
                        *current.timeline,
                        IncidentTimelineEntry(
                            kind=IncidentTimelineKind.HANDOFF,
                            summary=(
                                f"Incident command handed off from "
                                f"{previous or 'unassigned'} to "
                                f"{payload.commander_identity_id}: {payload.reason}"
                            ),
                            actor_id=actor.identity_id,
                            occurred_at=now,
                        ),
                    ),
                    "updated_at": now,
                }
            ),
        )
        await self._emit(incident, "command_handoff")
        return incident

    async def transition(
        self,
        incident_id: str,
        status: IncidentStatus,
        *,
        actor: AuthenticationActor,
        summary: str,
    ) -> IncidentRecord:
        incident = self.get(incident_id, actor=actor)
        if status not in _ALLOWED_TRANSITIONS[incident.status]:
            raise IncidentConflictError(
                f"illegal incident transition {incident.status.value}->{status.value}"
            )
        if status == IncidentStatus.RESOLVED:
            raise IncidentConflictError(
                "use resolve() so restoration Evidence is enforced"
            )
        now = float(self.clock())
        incident = self._update(
            incident_id,
            actor,
            lambda current: current.model_copy(
                update={
                    "status": status,
                    "timeline": (
                        *current.timeline,
                        IncidentTimelineEntry(
                            kind=IncidentTimelineKind.STATUS,
                            summary=summary,
                            actor_id=actor.identity_id,
                            occurred_at=now,
                        ),
                    ),
                    "updated_at": now,
                    "closed_at": now if status == IncidentStatus.CLOSED else current.closed_at,
                }
            ),
        )
        await self._emit(incident, f"status:{status.value}")
        return incident

    async def queue_action(
        self,
        incident_id: str,
        payload: IncidentActionRequest,
        *,
        actor: AuthenticationActor,
    ) -> IncidentRecord:
        incident = self.get(incident_id, actor=actor)
        if incident.status in {
            IncidentStatus.RESOLVED,
            IncidentStatus.POSTMORTEM,
            IncidentStatus.CLOSED,
        }:
            raise IncidentConflictError(
                "incident does not accept containment/recovery actions"
            )
        resource_ids = tuple(
            dict.fromkeys(
                payload.resource_ids or incident.affected_resource_ids
            )
        )
        if self.resources is not None:
            for resource_id in resource_ids:
                self.resources.get(resource_id, actor)

        intent = self.action_intents.create(
            ActionIntentCreate(
                binding_id=payload.provider_binding_id,
                request=ActionRequest(
                    action_id=payload.action_id,
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                    project_id=incident.project_id,
                    resource_ids=resource_ids,
                    parameters={
                        "incident_id": incident.id,
                        "incident_severity": incident.severity.value,
                        "incident_reason": payload.reason,
                        "recovery": payload.recovery,
                        **payload.parameters,
                    },
                    credential_ref=payload.credential_ref,
                    idempotency_key=(
                        f"incident:{incident.id}:"
                        f"{'recovery' if payload.recovery else 'containment'}:"
                        f"{payload.action_id}:{len(incident.action_intent_ids)}"
                    ),
                ),
                policy_decision=ActionDecisionSnapshot(
                    outcome=ActionDecisionOutcome.ALLOW,
                    source=(
                        "canonical:incident-recovery"
                        if payload.recovery
                        else "canonical:incident-containment"
                    ),
                    reason=(
                        "Incident action remains subject to canonical authority, "
                        "security and worker execution checks"
                    ),
                    evaluated_at=float(self.clock()),
                ),
                verification_required=True,
                rollback_required=not payload.recovery,
            ),
            actor=actor,
        )
        if intent.status == ActionIntentStatus.CANCELLED:
            raise IncidentConflictError(
                intent.last_error
                or "canonical authority/security denied incident action"
            )
        now = float(self.clock())
        kind = (
            IncidentTimelineKind.RECOVERY
            if payload.recovery
            else IncidentTimelineKind.CONTAINMENT
        )
        incident = self._update(
            incident_id,
            actor,
            lambda current: current.model_copy(
                update={
                    "action_intent_ids": (
                        *current.action_intent_ids,
                        intent.id,
                    ),
                    "status": (
                        IncidentStatus.ACTIVE
                        if current.status
                        in {IncidentStatus.DETECTED, IncidentStatus.TRIAGED}
                        else current.status
                    ),
                    "timeline": (
                        *current.timeline,
                        IncidentTimelineEntry(
                            kind=kind,
                            summary=payload.reason,
                            actor_id=actor.identity_id,
                            action_intent_id=intent.id,
                            resource_ids=resource_ids,
                            occurred_at=now,
                        ),
                    ),
                    "updated_at": now,
                }
            ),
        )
        await self._emit(
            incident,
            "recovery_queued" if payload.recovery else "containment_queued",
        )
        return incident

    def _valid_resolution_evidence(
        self,
        incident: IncidentRecord,
        evidence_ids: tuple[str, ...],
        actor: AuthenticationActor,
    ) -> tuple[str, ...]:
        valid: list[str] = []
        allowed_types = set(incident.policy.resolution_evidence_types)
        verifications = self.artifacts.list_verifications(actor)
        for evidence_id in dict.fromkeys(evidence_ids):
            evidence = self.artifacts.get_evidence(evidence_id, actor=actor)
            if (
                evidence.lifecycle != EvidenceLifecycle.VALID
                or evidence.result != EvidenceResult.PASS
                or evidence.evidence_type.value not in allowed_types
            ):
                continue
            if (
                incident.severity
                in incident.policy.require_independent_resolution_verification_for
            ):
                if not any(
                    item.independent
                    and item.result == VerificationResult.VERIFIED
                    and evidence.id in item.evidence_ids
                    for item in verifications
                ):
                    continue
            valid.append(evidence.id)
        return tuple(valid)

    async def attach_evidence(
        self,
        incident_id: str,
        payload: IncidentEvidenceAttach,
        *,
        actor: AuthenticationActor,
    ) -> IncidentRecord:
        incident = self.get(incident_id, actor=actor)
        ids = []
        for evidence_id in dict.fromkeys(payload.evidence_ids):
            evidence = self.artifacts.get_evidence(evidence_id, actor=actor)
            if evidence.lifecycle == EvidenceLifecycle.VALID:
                ids.append(evidence.id)
        now = float(self.clock())
        incident = self._update(
            incident_id,
            actor,
            lambda current: current.model_copy(
                update={
                    "evidence_ids": tuple(
                        dict.fromkeys((*current.evidence_ids, *ids))
                    ),
                    "timeline": (
                        *current.timeline,
                        IncidentTimelineEntry(
                            kind=IncidentTimelineKind.EVIDENCE,
                            summary=f"Attached {len(ids)} evidence record(s)",
                            actor_id=actor.identity_id,
                            evidence_ids=tuple(ids),
                            occurred_at=now,
                        ),
                    ),
                    "updated_at": now,
                }
            ),
        )
        await self._emit(incident, "evidence_attached")
        return incident

    async def resolve(
        self,
        incident_id: str,
        payload: IncidentResolve,
        *,
        actor: AuthenticationActor,
    ) -> IncidentRecord:
        incident = self.get(incident_id, actor=actor)
        if incident.status not in {
            IncidentStatus.ACTIVE,
            IncidentStatus.CONTAINED,
            IncidentStatus.MONITORING,
        }:
            raise IncidentResolutionError(
                "incident must be active/contained/monitoring before resolution"
            )
        valid = self._valid_resolution_evidence(
            incident,
            payload.evidence_ids,
            actor,
        )
        if not valid:
            raise IncidentResolutionError(
                "machine-readable restoration/verification Evidence is required"
            )
        now = float(self.clock())
        incident = self._update(
            incident_id,
            actor,
            lambda current: current.model_copy(
                update={
                    "status": IncidentStatus.RESOLVED,
                    "evidence_ids": tuple(
                        dict.fromkeys((*current.evidence_ids, *valid))
                    ),
                    "resolved_at": now,
                    "timeline": (
                        *current.timeline,
                        IncidentTimelineEntry(
                            kind=IncidentTimelineKind.STATUS,
                            summary=payload.summary,
                            actor_id=actor.identity_id,
                            evidence_ids=valid,
                            occurred_at=now,
                        ),
                    ),
                    "updated_at": now,
                }
            ),
        )
        if incident.attention_item_id:
            try:
                await self.attention.resolve(
                    incident.attention_item_id,
                    reason=f"Incident resolved: {payload.summary}",
                    actor=actor,
                )
            except Exception:
                pass
        await self._emit(incident, "resolved")
        return incident

    async def create_postmortem(
        self,
        incident_id: str,
        payload: IncidentPostmortemCreate,
        *,
        actor: AuthenticationActor,
    ) -> tuple[IncidentRecord, IncidentPostmortem]:
        incident = self.get(incident_id, actor=actor)
        if incident.status not in {
            IncidentStatus.RESOLVED,
            IncidentStatus.POSTMORTEM,
        }:
            raise IncidentConflictError(
                "postmortem requires a resolved incident"
            )
        for evidence_id in payload.evidence_ids:
            self.artifacts.get_evidence(evidence_id, actor=actor)
        now = float(self.clock())
        postmortem = IncidentPostmortem(
            incident_id=incident.id,
            summary=payload.summary,
            contributing_factors=tuple(
                dict.fromkeys(payload.contributing_factors)
            ),
            corrective_work_item_refs=tuple(
                dict.fromkeys(payload.corrective_work_item_refs)
            ),
            corrective_goal_ids=tuple(
                dict.fromkeys(payload.corrective_goal_ids)
            ),
            evidence_ids=tuple(dict.fromkeys(payload.evidence_ids)),
            created_by=actor.identity_id,
            created_at=now,
        )
        knowledge_id = None
        if payload.publish_to_memory:
            if self.memory is None:
                raise IncidentConflictError(
                    "organizational memory service is unavailable"
                )
            refs = [
                KnowledgeCanonicalRef(
                    object_type="incident",
                    object_id=incident.id,
                    relation="documents",
                ),
                *(
                    KnowledgeCanonicalRef(
                        object_type="work_item",
                        object_id=ref,
                        relation="corrective_action",
                    )
                    for ref in postmortem.corrective_work_item_refs
                ),
                *(
                    KnowledgeCanonicalRef(
                        object_type="goal",
                        object_id=goal_id,
                        relation="corrective_goal",
                    )
                    for goal_id in postmortem.corrective_goal_ids
                ),
            ]
            content = "\n".join(
                [
                    f"Incident: {incident.title}",
                    f"Severity: {incident.severity.value}",
                    f"Impact: {incident.impact_summary or 'unknown'}",
                    f"Resolution evidence: {', '.join(incident.evidence_ids)}",
                    "",
                    "Postmortem:",
                    postmortem.summary,
                    "",
                    "Contributing factors:",
                    *(
                        f"- {item}"
                        for item in postmortem.contributing_factors
                    ),
                    "",
                    "Corrective work:",
                    *(
                        f"- Work Item {item}"
                        for item in postmortem.corrective_work_item_refs
                    ),
                    *(
                        f"- Goal {item}"
                        for item in postmortem.corrective_goal_ids
                    ),
                ]
            )
            knowledge = self.memory.create(
                KnowledgeCreate(
                    logical_key=f"incident-postmortem/{incident.id}",
                    object_type=KnowledgeObjectType.POSTMORTEM,
                    title=f"Postmortem: {incident.title}",
                    summary=postmortem.summary,
                    content=content,
                    project_id=incident.project_id,
                    tags=(
                        "incident",
                        "postmortem",
                        incident.severity.value,
                    ),
                    canonical_refs=tuple(refs),
                    provenance=KnowledgeProvenance(
                        source_kind=KnowledgeSourceKind.INCIDENT,
                        source_ref=incident.id,
                        authored_by=actor.identity_id,
                        authored_at=now,
                        evidence_ids=tuple(
                            dict.fromkeys(
                                (*incident.evidence_ids, *postmortem.evidence_ids)
                            )
                        ),
                    ),
                ),
                actor=actor,
            )
            knowledge_id = knowledge.id
            postmortem = postmortem.model_copy(
                update={"knowledge_id": knowledge_id}
            )

        def apply(state):
            state.postmortems[postmortem.id] = postmortem
            current = state.incidents[incident.id]
            state.incidents[incident.id] = current.model_copy(
                update={
                    "status": IncidentStatus.POSTMORTEM,
                    "postmortem_id": postmortem.id,
                    "work_item_refs": tuple(
                        dict.fromkeys(
                            (
                                *current.work_item_refs,
                                *postmortem.corrective_work_item_refs,
                            )
                        )
                    ),
                    "goal_ids": tuple(
                        dict.fromkeys(
                            (*current.goal_ids, *postmortem.corrective_goal_ids)
                        )
                    ),
                    "timeline": (
                        *current.timeline,
                        IncidentTimelineEntry(
                            kind=IncidentTimelineKind.POSTMORTEM,
                            summary=postmortem.summary,
                            actor_id=actor.identity_id,
                            evidence_ids=postmortem.evidence_ids,
                            occurred_at=now,
                        ),
                    ),
                    "updated_at": now,
                }
            )
            return state

        state = self.store.update(apply)
        incident = state.incidents[incident.id]
        await self._emit(incident, "postmortem_created")
        return incident, postmortem

    def reasoning_budget(
        self,
        incident_id: str,
        *,
        actor: AuthenticationActor,
    ):
        incident = self.get(incident_id, actor=actor)
        return incident.policy.reasoning_budgets[incident.severity]
