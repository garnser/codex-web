"""Discovery uses the real registry, publication/security and Resource services."""
import dataclasses
import unittest
from unittest.mock import patch

from codex_web.definitions import DefinitionLifecycle, DefinitionReference
from codex_web.services.procedure_discovery import OperationContext, ProcedureDiscoveryService
from codex_web.resources import ResourceCreate, ResourceType
from codex_web.skills import SkillCreate, SkillUpdate
from tests import test_skill_procedures as fixtures
procedure = fixtures.procedure


class DiscoveryTests(unittest.TestCase):
    tearDown = fixtures.ProcedureTests.tearDown
    draft = fixtures.ProcedureTests.draft
    publish = fixtures.ProcedureTests.publish
    def setUp(self):
        fixtures.ProcedureTests.setUp(self)
        self.resource = self.resources.create(ResourceCreate(resource_type=ResourceType.SERVICE, name="GitLab"), actor=self.admin)
        self.discovery = ProcedureDiscoveryService(self.skills, self.resources)
        self.context = OperationContext(actor=self.admin, project_id="project-a", resource_ids=(self.resource.id,),
            operation="service.rotate", environment="staging", service_versions={self.resource.id: {"gitlab": "18.0.0"}},
            worker_capabilities=("network",), operation_capabilities=("service.change",),
            provider_type="ops", provider_instance="test", provider_contract_version="1.2",
            policy_source="canonical:test-policy", policy_allows=True, enforcement="canonical_action")

    def select(self, **changes):
        return self.discovery.discover(dataclasses.replace(self.context, **changes))

    def test_consults_and_pins_exact_contract_without_model_calls(self):
        published = self.publish(self.draft())
        result, text = self.select()
        self.assertTrue(result.admitted, result)
        self.assertEqual(result.outcome, "matched")
        self.assertEqual(result.selected, (DefinitionReference.model_validate(published["definitionReference"]),))
        self.assertIn('"operational_procedure"', text)
        self.assertIn('"change": "rotation"', text)
        self.assertEqual(result.instruction_characters, len(text))
        self.assertTrue(result.instruction_checksum)

    def test_genuine_no_match_retains_existing_authorization(self):
        result, _ = self.select()
        self.assertEqual(result.outcome, "no_match")
        self.assertTrue(result.admitted)
        denied, _ = self.select(policy_allows=False)
        self.assertFalse(denied.admitted)
        self.publish(self.draft())
        unmatched, text = self.select(operation="repository.read", operation_capabilities=())
        self.assertTrue(unmatched.admitted)
        self.assertEqual(unmatched.outcome, "no_match")
        self.assertEqual(text, "")
        self.assertIn("operation_does_not_match", unmatched.candidates[0].reasons)

    def test_failed_discovery_never_looks_like_no_match(self):
        with patch.object(self.skills.definitions, "list_records", side_effect=RuntimeError("sensitive transport detail")):
            result, text = self.select()
        self.assertEqual(result.outcome, "failed")
        self.assertFalse(result.admitted)
        self.assertEqual(text, "")
        self.assertNotIn("sensitive", str(result))

    def test_inaccessible_stale_quarantined_and_missing_facts_block(self):
        published = self.publish(self.draft())
        for changes in ({"environment": None}, {"service_versions": {}}, {"worker_capabilities": ()}):
            result, text = self.select(**changes)
            self.assertEqual(result.outcome, "inaccessible")
            self.assertFalse(result.admitted)
            self.assertEqual(text, "")
        self.now = 201
        result, _ = self.select()
        self.assertIn("procedure_expired", result.blockers)
        self.now = 100
        with patch.object(self.skills, "validate_required_procedure", side_effect=__import__('codex_web.services.skills', fromlist=['SkillResolutionFailure']).SkillResolutionFailure("procedure_quarantined", "quarantined")):
            result, _ = self.select()
            self.assertIn("procedure_quarantined", result.blockers)
        # Foreign records are excluded without exposing their identity/body.
        foreign, text = self.select(actor=self.foreign, resource_ids=())
        self.assertEqual(foreign.outcome, "no_match")
        self.assertEqual(foreign.candidates, ())
        self.assertEqual(text, "")

    def test_conflicts_fail_closed_and_precedence_explains_rejections(self):
        self.publish(self.draft())
        other = self.skills.create(SkillCreate(skill_id="other-gitlab", name="Other", instructions="Inspect canonical evidence", operational_procedure=procedure()), actor=self.admin)
        self.publish(other)
        conflict, _ = self.select()
        self.assertEqual(conflict.outcome, "ambiguous")
        self.assertFalse(conflict.admitted)
        self.assertEqual(conflict.selected, ())
        higher = procedure(); higher['precedence'] = 11
        changed = self.skills.update("other-gitlab", SkillUpdate(operational_procedure=higher, reason="Reviewed precedence"), actor=self.admin)
        self.publish(changed)
        result, _ = self.select()
        self.assertTrue(result.admitted)
        self.assertEqual(result.selected[0].definition_id, "other-gitlab")
        self.assertTrue(any("lower_precedence:gitlab-operations" in c.reasons for c in result.candidates))

    def test_scope_change_and_revision_change_do_not_rewrite_history(self):
        first = self.publish(self.draft())
        old, _ = self.select()
        changed_scope, _ = self.select(operation="repository.read", operation_capabilities=())
        self.assertNotEqual(old.context_checksum, changed_scope.context_checksum)
        self.assertEqual(changed_scope.selected, ())
        newer = procedure(); newer['precedence'] = 11
        self.publish(self.skills.update("gitlab-operations", SkillUpdate(operational_procedure=newer, reason="Reviewed successor"), actor=self.admin))
        refused, _ = self.discovery.discover(self.context, previous=old)
        self.assertFalse(refused.admitted)
        self.assertEqual(old.selected[0].record_id, first['recordId'])
        self.assertIn("procedure_revision_changed", refused.blockers[0])

    def test_bounded_context_and_retrieval_failure_block(self):
        self.publish(self.draft())
        self.discovery.max_context_characters = 1000
        result, _ = self.select()
        self.assertFalse(result.admitted)
        self.assertIn("procedure_context_limit", result.blockers[0])
        self.discovery.max_context_characters = 12000
        def fail(*args):
            raise OSError("private upstream response")
        self.discovery.supporting_context = fail
        result, text = self.select()
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(text, "")

    def test_alternative_provider_and_unmediated_native_mutation_refuse(self):
        self.publish(self.draft())
        bypass, text = self.select(provider_type="other", operation="other.rotate")
        self.assertFalse(bypass.admitted)
        self.assertIn("procedure_provider_enforcement_unsupported", bypass.blockers)
        self.assertEqual(text, "")
        native, _ = self.select(enforcement="native_tools")
        self.assertEqual(native.outcome, "unsupported_enforcement")
        self.assertFalse(native.admitted)
        # Read-only no-match exploration still relies on existing policy.
        exploration, _ = self.select(enforcement="bounded_read_only", operation="service.read", operation_capabilities=())
        self.assertTrue(exploration.admitted)

    def test_real_quarantine_disabled_incompatible_and_foreign_targets_fail_closed(self):
        published = self.publish(self.draft())
        record_id = published['recordId']
        self.definitions.quarantine(record_id, actor='security-operator', reason='Revoked trust')
        result, text = self.select()
        self.assertEqual(result.outcome, 'inaccessible')
        self.assertIn('procedure_quarantined', result.blockers)
        self.assertEqual(text, '')
        foreign, _ = self.select(actor=self.foreign)
        self.assertEqual(foreign.outcome, 'failed')
        self.assertFalse(foreign.admitted)

    def test_target_environment_operation_and_resource_versions_change_fingerprint(self):
        self.publish(self.draft())
        original, _ = self.select()
        for changes in ({'environment':'production'}, {'operation':'other.read', 'operation_capabilities':()},
                        {'service_versions':{self.resource.id:{'gitlab':'19.0.0'}}}):
            changed, text = self.select(**changes)
            self.assertEqual(changed.outcome, 'no_match')
            self.assertNotEqual(changed.context_checksum, original.context_checksum)
            self.assertEqual(changed.selected, ())
            self.assertEqual(text, '')
        self.assertTrue(original.selected)

    def test_unrelated_incompatible_candidate_is_rejected_before_compatibility(self):
        matching = self.publish(self.draft())
        unrelated_resource = self.resources.create(ResourceCreate(resource_type=ResourceType.SERVICE, name='Other'), actor=self.admin)
        content = procedure()
        content['resource_selectors'][0]['resource_ids'] = [unrelated_resource.id]
        unrelated = self.publish(self.skills.create(SkillCreate(skill_id='unrelated-service', name='Unrelated',
            instructions='Inspect unrelated service', operational_procedure=content), actor=self.admin))
        # Registry compatibility metadata is independent of payload integrity.
        self.skills.definitions.store.update(lambda records: [r.model_copy(update={'max_engine_version': '0.0.0'})
            if r.record_id == unrelated['recordId'] else r for r in records])
        result, _ = self.select()
        self.assertTrue(result.admitted, result)
        self.assertEqual(result.selected[0].record_id, matching['recordId'])
        candidate = next(c for c in result.candidates if c.reference.record_id == unrelated['recordId'])
        self.assertEqual(candidate.outcome, 'rejected')
        self.assertTrue(any(reason.startswith('resource_selector_does_not_match:') for reason in candidate.reasons))
