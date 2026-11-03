import hashlib
import json
import unittest

from codex_web.artifact_evidence import Artifact, ArtifactDigest, Verification
from codex_web.mr_closure_acceptance import (
    IndependentMRReviewManifest,
    MRClosureAcceptanceError,
    MRClosureScope,
    REVIEW_METHOD,
    check_independent_acceptance,
)


class MRClosureAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.scope = MRClosureScope(
            organization_id="org", workspace_id="space", project_id="project",
            work_item_ref="group/repo!33", repository_resource_id="repo",
            provider_binding_id="binding", pull_request_number=33,
            head_sha="a" * 40, base_sha="b" * 40,
        )
        self.content = IndependentMRReviewManifest(
            **self.scope.model_dump(), schema_version="1.0", disposition="accepted",
        ).model_dump_json().encode()
        self.artifact = Artifact(
            id="artifact-review-manifest", organization_id="org", workspace_id="space",
            project_id="project", work_item_ref="group/repo!33", resource_ids=("repo",),
            artifact_type="report", name="Exact MR review", producer_identity_id="implementer",
            revision=self.scope.head_sha, governance_record_id="governed",
            digest=ArtifactDigest(value=hashlib.sha256(self.content).hexdigest()), produced_at=10,
        )
        self.verification = Verification(
            organization_id="org", workspace_id="space", work_item_ref="group/repo!33",
            artifact_ids=(self.artifact.id,), verifier_identity_id="reviewer",
            independent=True, method=REVIEW_METHOD, result="verified", verified_at=20,
            findings=("mr_review_manifest_sha256:" + hashlib.sha256(self.content).hexdigest(),),
        )

    def check(self, **changes):
        values = dict(scope=self.scope, artifact=self.artifact,
                      verification=self.verification, content=self.content,
                      implementation_identity_ids=frozenset({"implementer"}),
                      eligible_reviewer_identity_ids=frozenset({"reviewer"}), now=30)
        values.update(changes)
        return check_independent_acceptance(**values)

    def test_independent_acceptance_for_exact_current_scope(self):
        self.assertEqual(self.check().disposition, "accepted")

    def test_generic_pass_verification_does_not_establish_acceptance(self):
        with self.assertRaisesRegex(MRClosureAcceptanceError, "verification_provenance"):
            self.check(verification=self.verification.model_copy(update={"method": "unit-tests"}))

    def test_changed_head_base_repository_or_binding_invalidates_acceptance(self):
        for field, value in (("head_sha", "c" * 40), ("base_sha", "d" * 40),
                             ("repository_resource_id", "other-repo"),
                             ("provider_binding_id", "other-binding"),
                             ("pull_request_number", 34)):
            with self.subTest(field=field), self.assertRaises(MRClosureAcceptanceError):
                self.check(scope=self.scope.model_copy(update={field: value}))

    def test_implementation_identity_cannot_accept_own_change(self):
        with self.assertRaisesRegex(MRClosureAcceptanceError, "reviewer_not_independent"):
            self.check(verification=self.verification.model_copy(
                update={"verifier_identity_id": "implementer"}))

    def test_revoked_reviewer_and_missing_implementation_provenance_refuse(self):
        for changes in ({"eligible_reviewer_identity_ids": frozenset()},
                        {"implementation_identity_ids": frozenset()}):
            with self.subTest(changes=changes), self.assertRaises(MRClosureAcceptanceError):
                self.check(**changes)

    def test_tampered_manifest_cannot_reuse_verification(self):
        content = self.content.replace(b"accepted", b"rejected")
        with self.assertRaisesRegex(MRClosureAcceptanceError, "artifact_scope_or_content"):
            self.check(content=content)

    def test_valid_digest_of_rejection_is_not_acceptance(self):
        raw = json.loads(self.content)
        raw["disposition"] = "rejected"
        content = json.dumps(raw).encode()
        artifact = self.artifact.model_copy(update={
            "digest": ArtifactDigest(value=hashlib.sha256(content).hexdigest())})
        verification = self.verification.model_copy(update={
            "findings": ("mr_review_manifest_sha256:" + hashlib.sha256(content).hexdigest(),)})
        with self.assertRaisesRegex(MRClosureAcceptanceError, "not_accepted_for_current_scope"):
            self.check(content=content, artifact=artifact, verification=verification)

    def test_replaced_content_with_updated_artifact_digest_needs_new_review(self):
        content = self.content + b" "
        artifact = self.artifact.model_copy(update={
            "digest": ArtifactDigest(value=hashlib.sha256(content).hexdigest())})
        with self.assertRaisesRegex(MRClosureAcceptanceError, "verification_provenance"):
            self.check(content=content, artifact=artifact)

    def test_invalidated_expired_or_wrong_work_artifact_refuses(self):
        for update in ({"lifecycle": "invalidated"}, {"retention_expires_at": 29},
                       {"work_item_ref": "group/other!33"}, {"governance_record_id": None}):
            with self.subTest(update=update), self.assertRaises(MRClosureAcceptanceError):
                self.check(artifact=self.artifact.model_copy(update=update))

    def test_verification_cannot_cover_an_unrelated_or_mixed_artifact_set(self):
        for update in ({"artifact_ids": ("different",)},
                       {"artifact_ids": (self.artifact.id, "different")},
                       {"evidence_ids": ("generic-pass",)}, {"independent": False},
                       {"findings": ()},
                       {"result": "invalidated"}, {"verified_at": 9}, {"verified_at": 31}):
            with self.subTest(update=update), self.assertRaises(MRClosureAcceptanceError):
                self.check(verification=self.verification.model_copy(update=update))

    def test_review_content_is_bounded(self):
        with self.assertRaisesRegex(MRClosureAcceptanceError, "content_bounds"):
            self.check(content=b"x" * (16 * 1024 + 1))
