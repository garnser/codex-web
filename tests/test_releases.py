from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.action_intents import ActionIntentStatus
from codex_web.artifact_evidence import (
    Artifact,
    ArtifactDigest,
    ArtifactLifecycle,
    ArtifactType,
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
from codex_web.releases import (
    PromotionCreate,
    PromotionQueueRequest,
    ReleaseArtifactSignature,
    ReleaseCreate,
    ReleaseEvidenceRequirement,
    ReleasePolicy,
)
from codex_web.resources import Resource, ResourceRisk, ResourceType
from codex_web.services.releases import (
    ReleaseGateError,
    ReleaseService,
)
from codex_web.storage.releases import ReleaseStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Artifacts:
    def __init__(self):
        self.artifacts = {}
        self.evidence = {}
        self.verifications = []
        self.created_evidence = []

    def get_artifact(self, artifact_id, *, actor):
        del actor
        return self.artifacts[artifact_id]

    def get_evidence(self, evidence_id, *, actor):
        del actor
        return self.evidence[evidence_id]

    def list_verifications(self, actor):
        del actor
        return list(self.verifications)

    def create_evidence(self, payload, *, actor):
        item = Evidence(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=payload.project_id,
            evidence_type=payload.evidence_type,
            artifact_ids=payload.artifact_ids,
            producer_identity_id=actor.identity_id,
            provider=payload.provider,
            source=payload.source,
            result=payload.result,
            summary=payload.summary,
            metadata=payload.metadata,
        )
        self.evidence[item.id] = item
        self.created_evidence.append(item)
        return item


class _Resources:
    def __init__(self, resource):
        self.resource = resource

    def get(self, resource_id, actor):
        del actor
        if resource_id != self.resource.id:
            raise KeyError(resource_id)
        return self.resource


class _Approvals:
    def __init__(self):
        self.created = []
        self.consumed = []
        self.status = "approved"

    async def create(self, payload, *, requester):
        item = SimpleNamespace(
            id="approval-release",
            status=SimpleNamespace(value="pending"),
            target=payload.target,
            payload=payload,
            requester=requester,
        )
        self.created.append(item)
        return item

    def get(self, request_id, *, actor):
        del actor
        assert request_id == "approval-release"
        from codex_web.approval_requests import ApprovalRequestStatus
        return SimpleNamespace(
            id=request_id,
            status=ApprovalRequestStatus(self.status),
        )

    async def consume(self, request_id, payload, *, actor):
        self.consumed.append((request_id, payload, actor))
        return SimpleNamespace(id=request_id, status="consumed")


class _ActionIntents:
    def __init__(self):
        self.created = []
        self.items = {}

    def create(self, payload, *, actor):
        item = SimpleNamespace(
            id=f"intent-{len(self.created) + 1}",
            status=ActionIntentStatus.PENDING,
            last_error=None,
            completed_at=None,
            payload=payload,
            actor=actor,
        )
        self.created.append(item)
        self.items[item.id] = item
        return item

    def get(self, intent_id, actor):
        del actor
        return self.items[intent_id]


class _Signer:
    def sign_digest(self, digest, *, credential_ref):
        return ReleaseArtifactSignature(
            algorithm="test-sha256",
            key_ref=credential_ref,
            signature=hashlib.sha256(
                f"{credential_ref}:{digest}".encode()
            ).hexdigest(),
            signed_digest=digest,
        )

    def verify_digest(self, digest, signature):
        return (
            signature.signed_digest == digest
            and signature.signature
            == hashlib.sha256(
                f"{signature.key_ref}:{digest}".encode()
            ).hexdigest()
        )


class ReleasePromotionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ReleaseStore(
            SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        )
        self.actor = AuthenticationActor(
            identity_id="release-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.artifacts = _Artifacts()
        self.build = Artifact(
            id="artifact-build",
            organization_id="org-a",
            workspace_id="ws-a",
            project_id="project-a",
            artifact_type=ArtifactType.BUILD,
            name="app.tar",
            producer_identity_id="builder",
            revision="commit-abc",
            digest=ArtifactDigest(value="a" * 64),
            lifecycle=ArtifactLifecycle.ACTIVE,
        )
        self.sbom = Artifact(
            id="artifact-sbom",
            organization_id="org-a",
            workspace_id="ws-a",
            project_id="project-a",
            artifact_type=ArtifactType.GENERATED_FILE,
            name="sbom.spdx.json",
            producer_identity_id="builder",
            digest=ArtifactDigest(value="b" * 64),
            lifecycle=ArtifactLifecycle.ACTIVE,
        )
        self.artifacts.artifacts = {
            self.build.id: self.build,
            self.sbom.id: self.sbom,
        }
        for evidence_id, evidence_type in (
            ("evidence-provenance", EvidenceType.ARTIFACT_VERIFICATION),
            ("evidence-tests", EvidenceType.TEST_RESULT),
            ("evidence-ci", EvidenceType.CI_CHECK),
            ("evidence-security", EvidenceType.SECURITY_SCAN),
        ):
            self.artifacts.evidence[evidence_id] = Evidence(
                id=evidence_id,
                organization_id="org-a",
                workspace_id="ws-a",
                project_id="project-a",
                evidence_type=evidence_type,
                artifact_ids=(self.build.id,),
                producer_identity_id="ci",
                provider="ci",
                result=EvidenceResult.PASS,
                lifecycle=EvidenceLifecycle.VALID,
            )
        self.environment = Resource(
            id="resource-prod",
            organization_id="org-a",
            workspace_id="ws-a",
            resource_type=ResourceType.ENVIRONMENT,
            name="production",
            risk=ResourceRisk.CRITICAL,
        )
        self.approvals = _Approvals()
        self.intents = _ActionIntents()
        self.service = ReleaseService(
            self.store,
            artifacts=self.artifacts,
            resources=_Resources(self.environment),
            approvals=self.approvals,
            action_intents=self.intents,
            signer=_Signer(),
            clock=lambda: 1000.0,
        )

    def tearDown(self):
        self.temp.cleanup()

    def create_release(self, *, rollback_release_id=None, require_signature=True):
        policy = ReleasePolicy(
            require_signature=require_signature,
            required_evidence=(
                ReleaseEvidenceRequirement(
                    id="tests",
                    evidence_type=EvidenceType.TEST_RESULT,
                ),
                ReleaseEvidenceRequirement(
                    id="ci",
                    evidence_type=EvidenceType.CI_CHECK,
                ),
                ReleaseEvidenceRequirement(
                    id="security",
                    evidence_type=EvidenceType.SECURITY_SCAN,
                ),
            ),
        )
        return self.service.create(
            ReleaseCreate(
                project_id="project-a",
                name="web",
                version="1.2.3",
                artifact_id=self.build.id,
                source_revision="commit-abc",
                build_id="build-77",
                sbom_artifact_id=self.sbom.id,
                provenance_evidence_id="evidence-provenance",
                signing_credential_ref="secret://release-signing-key",
                evidence_ids=(
                    "evidence-tests",
                    "evidence-ci",
                    "evidence-security",
                ),
                rollback_release_id=rollback_release_id,
                policy=policy,
            ),
            actor=self.actor,
        )

    def test_release_freezes_digest_and_generates_signature_evidence(self):
        release = self.create_release(require_signature=True)
        self.assertEqual(release.build.digest, f"sha256:{'a' * 64}")
        self.assertEqual(release.build.source_revision, "commit-abc")
        self.assertIsNotNone(release.build.signature)
        self.assertEqual(
            release.build.signing_credential_ref,
            "secret://release-signing-key",
        )
        self.assertEqual(len(self.artifacts.created_evidence), 1)
        self.assertEqual(
            self.artifacts.created_evidence[0].evidence_type,
            EvidenceType.ARTIFACT_VERIFICATION,
        )

    async def test_production_gate_blocks_without_known_good_rollback(self):
        release = self.create_release()
        release = self.service.add_promotion(
            release.id,
            PromotionCreate(
                environment_resource_id=self.environment.id,
                environment_name="production",
            ),
            actor=self.actor,
        )
        promotion = release.promotions[0]
        evaluation = self.service.evaluate_promotion(
            release.id,
            promotion.id,
            actor=self.actor,
        )
        self.assertFalse(evaluation.satisfied)
        self.assertIn(
            "known_good_rollback_release_required",
            evaluation.blockers,
        )

    async def test_approved_production_promotion_queues_exact_immutable_artifact(self):
        rollback = self.service.create(
            ReleaseCreate(
                project_id="project-a",
                name="web",
                version="1.2.2",
                artifact_id=self.build.id,
                source_revision="commit-abc",
                build_id="build-70",
                sbom_artifact_id=self.sbom.id,
                provenance_evidence_id="evidence-provenance",
                evidence_ids=(
                    "evidence-tests",
                    "evidence-ci",
                    "evidence-security",
                ),
                policy=ReleasePolicy(require_signature=False),
            ),
            actor=self.actor,
        )
        release = self.create_release(
            rollback_release_id=rollback.id,
            require_signature=True,
        )
        release = self.service.add_promotion(
            release.id,
            PromotionCreate(
                environment_resource_id=self.environment.id,
                environment_name="production",
            ),
            actor=self.actor,
        )
        promotion = release.promotions[0]

        release = await self.service.request_promotion_approval(
            release.id,
            promotion.id,
            actor=self.actor,
        )
        promotion = release.promotions[0]
        self.assertEqual(
            promotion.approval_request_id,
            "approval-release",
        )
        self.assertEqual(
            self.approvals.created[0].target.target_digest,
            release.build.digest,
        )

        release = await self.service.queue_promotion(
            release.id,
            promotion.id,
            PromotionQueueRequest(
                provider_binding_id="deploy-provider",
                deployment_action_id="deploy.release",
                credential_ref="secret://deployment",
            ),
            actor=self.actor,
        )
        queued = release.promotions[0]
        self.assertEqual(queued.status.value, "queued")
        self.assertEqual(len(self.approvals.consumed), 1)
        self.assertEqual(len(self.intents.created), 1)
        payload = self.intents.created[0].payload
        self.assertEqual(
            payload.request.parameters["artifact_digest"],
            release.build.digest,
        )
        self.assertEqual(
            payload.request.parameters["rollback_artifact_digest"],
            rollback.build.digest,
        )
        self.assertTrue(payload.verification_required)
        self.assertTrue(payload.rollback_required)

    async def test_policy_failure_prevents_approval_request(self):
        self.artifacts.evidence["evidence-security"] = self.artifacts.evidence[
            "evidence-security"
        ].model_copy(update={"result": EvidenceResult.FAIL})
        release = self.create_release()
        release = self.service.add_promotion(
            release.id,
            PromotionCreate(
                environment_resource_id=self.environment.id,
                environment_name="staging",
            ),
            actor=self.actor,
        )
        promotion = release.promotions[0]
        updated = await self.service.request_promotion_approval(
            release.id,
            promotion.id,
            actor=self.actor,
        )
        self.assertEqual(updated.promotions[0].status.value, "blocked")
        self.assertIn(
            "evidence_requirement:security",
            updated.promotions[0].blockers,
        )
        self.assertEqual(self.approvals.created, [])


if __name__ == "__main__":
    unittest.main()
