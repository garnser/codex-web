from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.action_intents import ActionIntentStatus
from codex_web.artifact_evidence import (
    Evidence,
    EvidenceLifecycle,
    EvidenceResult,
    EvidenceType,
    Verification,
    VerificationResult,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.incidents import (
    IncidentActionRequest,
    IncidentDetect,
    IncidentPostmortemCreate,
    IncidentResolve,
    IncidentSeverity,
    IncidentStatus,
    IncidentTriage,
)
from codex_web.resources import Resource, ResourceRisk, ResourceType
from codex_web.services.incidents import (
    IncidentResolutionError,
    IncidentService,
)
from codex_web.storage.incidents import IncidentStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Attention:
    def __init__(self):
        self.items = []
        self.resolved = []

    async def upsert(self, payload, *, actor_id):
        item = SimpleNamespace(id="attention-incident", payload=payload, actor_id=actor_id)
        self.items.append(item)
        return item

    async def resolve(self, item_id, *, actor, reason=None):
        self.resolved.append((item_id, actor.identity_id, reason))
        return SimpleNamespace(id=item_id)


class _Artifacts:
    def __init__(self):
        self.evidence = {}
        self.verifications = []

    def get_evidence(self, evidence_id, *, actor):
        del actor
        return self.evidence[evidence_id]

    def list_verifications(self, actor):
        del actor
        return list(self.verifications)


class _Actions:
    def __init__(self):
        self.created = []
        self.cancel = False

    def create(self, payload, *, actor):
        item = SimpleNamespace(
            id=f"intent-{len(self.created)+1}",
            status=(
                ActionIntentStatus.CANCELLED
                if self.cancel
                else ActionIntentStatus.PENDING
            ),
            last_error="denied" if self.cancel else None,
            payload=payload,
            actor=actor,
        )
        self.created.append(item)
        return item


class _Resources:
    def __init__(self, resource):
        self.resource = resource

    def get(self, resource_id, actor):
        del actor
        if resource_id != self.resource.id:
            raise KeyError(resource_id)
        return self.resource


class _Memory:
    def __init__(self):
        self.created = []

    def create(self, payload, *, actor):
        item = SimpleNamespace(
            id="knowledge-postmortem",
            payload=payload,
            actor=actor,
        )
        self.created.append(item)
        return item


class _Events:
    def __init__(self):
        self.events = []

    async def ingest(self, **kwargs):
        self.events.append(kwargs)
        return SimpleNamespace(inserted=True)


class IncidentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.actor = AuthenticationActor(
            identity_id="incident-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.resource = Resource(
            id="resource-prod",
            organization_id="org-a",
            workspace_id="ws-a",
            resource_type=ResourceType.SERVICE,
            name="api",
            risk=ResourceRisk.CRITICAL,
        )
        self.attention = _Attention()
        self.artifacts = _Artifacts()
        self.actions = _Actions()
        self.memory = _Memory()
        self.events = _Events()
        self.service = IncidentService(
            IncidentStore(
                SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
            ),
            attention=self.attention,
            artifacts=self.artifacts,
            action_intents=self.actions,
            resources=_Resources(self.resource),
            memory=self.memory,
            canonical_events=self.events,
            clock=lambda: 1000.0,
        )

    def tearDown(self):
        self.temp.cleanup()

    async def detect(self, severity=IncidentSeverity.SEV2):
        return await self.service.detect(
            IncidentDetect(
                title="API unavailable",
                severity=severity,
                source="health-check",
                source_event_id="event-alert",
                affected_resource_ids=(self.resource.id,),
                impact_summary="Requests are failing",
                owner_identity_ids=("incident-admin",),
                recipient_team_ids=("sre",),
            ),
            actor=self.actor,
        )

    async def test_duplicate_detection_correlates_into_one_canonical_incident(self):
        first = await self.detect()
        second = await self.detect()
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.occurrence_count, 2)
        self.assertEqual(len(self.service.list(self.actor)), 1)
        self.assertEqual(len(self.attention.items), 1)
        self.assertEqual(
            self.attention.items[0].payload.severity.value,
            "high",
        )
        self.assertEqual(
            self.attention.items[0].payload.dedupe_key,
            f"incident:{first.dedupe_key}",
        )

    async def test_containment_uses_action_intent_and_preserves_normal_authority_boundary(self):
        incident = await self.detect()
        incident = await self.service.triage(
            incident.id,
            IncidentTriage(
                commander_identity_id=self.actor.identity_id,
            ),
            actor=self.actor,
        )
        updated = await self.service.queue_action(
            incident.id,
            IncidentActionRequest(
                provider_binding_id="ops-provider",
                action_id="service.disable",
                resource_ids=(self.resource.id,),
                reason="Stop error amplification",
                rollback_required=True,
            ),
            actor=self.actor,
        )
        self.assertEqual(updated.status, IncidentStatus.ACTIVE)
        self.assertEqual(updated.action_intent_ids, ("intent-1",))
        self.assertEqual(len(self.actions.created), 1)
        payload = self.actions.created[0].payload
        self.assertEqual(
            payload.policy_decision.source,
            "canonical:incident-containment",
        )
        self.assertTrue(payload.verification_required)
        self.assertTrue(payload.rollback_required)
        self.assertEqual(
            payload.request.parameters["incident_id"],
            incident.id,
        )

    async def test_sev1_resolution_requires_independent_machine_readable_verification(self):
        incident = await self.detect(IncidentSeverity.SEV1)
        incident = await self.service.triage(
            incident.id,
            IncidentTriage(
                commander_identity_id=self.actor.identity_id,
            ),
            actor=self.actor,
        )
        incident = await self.service.transition(
            incident.id,
            IncidentStatus.ACTIVE,
            actor=self.actor,
            summary="Response active",
        )
        incident = await self.service.transition(
            incident.id,
            IncidentStatus.MONITORING,
            actor=self.actor,
            summary="Service restored, observing",
        )

        evidence = Evidence(
            id="evidence-restoration",
            organization_id="org-a",
            workspace_id="ws-a",
            evidence_type=EvidenceType.RUNTIME_RESULT,
            producer_identity_id="monitor",
            result=EvidenceResult.PASS,
            lifecycle=EvidenceLifecycle.VALID,
        )
        self.artifacts.evidence[evidence.id] = evidence

        with self.assertRaises(IncidentResolutionError):
            await self.service.resolve(
                incident.id,
                IncidentResolve(
                    evidence_ids=(evidence.id,),
                    summary="Healthy",
                ),
                actor=self.actor,
            )

        self.artifacts.verifications.append(
            Verification(
                id="verification-independent",
                organization_id="org-a",
                workspace_id="ws-a",
                evidence_ids=(evidence.id,),
                verifier_identity_id="independent-monitor",
                independent=True,
                method="synthetic probe",
                result=VerificationResult.VERIFIED,
            )
        )
        resolved = await self.service.resolve(
            incident.id,
            IncidentResolve(
                evidence_ids=(evidence.id,),
                summary="Synthetic probes and health checks pass",
            ),
            actor=self.actor,
        )
        self.assertEqual(resolved.status, IncidentStatus.RESOLVED)
        self.assertEqual(resolved.evidence_ids, (evidence.id,))
        self.assertEqual(len(self.attention.resolved), 1)

    async def test_postmortem_publishes_reusable_memory_and_followup_refs(self):
        incident = await self.detect(IncidentSeverity.SEV2)
        incident = await self.service.triage(
            incident.id,
            IncidentTriage(
                commander_identity_id=self.actor.identity_id,
            ),
            actor=self.actor,
        )
        incident = await self.service.transition(
            incident.id,
            IncidentStatus.ACTIVE,
            actor=self.actor,
            summary="Response active",
        )
        incident = await self.service.transition(
            incident.id,
            IncidentStatus.MONITORING,
            actor=self.actor,
            summary="Monitoring",
        )
        evidence = Evidence(
            id="evidence-restoration",
            organization_id="org-a",
            workspace_id="ws-a",
            evidence_type=EvidenceType.DEPLOYMENT_VERIFICATION,
            producer_identity_id="deploy-provider",
            result=EvidenceResult.PASS,
            lifecycle=EvidenceLifecycle.VALID,
        )
        self.artifacts.evidence[evidence.id] = evidence
        incident = await self.service.resolve(
            incident.id,
            IncidentResolve(
                evidence_ids=(evidence.id,),
                summary="Restored",
            ),
            actor=self.actor,
        )

        updated, postmortem = await self.service.create_postmortem(
            incident.id,
            IncidentPostmortemCreate(
                summary="Bad rollout exhausted connection pool",
                contributing_factors=("missing capacity guard",),
                corrective_work_item_refs=("work-123",),
                corrective_goal_ids=("goal-456",),
                evidence_ids=(evidence.id,),
                publish_to_memory=True,
            ),
            actor=self.actor,
        )
        self.assertEqual(updated.status, IncidentStatus.POSTMORTEM)
        self.assertEqual(updated.work_item_refs, ("work-123",))
        self.assertEqual(updated.goal_ids, ("goal-456",))
        self.assertEqual(postmortem.knowledge_id, "knowledge-postmortem")
        self.assertEqual(len(self.memory.created), 1)
        memory = self.memory.created[0].payload
        self.assertEqual(memory.object_type.value, "postmortem")
        refs = {(item.object_type, item.object_id) for item in memory.canonical_refs}
        self.assertIn(("incident", incident.id), refs)
        self.assertIn(("work_item", "work-123"), refs)
        self.assertIn(("goal", "goal-456"), refs)


if __name__ == "__main__":
    unittest.main()
