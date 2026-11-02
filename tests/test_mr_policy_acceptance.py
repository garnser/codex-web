"""Real current Definition owner and canonical native-review adapter composition."""
from unittest.mock import patch
import unittest
from types import SimpleNamespace

from codex_web.artifact_evidence import EvidenceRequirement, EvidenceCreate, VerificationCreate

from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.mr_assignment_review import AssignmentReviewError
from codex_web.services.mr_policy_acceptance import CurrentPolicyMRAcceptanceOwner, MRPolicyAcceptanceError
from tests import test_mr_assignment_review as fixtures


class CurrentPolicyMRAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=f=fixtures.AssignmentMRReviewTests()
        f.addCleanup=self.addCleanup
        f.addAsyncCleanup=self.addAsyncCleanup
        await f.asyncSetUp()
        await f.prepare_broker_review(bootstrap=True)
        f.e.evidence.work_item_host=SimpleNamespace(_load_work_item_states=f.e.items.load,
            _save_work_item_states=f.e.items.save)
        self.result=await f.native_create()
        self.assertIsNotNone(self.result['item'],self.result)
        self.owner=CurrentPolicyMRAcceptanceOwner(execution_roles=f.e.roles,
            assignment_reviews=f.native_owner,evidence=f.e.evidence,work_items=f.e.work_items)

    def accept(self, owner=None):
        f=self.fixture
        return (owner or self.owner).acceptance(f.scope,f.native_artifact.id,
            self.result['item']['id'],actor=f.e.worker_actor)

    def publish_policy(self, policy):
        f=self.fixture
        record=f.e.registry.get_record(f.e.roles.reference().record_id)
        payload=record.payload.copy()
        if policy is None:
            payload.pop('mr_review_policy',None)
        else:
            payload['mr_review_policy']=policy
        draft=f.e.registry.create_draft(DefinitionDraftCreate(definition_id=record.definition_id,
            kind=record.kind,definition_schema_version='1.1',scope_type=record.scope_type,
            scope_id=record.scope_id,payload=payload,actor='private-policy-author'))
        f.e.registry.approve_publication(draft.record_id,actor='private-separate-reviewer',
            reference='PRIVATE-POLICY-REVIEW',reason='Private current-policy refusal fixture')
        return f.e.registry.publish(draft.record_id,DefinitionPublishRequest(
            actor='private-policy-author',expected_active_revision=record.revision))

    async def test_explicit_assignment_policy_uses_canonical_receipt_without_independence_upgrade(self):
        manifest=self.accept()
        self.assertEqual(manifest.disposition,'accepted')
        record=self.fixture.e.evidence.get_verification(self.result['item']['id'],actor=self.fixture.e.worker_actor)
        self.assertFalse(record.independent)
        self.assertEqual(record.model_dump(mode='json'),self.result['item'])

    async def test_absent_or_principal_policy_never_falls_back_to_assignment(self):
        for policy in (None,{'schema_version':'1.0','basis':'principal'}):
            with self.subTest(policy=policy):
                self.publish_policy(policy)
                with patch.object(self.fixture.native_owner,'validate_canonical_receipt',
                        side_effect=AssertionError('assignment fallback forbidden')):
                    with self.assertRaisesRegex(MRPolicyAcceptanceError,'principal_eligibility_owner_unavailable'):
                        self.accept()

    async def test_stricter_principal_independence_stays_required(self):
        strict=CurrentPolicyMRAcceptanceOwner(execution_roles=self.fixture.e.roles,
            assignment_reviews=self.fixture.native_owner,evidence=self.fixture.e.evidence,
            work_items=self.fixture.e.work_items,require_principal_independence=True)
        with self.assertRaises(AssignmentReviewError):
            self.accept(strict)
        self.assertFalse(self.fixture.e.evidence.get_verification(self.result['item']['id'],
            actor=self.fixture.e.worker_actor).independent)

    async def test_policy_publication_during_observation_refuses_same_payload_new_revision(self):
        original=self.fixture.native_owner.validate_canonical_receipt
        def concurrent(*args,**kwargs):
            result=original(*args,**kwargs)
            self.publish_policy(self.fixture.raw_catalog['mr_review_policy'])
            return result
        with patch.object(self.fixture.native_owner,'validate_canonical_receipt',side_effect=concurrent):
            with self.assertRaisesRegex(MRPolicyAcceptanceError,'current_policy_changed'):
                self.accept()

    async def test_current_catalog_changed_before_gate_cannot_accept_historical_receipt(self):
        self.publish_policy(self.fixture.raw_catalog['mr_review_policy'])
        with self.assertRaises(AssignmentReviewError):
            self.accept()

    async def test_foreign_tenant_does_not_consult_or_relabel_policy(self):
        scope=self.fixture.scope.model_copy(update={'organization_id':'foreign'})
        with patch.object(self.fixture.e.roles,'resolve_catalog',side_effect=AssertionError('foreign query forbidden')):
            with self.assertRaisesRegex(MRPolicyAcceptanceError,'actor_tenant_mismatch'):
                self.owner.acceptance(scope,self.fixture.native_artifact.id,self.result['item']['id'],
                    actor=self.fixture.e.worker_actor)

    def requirements(self, *values):
        f=self.fixture
        return f.e.evidence.set_work_item_requirements(f.e.ref,values,actor=f.e.admin)

    async def test_generic_bootstrap_strict_scoped_review_cannot_be_weakened_by_constructor_false(self):
        self.assertIsNone(self.fixture.e.assignment.work_item_ref)
        review=self.fixture.e.evidence.get_evidence(self.result['evidence']['id'],actor=self.fixture.e.worker_actor)
        self.requirements(EvidenceRequirement(id='independent-review',evidence_type='review',
            artifact_type='report',provider=review.provider,independent_verification=True))
        with self.assertRaisesRegex(AssignmentReviewError,'principal_independence_required'):
            self.accept()
        self.assertFalse(self.fixture.e.evidence.get_verification(self.result['item']['id'],
            actor=self.fixture.e.worker_actor).independent)

    async def test_unrelated_independent_ci_is_evaluated_without_demanding_independent_qa(self):
        f=self.fixture
        self.requirements(EvidenceRequirement(id='independent-ci',evidence_type='ci_check',
            independent_verification=True))
        with self.assertRaisesRegex(MRPolicyAcceptanceError,'work_evidence_unsatisfied'):
            self.accept()
        # Real canonical distinct producer/verifier; synthetic CI facts, not a
        # claim of an external CI run or a fixed provider qualification method.
        evidence=f.e.evidence.create_evidence(EvidenceCreate(project_id=f.scope.project_id,
            work_item_ref=f.e.ref,resource_ids=(f.e.target.id,),evidence_type='ci_check',result='pass'),
            actor=f.e.admin)
        verification=f.e.evidence.create_verification(VerificationCreate(work_item_ref=f.e.ref,
            evidence_ids=(evidence.id,),method='private-ci-proof',result='verified'),actor=f.e.worker_actor)
        self.assertTrue(verification.independent)
        self.assertEqual(self.accept().disposition,'accepted')

    async def test_review_provider_and_artifact_filters_do_not_reclassify_qa(self):
        self.requirements(EvidenceRequirement(id='other-review',evidence_type='review',
            artifact_type='report',provider='different-provider',independent_verification=True))
        # The current receipt does not match; the full normal evidence gate
        # refuses missing required evidence rather than claiming independent QA.
        with self.assertRaisesRegex(MRPolicyAcceptanceError,'work_evidence_unsatisfied'):
            self.accept()

    async def test_stricter_work_requirement_added_during_receipt_io_is_refused(self):
        original=self.fixture.native_owner.validate_canonical_receipt
        def concurrent(*args,**kwargs):
            result=original(*args,**kwargs)
            self.requirements(EvidenceRequirement(id='new-independent-review',evidence_type='review',
                independent_verification=True))
            return result
        with patch.object(self.fixture.native_owner,'validate_canonical_receipt',side_effect=concurrent):
            with self.assertRaisesRegex(MRPolicyAcceptanceError,'work_requirements_or_review_changed'):
                self.accept()

    async def test_unavailable_missing_or_foreign_project_work_cannot_look_like_empty_requirements(self):
        f=self.fixture
        host=f.e.evidence.work_item_host
        f.e.evidence.work_item_host=None
        with self.assertRaisesRegex(MRPolicyAcceptanceError,'requirements_owner_unavailable'):
            self.accept()
        f.e.evidence.work_item_host=host
        original=f.e.items.get(f.e.ref)
        f.e.items.put(f.e.ref,original.model_copy(update={'project_id':'foreign-project'}))
        with self.assertRaisesRegex(MRPolicyAcceptanceError,'requirements_scope_mismatch'):
            self.accept()
        f.e.items.delete(f.e.ref)
        with self.assertRaisesRegex(MRPolicyAcceptanceError,'requirements_scope_unavailable'):
            self.accept()
