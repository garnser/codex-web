"""Real current Definition owner and canonical native-review adapter composition."""
from unittest.mock import patch
import unittest

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
        f.native_resolved=await f.prepare_native_owner()
        self.result=await f.native_create()
        self.assertIsNotNone(self.result['item'],self.result)
        self.owner=CurrentPolicyMRAcceptanceOwner(execution_roles=f.e.roles,
            assignment_reviews=f.native_owner)

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
            assignment_reviews=self.fixture.native_owner,require_principal_independence=True)
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
