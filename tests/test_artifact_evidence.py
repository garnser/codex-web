from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from codex_web.artifact_content import (
    ArtifactContentIntegrityError,
    ArtifactContentScope,
    ArtifactContentState,
)
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
from codex_web.data_governance import (
    ContextFilterRequest,
    DataClassification,
    GovernanceAction,
    GovernanceActionRequest,
    GovernanceRequestStatus,
)
from codex_web.execution_contract_schema import execution_contract_for_work_item
from codex_web.execution_contract_seed import execution_role_catalog_seed_payload
from codex_web.execution_role_models import ExecutionRoleCatalogDefinition
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import WorkItemState
from codex_web.services.artifact_content import (
    ArtifactContentRegistry,
    ArtifactContentService,
)
from codex_web.services.artifact_evidence import (
    ArtifactEvidenceConflictError,
    ArtifactEvidenceService,
)
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.data_governance import GovernanceConflictError
from codex_web.services.identity import IdentityService
from codex_web.services.local_artifact_content import LocalArtifactContentStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.organizational_memory import KnowledgeCreate, KnowledgeInvalidate, KnowledgeProvenance
from codex_web.services.organizational_memory import OrganizationalMemoryService, KnowledgeValidationError
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.organizational_memory import OrganizationalMemoryStore
from codex_web.services.identity import AuthorizationError


ROLE_CONTRACTS = ExecutionRoleCatalogDefinition.model_validate(
    execution_role_catalog_seed_payload()
).role_map


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
        self.artifact_store = ArtifactEvidenceStore(sqlite)
        self.governance = DataGovernanceService(DataGovernanceStore(sqlite))
        self.content_registry = ArtifactContentRegistry()
        self.local_content = LocalArtifactContentStore(
            root / "artifact-content",
            backend_id="local",
        )
        self.archive_content = LocalArtifactContentStore(
            root / "archive-content",
            backend_id="archive",
        )
        self.content_registry.register(self.local_content)
        self.content_registry.register(self.archive_content)
        self.content_service = ArtifactContentService(self.content_registry)
        self.service = ArtifactEvidenceService(
            self.artifact_store,
            work_item_host=self.host,
            governance=self.governance,
            content=self.content_service,
            content_backend_resolver=lambda _actor, _project_id: "local",
        )
        self.governance.register_action_handler(
            "artifact",
            self.service.apply_governance_action,
        )
        self.governance.register_action_handler(
            "evidence",
            self.service.apply_governance_action,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _research(self, origin="human_observed", reviewed=False, source_classification=DataClassification.INTERNAL):
        if self.service.organizational_memory is None:
            state = self.artifact_store.store
            authority = install_authority_roles(DefinitionRegistryService(DefinitionRegistryStore(state)))
            self.memory = OrganizationalMemoryService(OrganizationalMemoryStore(state), self.governance, authority,
                artifact_evidence=self.service)
            self.service.organizational_memory = self.memory
        artifact = self._artifact(artifact_type=ArtifactType.REPORT, project_id="home", classification=source_classification)
        proof = self.service.create_evidence(EvidenceCreate(
            project_id="home", work_item_ref=self.work_item.ref, evidence_type="runtime_result",
            result="pass", artifact_ids=(artifact.id,), summary="Fixture research source",
        ), actor=self.producer)
        source = self.memory.create(KnowledgeCreate(
            logical_key="research/"+proof.id, object_type="other", project_id="home",
            title="Research fixture", summary="Research fixture", content="Fixture content",
            provenance=KnowledgeProvenance(source_kind="evidence", source_ref=proof.id,
                origin=origin, review_status="reviewed" if reviewed else "unreviewed",
                evidence_ids=(proof.id,)),
        ), actor=self.producer)
        return source, proof

    def _research_candidate(self, *sources):
        return self.service.create_evidence(EvidenceCreate(
            project_id="home", work_item_ref=self.work_item.ref, evidence_type="review", result="pass",
            knowledge_ids=tuple(source.id for source in sources), summary="Fixture research report",
        ), actor=self.producer)

    def _observed_gate(self):
        return self.service.evaluate(self.work_item.ref, (EvidenceRequirement(
            id="real-user-validation", evidence_type="review", observed_research=True,
        ),), actor=self.producer)

    def test_observed_label_and_review_metadata_cannot_satisfy_research_gate(self):
        source, proof = self._research(reviewed=True)
        candidate = self._research_candidate(source)
        self.assertEqual(candidate.research_origin.value, "human_observed")
        self.assertFalse(self._observed_gate().satisfied)
        self.service.create_verification(VerificationCreate(
            evidence_ids=(proof.id,), method="ordinary-review", result="verified",
        ), actor=self.verifier)
        self.assertFalse(self._observed_gate().satisfied)

    def test_independent_human_observed_source_proof_satisfies_gate(self):
        source, proof = self._research()
        candidate = self._research_candidate(source)
        verification = self.service.create_verification(VerificationCreate(
            evidence_ids=(proof.id,), method="observed-user-research", result="verified",
        ), actor=self.verifier)
        gate = self._observed_gate()
        self.assertTrue(gate.satisfied)
        self.assertEqual(gate.outcomes[0].matching_evidence_ids, (candidate.id,))
        self.assertIn(verification.id, gate.outcomes[0].verification_ids)
        self.memory.invalidate(source.id, KnowledgeInvalidate(reason="Source withdrawn"), actor=self.producer)
        self.assertFalse(self._observed_gate().satisfied)

    def test_synthetic_only_source_remains_ineligible_after_human_review(self):
        source, proof = self._research("ai_inference", reviewed=True)
        candidate = self._research_candidate(source)
        self.service.create_verification(VerificationCreate(
            evidence_ids=(proof.id,), method="observed-user-research", result="verified",
        ), actor=self.verifier)
        self.assertEqual(candidate.research_origin.value, "ai_inference")
        self.assertFalse(self._observed_gate().satisfied)

    def test_observed_research_requires_human_verifier_and_independence(self):
        source, proof = self._research()
        self._research_candidate(source)
        request = VerificationCreate(evidence_ids=(proof.id,), method="observed-user-research", result="verified")
        with self.assertRaises(AuthorizationError):
            self.service.create_verification(request, actor=self.verifier.model_copy(update={"principal_kind": PrincipalKind.SERVICE}))
        verification = self.service.create_verification(request, actor=self.producer)
        self.assertFalse(verification.independent)
        self.assertFalse(self._observed_gate().satisfied)

    def test_mixed_research_keeps_synthetic_origin_and_uses_observed_source_proof(self):
        observed, proof = self._research()
        synthetic, _ = self._research("ai_inference")
        candidate = self._research_candidate(observed, synthetic)
        self.service.create_verification(VerificationCreate(
            evidence_ids=(proof.id,), method="observed-user-research", result="verified",
        ), actor=self.verifier)
        self.assertEqual(candidate.research_origin.value, "mixed")
        self.assertTrue(self._observed_gate().satisfied)

    def test_research_gate_refuses_invalidated_observation_artifact(self):
        source, proof = self._research()
        self._research_candidate(source)
        self.service.create_verification(VerificationCreate(
            evidence_ids=(proof.id,), method="observed-user-research", result="verified",
        ), actor=self.verifier)
        self.assertTrue(self._observed_gate().satisfied)
        self.service.invalidate_artifact(proof.artifact_ids[0], "Observation withdrawn", actor=self.producer)
        self.assertFalse(self._observed_gate().satisfied)

    def test_legacy_evidence_defaults_unknown_origin_and_engineering_gate_unchanged(self):
        artifact = self._artifact()
        proof = self.service.create_evidence(EvidenceCreate(
            work_item_ref=self.work_item.ref, evidence_type="test_result", result="pass",
            artifact_ids=(artifact.id,),
        ), actor=self.producer)
        raw = self.artifact_store.store.get(self.artifact_store.namespace)
        raw["schema_version"] = "1.1"
        for record in (*raw["artifacts"], *raw["evidence"]):
            record.pop("research_origin"); record.pop("knowledge_ids")
        self.artifact_store.store.put(self.artifact_store.namespace, raw)
        restored = self.service.get_evidence(proof.id, actor=self.producer)
        self.assertEqual(restored.research_origin.value, "unknown")
        self.assertEqual(self.artifact_store.load().schema_version, "1.4")
        self.assertTrue(self.service.evaluate(self.work_item.ref, (
            EvidenceRequirement(id="engineering", evidence_type="test_result"),
        ), actor=self.producer).satisfied)

    def test_research_source_project_mismatch_and_forged_origin_are_rejected(self):
        source, _ = self._research()
        with self.assertRaises(ArtifactEvidenceConflictError):
            self.service.create_evidence(EvidenceCreate(
                project_id="other-project", evidence_type="review", result="pass", knowledge_ids=(source.id,),
            ), actor=self.producer)
        with self.assertRaises(ValueError):
            EvidenceCreate(evidence_type="review", result="pass", research_origin="human_observed")

    def test_legacy_method_string_cannot_manufacture_human_verification(self):
        source, proof = self._research()
        self._research_candidate(source)
        verification = self.service.create_verification(VerificationCreate(
            evidence_ids=(proof.id,), method="observed-user-research", result="verified",
        ), actor=self.verifier)
        self.assertTrue(self._observed_gate().satisfied)
        raw = self.artifact_store.store.get(self.artifact_store.namespace)
        raw["schema_version"] = "1.1"
        for row in raw["verifications"]:
            row.pop("verifier_principal_kind", None)
        self.artifact_store.store.put(self.artifact_store.namespace, raw)
        restored = next(row for row in self.artifact_store.load().verifications if row.id == verification.id)
        self.assertIsNone(restored.verifier_principal_kind)
        self.assertFalse(self._observed_gate().satisfied)

    def test_source_evidence_governance_is_inherited_without_caller_governance_ids(self):
        source, proof = self._research(source_classification=DataClassification.CONFIDENTIAL)
        self.assertIn(proof.governance_record_id, source.provenance.source_governance_record_ids)
        self.assertEqual(source.classification, DataClassification.CONFIDENTIAL)
        candidate = self._research_candidate(source)
        governed = self.governance.get_record(candidate.governance_record_id, actor=self.producer)
        self.assertEqual(governed.classification, DataClassification.CONFIDENTIAL)
        request = self.governance.request_action(GovernanceActionRequest(
            record_id=proof.governance_record_id, action=GovernanceAction.REDACT,
            reason="Source research removed",
        ), actor=self.producer)
        self.governance.execute_request(request.id, actor=self.producer)
        self.assertEqual(self.memory.store.get(source.id).lifecycle.value, "redacted")
        self.assertEqual(self.service.get_evidence(candidate.id, actor=self.producer).lifecycle, EvidenceLifecycle.INVALIDATED)

    def test_foreign_source_evidence_cannot_supply_research_provenance(self):
        self._research()
        other = self.producer.model_copy(update={"organization_id":"other", "workspace_id":"other"})
        artifact = self.service.create_artifact(ArtifactCreate(
            artifact_type="report", name="Foreign fixture", digest=sha256_digest(b"foreign fixture"),
        ), actor=other)
        proof = self.service.create_evidence(EvidenceCreate(
            evidence_type="runtime_result", result="pass", artifact_ids=(artifact.id,),
        ), actor=other)
        with self.assertRaises(KnowledgeValidationError):
            self.memory.create(KnowledgeCreate(
                logical_key="foreign-research", object_type="other", project_id="home",
                title="Fixture", summary="Fixture", content="Fixture",
                provenance=KnowledgeProvenance(source_kind="evidence", source_ref=proof.id,
                    origin="human_observed", evidence_ids=(proof.id,)),
            ), actor=self.producer)

    def test_derived_legal_hold_blocks_source_redaction_before_payload_mutation(self):
        source, proof = self._research()
        candidate = self._research_candidate(source)
        self.governance.set_legal_hold(candidate.governance_record_id, "Preserve research evidence", actor=self.producer)
        request = self.governance.request_action(GovernanceActionRequest(
            record_id=proof.governance_record_id, action=GovernanceAction.REDACT,
            reason="Requested source redaction",
        ), actor=self.producer)
        with self.assertRaises(GovernanceConflictError):
            self.governance.execute_request(request.id, actor=self.producer)
        self.assertEqual(self.service.get_evidence(proof.id, actor=self.producer).lifecycle, EvidenceLifecycle.VALID)
        self.assertEqual(self.memory.store.get(source.id).content, "Fixture content")
        self.assertEqual(self.service.get_evidence(candidate.id, actor=self.producer).lifecycle, EvidenceLifecycle.VALID)

    def test_exported_report_metadata_preserves_source_level_origins_without_transcript_fields(self):
        observed, _ = self._research()
        simulated, _ = self._research("ai_inference")
        report = self._artifact(artifact_type="report", project_id="home", knowledge_ids=(observed.id, simulated.id))
        exported = report.model_dump(mode="json")
        self.assertEqual(exported["research_origin"], "mixed")
        self.assertEqual({row["knowledge_id"]: row["origin"] for row in exported["research_sources"]}, {
            observed.id: "human_observed", simulated.id: "ai_inference",
        })
        self.assertTrue(all(row["source_ref"]=="memory:"+row["knowledge_id"] for row in exported["research_sources"]))
        self.assertTrue(all("content" not in row and "authored_by" not in row for row in exported["research_sources"]))

    def test_observation_artifact_producer_cannot_self_verify_through_another_evidence_producer(self):
        _, original = self._research()
        proof = self.service.create_evidence(EvidenceCreate(
            project_id="home", work_item_ref=self.work_item.ref, evidence_type="runtime_result",
            result="pass", artifact_ids=original.artifact_ids,
        ), actor=self.verifier)
        source = self.memory.create(KnowledgeCreate(
            logical_key="transitive-producer", object_type="other", project_id="home",
            title="Fixture", summary="Fixture", content="Fixture",
            provenance=KnowledgeProvenance(source_kind="evidence", source_ref=proof.id,
                origin="human_observed", evidence_ids=(proof.id,)),
        ), actor=self.producer)
        self._research_candidate(source)
        verification = self.service.create_verification(VerificationCreate(
            evidence_ids=(proof.id,), method="observed-user-research", result="verified",
        ), actor=self.producer)
        self.assertTrue(verification.independent)  # Ordinary direct-evidence semantics.
        self.assertFalse(self._observed_gate().satisfied)  # Research checks source-artifact producers too.

    def test_withdrawn_source_proof_removes_research_from_normal_memory_context(self):
        source, proof = self._research()
        self.service.invalidate_evidence(proof.id, "Capture invalidated", actor=self.producer)
        self.assertEqual(self.memory.freshness(source).value, "invalid")
        from codex_web.organizational_memory import KnowledgeQuery
        self.assertEqual(self.memory.search(KnowledgeQuery(logical_keys=(source.logical_key,)), actor=self.producer).items, ())

    def test_negative_observed_outcome_can_still_supply_verified_research_source(self):
        _, original = self._research()
        proof = self.service.create_evidence(EvidenceCreate(
            project_id="home", work_item_ref=self.work_item.ref, evidence_type="runtime_result",
            result="fail", artifact_ids=original.artifact_ids, summary="Observed user could not complete the task",
        ), actor=self.producer)
        source = self.memory.create(KnowledgeCreate(
            logical_key="negative-observation", object_type="other", project_id="home",
            title="Negative outcome", summary="Negative outcome", content="Fixture negative observation",
            provenance=KnowledgeProvenance(source_kind="evidence", source_ref=proof.id,
                origin="human_observed", evidence_ids=(proof.id,)),
        ), actor=self.producer)
        self._research_candidate(source)
        self.service.create_verification(VerificationCreate(
            evidence_ids=(proof.id,), method="observed-user-research", result="verified",
        ), actor=self.verifier)
        self.assertTrue(self._observed_gate().satisfied)



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

    def test_evidence_can_carry_repository_resource_attribution(self) -> None:
        artifact = self._artifact()
        evidence = self.service.create_evidence(
            EvidenceCreate(
                work_item_ref=self.work_item.ref,
                execution_id="exec-repository-test",
                resource_ids=("repo-app", "repo-app"),
                evidence_type=EvidenceType.TEST_RESULT,
                artifact_ids=(artifact.id,),
                provider="pytest",
                source="worker",
                result=EvidenceResult.PASS,
                summary="Repository-scoped tests passed.",
            ),
            actor=self.producer,
        )

        self.assertEqual(evidence.resource_ids, ("repo-app",))
        persisted = {
            item.id: item
            for item in self.service.list_evidence(
                self.producer,
                work_item_ref=self.work_item.ref,
            )
        }
        self.assertEqual(
            persisted[evidence.id].resource_ids,
            ("repo-app",),
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

    def test_artifact_and_evidence_governance_propagates_sensitivity_and_retention(self) -> None:
        artifact = self._artifact(
            classification=DataClassification.CONFIDENTIAL,
            retention_expires_at=100.0,
        )
        evidence = self._evidence(
            artifact.id,
            classification=DataClassification.INTERNAL,
            retention_expires_at=200.0,
        )

        self.assertIsNotNone(artifact.governance_record_id)
        self.assertIsNotNone(evidence.governance_record_id)
        artifact_governance = self.governance.get_record(
            artifact.governance_record_id,
            self.producer,
        )
        evidence_governance = self.governance.get_record(
            evidence.governance_record_id,
            self.producer,
        )
        self.assertEqual(
            artifact_governance.classification,
            DataClassification.CONFIDENTIAL,
        )
        self.assertEqual(
            evidence_governance.classification,
            DataClassification.CONFIDENTIAL,
        )
        self.assertEqual(evidence_governance.retention_expires_at, 100.0)
        self.assertEqual(evidence.retention_expires_at, 100.0)

        filtered = self.governance.filter_context(
            ContextFilterRequest(
                record_ids=(evidence.governance_record_id,),
                max_classification=DataClassification.INTERNAL,
            ),
            actor=self.producer,
        )
        self.assertEqual(filtered.allowed_record_ids, ())
        self.assertEqual(
            filtered.denied_record_ids,
            (evidence.governance_record_id,),
        )

    def test_governance_redaction_scrubs_evidence_and_invalidates_verification(self) -> None:
        artifact = self._artifact()
        evidence = self._evidence(
            artifact.id,
            external_id="ci-123",
            deep_link="https://ci.example/jobs/123",
            metadata={"job": "123"},
        )
        verification = self.service.create_verification(
            VerificationCreate(
                evidence_ids=(evidence.id,),
                method="review",
                result=VerificationResult.VERIFIED,
            ),
            actor=self.verifier,
        )

        action = self.governance.request_action(
            GovernanceActionRequest(
                record_id=evidence.governance_record_id,
                action=GovernanceAction.REDACT,
                reason="privacy request",
            ),
            actor=self.producer,
        )
        completed = self.governance.execute_request(action.id, actor=self.producer)
        self.assertEqual(completed.status, GovernanceRequestStatus.COMPLETED)

        redacted = {
            item.id: item for item in self.service.list_evidence(self.producer)
        }[evidence.id]
        self.assertIsNone(redacted.summary)
        self.assertIsNone(redacted.provider)
        self.assertIsNone(redacted.source)
        self.assertIsNone(redacted.external_id)
        self.assertIsNone(redacted.deep_link)
        self.assertEqual(redacted.metadata, {})
        self.assertEqual(redacted.lifecycle, EvidenceLifecycle.INVALIDATED)

        verification_rows = {
            item.id: item for item in self.service.list_verifications(self.producer)
        }
        self.assertEqual(
            verification_rows[verification.id].result,
            VerificationResult.INVALIDATED,
        )

    def test_managed_content_attaches_reads_and_migrates_without_changing_artifact_id(self) -> None:
        payload = b"managed-report-content"
        artifact = self._artifact(
            artifact_type=ArtifactType.REPORT,
            digest=sha256_digest(payload),
        )

        attached = self.service.attach_artifact_content(
            artifact.id,
            (payload[:6], payload[6:]),
            actor=self.producer,
            media_type="text/plain",
        )
        pointer, stream = self.service.open_artifact_content(
            artifact.id,
            actor=self.producer,
        )

        self.assertEqual(attached.id, artifact.id)
        self.assertEqual(pointer.backend_id, "local")
        self.assertEqual(b"".join(stream), payload)
        self.assertEqual(pointer.size_bytes, len(payload))

        migrated = self.service.migrate_artifact_content(
            artifact.id,
            "archive",
            actor=self.producer,
        )
        self.assertEqual(migrated.id, artifact.id)
        self.assertEqual(migrated.content.backend_id, "archive")
        self.assertEqual(migrated.digest, artifact.digest)
        old_head = self.local_content.head(
            ArtifactContentScope(
                organization_id=self.producer.organization_id,
                workspace_id=self.producer.workspace_id,
            ),
            pointer.locator,
        )
        self.assertTrue(old_head.tombstoned)

    def test_corrupted_managed_content_cannot_satisfy_evidence_gate(self) -> None:
        payload = b"verified-test-output"
        artifact = self._artifact(
            artifact_type=ArtifactType.REPORT,
            digest=sha256_digest(payload),
        )
        attached = self.service.attach_artifact_content(
            artifact.id,
            (payload,),
            actor=self.producer,
            media_type="text/plain",
        )
        evidence = self._evidence(artifact.id)
        requirement = EvidenceRequirement(
            id="content-backed-test",
            evidence_type=EvidenceType.TEST_RESULT,
            artifact_type=ArtifactType.REPORT,
        )

        before = self.service.evaluate(
            self.work_item.ref,
            (requirement,),
            actor=self.producer,
        )
        self.assertTrue(before.satisfied)

        path = self.local_content._resolve(
            ArtifactContentScope(
                organization_id=self.producer.organization_id,
                workspace_id=self.producer.workspace_id,
            ),
            attached.content.locator,
        )
        path.write_bytes(b"corrupted")

        with self.assertRaises(ArtifactContentIntegrityError):
            self.service.verify_artifact_content(
                artifact.id,
                actor=self.producer,
            )
        after = self.service.evaluate(
            self.work_item.ref,
            (requirement,),
            actor=self.producer,
        )
        self.assertFalse(after.satisfied)
        self.assertEqual(after.outcomes[0].matching_evidence_ids, ())
        self.assertEqual(evidence.lifecycle, EvidenceLifecycle.VALID)

    def test_retention_expiry_tombstones_managed_content(self) -> None:
        payload = b"retained-content"
        expires = time.time() + 5
        artifact = self._artifact(
            artifact_type=ArtifactType.REPORT,
            digest=sha256_digest(payload),
            retention_expires_at=expires,
        )
        attached = self.service.attach_artifact_content(
            artifact.id,
            (payload,),
            actor=self.producer,
        )

        expired = self.service.expire_retention(now=expires + 1)
        stored = {
            item.id: item
            for item in self.service.list_artifacts(self.producer)
        }[artifact.id]

        self.assertIn(artifact.id, expired["artifacts"])
        self.assertEqual(stored.lifecycle, ArtifactLifecycle.EXPIRED)
        self.assertEqual(stored.content.state, ArtifactContentState.TOMBSTONED)
        head = self.local_content.head(
            ArtifactContentScope(
                organization_id=self.producer.organization_id,
                workspace_id=self.producer.workspace_id,
            ),
            attached.content.locator,
        )
        self.assertTrue(head.tombstoned)

    def test_governance_delete_tombstones_managed_content_before_receipt(self) -> None:
        payload = b"sensitive-content"
        artifact = self._artifact(
            artifact_type=ArtifactType.REPORT,
            digest=sha256_digest(payload),
        )
        attached = self.service.attach_artifact_content(
            artifact.id,
            (payload,),
            actor=self.producer,
        )
        action = self.governance.request_action(
            GovernanceActionRequest(
                record_id=artifact.governance_record_id,
                action=GovernanceAction.DELETE,
                reason="retention deletion",
            ),
            actor=self.producer,
        )

        completed = self.governance.execute_request(action.id, actor=self.producer)
        stored = {
            item.id: item
            for item in self.service.list_artifacts(self.producer)
        }[artifact.id]

        self.assertEqual(completed.status, GovernanceRequestStatus.COMPLETED)
        self.assertEqual(stored.content.state, ArtifactContentState.TOMBSTONED)
        self.assertIsNone(stored.digest)
        self.assertTrue(
            self.local_content.head(
                ArtifactContentScope(
                    organization_id=self.producer.organization_id,
                    workspace_id=self.producer.workspace_id,
                ),
                attached.content.locator,
            ).tombstoned
        )

    def test_legacy_artifact_and_evidence_can_be_backfilled_into_governance(self) -> None:
        legacy_service = ArtifactEvidenceService(
            self.artifact_store,
            work_item_host=self.host,
        )
        artifact = legacy_service.create_artifact(
            ArtifactCreate(
                work_item_ref=self.work_item.ref,
                artifact_type=ArtifactType.REPORT,
                name="legacy report",
                retention_expires_at=100.0,
            ),
            actor=self.producer,
        )
        evidence = legacy_service.create_evidence(
            EvidenceCreate(
                work_item_ref=self.work_item.ref,
                evidence_type=EvidenceType.REVIEW,
                artifact_ids=(artifact.id,),
                result=EvidenceResult.PASS,
                summary="legacy review",
                retention_expires_at=200.0,
            ),
            actor=self.producer,
        )
        self.assertIsNone(artifact.governance_record_id)
        self.assertIsNone(evidence.governance_record_id)

        result = self.service.sync_governance_records(actor=self.producer)
        self.assertEqual(result, {"artifacts": 1, "evidence": 1})

        artifacts = {item.id: item for item in self.service.list_artifacts(self.producer)}
        evidence_rows = {item.id: item for item in self.service.list_evidence(self.producer)}
        self.assertIsNotNone(artifacts[artifact.id].governance_record_id)
        self.assertIsNotNone(evidence_rows[evidence.id].governance_record_id)
        evidence_governance = self.governance.get_record(
            evidence_rows[evidence.id].governance_record_id,
            self.producer,
        )
        self.assertEqual(
            evidence_governance.classification,
            DataClassification.CONFIDENTIAL,
        )
        self.assertEqual(evidence_governance.retention_expires_at, 100.0)

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
