from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from codex_web.artifact_evidence import (
    ArtifactCreate,
    ArtifactLifecycle,
    ArtifactType,
    EvidenceCreate,
    EvidenceLifecycle,
    EvidenceRequirement,
    EvidenceResult,
    EvidenceType,
    VerificationCreate,
    VerificationResult,
    sha256_digest,
)
from codex_web.execution_contract_schema import execution_contract_for_work_item
from codex_web.execution_contracts import ROLE_CONTRACTS
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import WorkItemState
from codex_web.services.artifact_evidence import (
    ArtifactEvidenceConflictError,
    ArtifactEvidenceService,
)
from codex_web.services.identity import IdentityService
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _WorkItemHost:
    def __init__(self, state: WorkItemState) -> None:
        self.states = {state.ref: state}
        self.events: list[dict] = []

    def _load_work_item_states(self):
        return {key: value.model_copy(deep=True) for key, value in self.states.items()}

    def _save_work_item_states(self, states):
        self.states = {key: value.model_copy(deep=True) for key, value in states.items()}

    @staticmethod
    def _work_item_event(ref, event_type, **kwargs):
        return {"ref": ref, "event_type": event_type, **kwargs}

    def _append_work_item_event(self, event):
        self.events.append(event)


class ArtifactEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        sqlite = SQLiteStateStore(root / "state.sqlite3")
        identity = IdentityService(IdentityStateStore(sqlite))
        identity.bootstrap_local()
        self.producer = identity.local_trusted_actor()
        self.verifier = AuthenticationActor(
            identity_id="human-verifier",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.work_item = WorkItemState(
            ref="group/app#42",
            organization_id="local",
            workspace_id="default",
            project_id="home",
            project_path="group/app",
            current_owner="james",
            current_stage="implementation_active",
            artifact_state="branch",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        self.host = _WorkItemHost(self.work_item)
        self.service = ArtifactEvidenceService(
            ArtifactEvidenceStore(sqlite),
            work_item_host=self.host,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _artifact(self, **overrides):
        payload = {
            "work_item_ref": self.work_item.ref,
            "execution_id": "exec-1",
            "artifact_type": ArtifactType.COMMIT,
            "name": "implementation commit",
            "provider": "git",
            "source": "workspace",
            "revision": "abc123",
            "digest": sha256_digest(b"commit-content"),
        }
        payload.update(overrides)
        return self.service.create_artifact(
            ArtifactCreate(**payload),
            actor=self.producer,
        )

    def _evidence(self, artifact_id: str, **overrides):
        payload = {
            "work_item_ref": self.work_item.ref,
            "execution_id": "exec-1",
            "evidence_type": EvidenceType.TEST_RESULT,
            "artifact_ids": (artifact_id,),
            "provider": "pytest",
            "source": "ci",
            "result": EvidenceResult.PASS,
            "summary": "Focused and full tests passed.",
        }
        payload.update(overrides)
        return self.service.create_evidence(
            EvidenceCreate(**payload),
            actor=self.producer,
        )

    def test_digest_and_provenance_are_stable_and_queryable(self) -> None:
        artifact = self._artifact(
            external_id="commit-abc123",
            external_url="https://git.example/group/app/commit/abc123",
        )

        self.assertEqual(
            artifact.digest.value,
            sha256_digest(b"commit-content").value,
        )
        self.assertEqual(artifact.producer_identity_id, self.producer.identity_id)
        self.assertEqual(artifact.provider, "git")
        self.assertEqual(artifact.work_item_ref, self.work_item.ref)
        self.assertEqual(
            self.service.list_artifacts(
                self.producer,
                work_item_ref=self.work_item.ref,
            )[0].id,
            artifact.id,
        )

    def test_verification_independence_is_computed_not_asserted(self) -> None:
        artifact = self._artifact()
        evidence = self._evidence(artifact.id)

        self_verification = self.service.create_verification(
            VerificationCreate(
                evidence_ids=(evidence.id,),
                method="review",
                result=VerificationResult.VERIFIED,
            ),
            actor=self.producer,
        )
        independent = self.service.create_verification(
            VerificationCreate(
                evidence_ids=(evidence.id,),
                method="independent-review",
                result=VerificationResult.VERIFIED,
            ),
            actor=self.verifier,
        )

        self.assertFalse(self_verification.independent)
        self.assertTrue(independent.independent)
        self.assertNotEqual(
            independent.verifier_identity_id,
            evidence.producer_identity_id,
        )

    def test_independent_evidence_requirement_is_deterministic(self) -> None:
        artifact = self._artifact()
        evidence = self._evidence(artifact.id)
        requirement = EvidenceRequirement(
            id="tests-independent",
            evidence_type=EvidenceType.TEST_RESULT,
            artifact_type=ArtifactType.COMMIT,
            independent_verification=True,
            provider="pytest",
        )

        before = self.service.evaluate(
            self.work_item.ref,
            (requirement,),
            actor=self.producer,
            now=time.time(),
        )
        self.assertFalse(before.satisfied)

        verification = self.service.create_verification(
            VerificationCreate(
                work_item_ref=self.work_item.ref,
                artifact_ids=(artifact.id,),
                evidence_ids=(evidence.id,),
                method="validation-lane",
                result=VerificationResult.VERIFIED,
            ),
            actor=self.verifier,
        )
        after = self.service.evaluate(
            self.work_item.ref,
            (requirement,),
            actor=self.producer,
            now=time.time(),
        )

        self.assertTrue(after.satisfied)
        self.assertEqual(
            after.outcomes[0].matching_evidence_ids,
            (evidence.id,),
        )
        self.assertEqual(
            after.outcomes[0].verification_ids,
            (verification.id,),
        )

    def test_superseding_artifact_invalidates_dependent_evidence_and_verification(self) -> None:
        artifact = self._artifact()
        evidence = self._evidence(artifact.id)
        verification = self.service.create_verification(
            VerificationCreate(
                artifact_ids=(artifact.id,),
                evidence_ids=(evidence.id,),
                method="validation-lane",
                result=VerificationResult.VERIFIED,
            ),
            actor=self.verifier,
        )

        replacement = self._artifact(
            name="replacement commit",
            revision="def456",
            digest=sha256_digest(b"replacement"),
            supersedes_artifact_id=artifact.id,
        )

        artifacts = {item.id: item for item in self.service.list_artifacts(self.producer)}
        evidence_rows = {item.id: item for item in self.service.list_evidence(self.producer)}
        verification_rows = {
            item.id: item for item in self.service.list_verifications(self.producer)
        }
        self.assertEqual(artifacts[artifact.id].lifecycle, ArtifactLifecycle.SUPERSEDED)
        self.assertEqual(artifacts[artifact.id].superseded_by_id, replacement.id)
        self.assertEqual(evidence_rows[evidence.id].lifecycle, EvidenceLifecycle.INVALIDATED)
        self.assertEqual(
            verification_rows[verification.id].result,
            VerificationResult.INVALIDATED,
        )

        requirement = EvidenceRequirement(
            id="tests",
            evidence_type=EvidenceType.TEST_RESULT,
        )
        evaluation = self.service.evaluate(
            self.work_item.ref,
            (requirement,),
            actor=self.producer,
        )
        self.assertFalse(evaluation.satisfied)

    def test_evidence_cannot_be_created_for_inactive_artifact(self) -> None:
        artifact = self._artifact()
        self.service.invalidate_artifact(
            artifact.id,
            "artifact withdrawn",
            actor=self.producer,
        )

        with self.assertRaises(ArtifactEvidenceConflictError):
            self._evidence(artifact.id)

    def test_non_owner_cannot_invalidate_or_supersede_producer_artifact(self) -> None:
        artifact = self._artifact()
        outsider = AuthenticationActor(
            identity_id="human-outsider",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.MFA,
        )

        with self.assertRaises(Exception):
            self.service.invalidate_artifact(
                artifact.id,
                "not my artifact",
                actor=outsider,
            )

        with self.assertRaises(Exception):
            self.service.create_artifact(
                ArtifactCreate(
                    work_item_ref=self.work_item.ref,
                    artifact_type=ArtifactType.COMMIT,
                    name="unauthorised replacement",
                    supersedes_artifact_id=artifact.id,
                ),
                actor=outsider,
            )

    def test_evidence_cannot_claim_different_work_item_than_artifact(self) -> None:
        artifact = self._artifact()
        other = WorkItemState(
            ref="group/app#99",
            organization_id="local",
            workspace_id="default",
            project_id="home",
            project_path="group/app",
            current_owner="james",
            current_stage="implementation_active",
            artifact_state="branch",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        self.host.states[other.ref] = other

        with self.assertRaises(ArtifactEvidenceConflictError):
            self.service.create_evidence(
                EvidenceCreate(
                    work_item_ref=other.ref,
                    evidence_type=EvidenceType.TEST_RESULT,
                    artifact_ids=(artifact.id,),
                    result=EvidenceResult.PASS,
                ),
                actor=self.producer,
            )

    def test_retention_expiry_invalidates_dependent_verification(self) -> None:
        expires = time.time() + 5
        artifact = self._artifact(retention_expires_at=expires)
        evidence = self._evidence(
            artifact.id,
            retention_expires_at=expires,
        )
        verification = self.service.create_verification(
            VerificationCreate(
                evidence_ids=(evidence.id,),
                method="independent-review",
                result=VerificationResult.VERIFIED,
            ),
            actor=self.verifier,
        )

        expired = self.service.expire_retention(now=expires + 1)
        self.assertIn(artifact.id, expired["artifacts"])
        self.assertIn(evidence.id, expired["evidence"])

        artifacts = {item.id: item for item in self.service.list_artifacts(self.producer)}
        evidence_rows = {item.id: item for item in self.service.list_evidence(self.producer)}
        verification_rows = {
            item.id: item for item in self.service.list_verifications(self.producer)
        }
        self.assertEqual(artifacts[artifact.id].lifecycle, ArtifactLifecycle.EXPIRED)
        self.assertEqual(evidence_rows[evidence.id].lifecycle, EvidenceLifecycle.EXPIRED)
        self.assertEqual(
            verification_rows[verification.id].result,
            VerificationResult.INVALIDATED,
        )

    def test_work_item_requirements_are_canonical_and_flow_into_execution_contract(self) -> None:
        requirements = (
            EvidenceRequirement(
                id="ci",
                evidence_type=EvidenceType.CI_CHECK,
                accepted_results=(EvidenceResult.PASS,),
                independent_verification=False,
            ),
            EvidenceRequirement(
                id="review",
                evidence_type=EvidenceType.REVIEW,
                accepted_results=(EvidenceResult.PASS,),
                independent_verification=True,
            ),
        )
        saved = self.service.set_work_item_requirements(
            self.work_item.ref,
            requirements,
            actor=self.producer,
        )

        state = self.host.states[self.work_item.ref]
        contract = execution_contract_for_work_item(
            state,
            ROLE_CONTRACTS["james"],
        )

        self.assertEqual(saved, requirements)
        self.assertEqual(tuple(state.execution.evidence_requirements), requirements)
        self.assertEqual(contract.schema_version, "1.4")
        self.assertEqual(contract.required_evidence, requirements)
        self.assertEqual(
            [item.id for item in self.service.work_item_requirements(
                self.work_item.ref,
                actor=self.producer,
            )],
            ["ci", "review"],
        )
        self.assertEqual(self.host.events[-1]["event_type"], "evidence_requirements_updated")


if __name__ == "__main__":
    unittest.main()
