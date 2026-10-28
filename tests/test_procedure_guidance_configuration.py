"""Canonical private registry composition; no application, native or provider calls."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_web.configuration import ConfigurationDraftCreate, ConfigurationPublishRequest
from codex_web.services.configuration import ConfigurationService
from codex_web.services.procedure_configuration import (
    PROCEDURE_CONTEXT_CHARACTERS, install_procedure_configuration,
)
from codex_web.skills import SkillCreate
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from tests import test_procedure_discovery as discovery_fixture


class ProcedureGuidanceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.f = discovery_fixture.DiscoveryTests(
            'test_consults_and_pins_exact_contract_without_model_calls')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.config = ConfigurationService(ConfigurationRegistryStore(self.f.state))
        self.spec = install_procedure_configuration(self.config)
        self.resolutions = []

        def resolve():
            current = self.config.resolve(self.spec.key)
            self.resolutions.append(current)
            return current

        self.f.discovery.context_capacity_resolver = resolve

    def publish(self, value):
        draft = self.config.create_draft(ConfigurationDraftCreate(
            key=self.spec.key, scope_type='deployment', value=value,
            actor=self.f.admin.identity_id, reason='Measured complete procedure guidance',
        ))
        return self.config.publish(draft.id, ConfigurationPublishRequest(
            actor=self.f.admin.identity_id, reason='Measured complete procedure guidance',
        ))

    def two_procedures(self):
        # Generic provider contracts, independent conflict groups, full recovery,
        # verification and approval fields. No production Skill data is copied.
        for name in ('readiness', 'verified-change'):
            contract = discovery_fixture.procedure()
            contract['conflict_group'] = name
            item = self.f.skills.create(SkillCreate(
                skill_id=name, name=name,
                instructions=(name + ' requires the complete canonical contract. ') * 40,
                operational_procedure=contract,
            ), actor=self.f.admin)
            self.f.publish(item)

    def test_complete_two_contracts_default_refuses_24000_keeps_every_field_and_support(self):
        self.two_procedures()
        rejected, text = self.f.select()
        self.assertFalse(rejected.admitted)
        self.assertEqual(text, '')
        self.assertIn('required instructions cannot be truncated', rejected.blockers[0])
        published = self.publish(24000)
        remaining = []

        def support(context, refs, available):
            remaining.append(available)
            return SimpleNamespace(text='Authorized supporting context.\n', source_refs=('private-support',))

        self.f.discovery.supporting_context = support
        accepted, text = self.f.select()
        self.assertTrue(accepted.admitted, accepted)
        self.assertEqual(len(accepted.selected), 2)
        self.assertGreater(len(text), 17316)
        self.assertLessEqual(len(text), 24000)
        self.assertEqual(accepted.instruction_characters, len(text))
        self.assertIn('<UNTRUSTED_DATA', text)
        self.assertIn('Authorized supporting context.', text)
        self.assertEqual(accepted.supporting_sources[-1], 'private-support')
        self.assertEqual(len(remaining), 1)
        self.assertGreater(remaining[0], 0)
        self.assertLess(remaining[0], 24000 - 1024)
        for ref in accepted.selected:
            self.assertIn(ref.checksum, text)
            self.assertIn(ref.definition_id, text)
        self.assertEqual(text.count('"operational_procedure"'), 2)
        self.assertEqual(text.count('"on_conflict": "fail_closed"'), 2)
        self.assertEqual(text.count('"count": 1'), 2)
        self.assertEqual(text.count('"verification_step_id": "http"'), 2)
        self.assertEqual(self.resolutions[-1].record_id, published.id)
        self.publish(12000)
        again, text = self.f.select()
        self.assertFalse(again.admitted)
        self.assertEqual(text, '')
        self.assertEqual(len(self.resolutions), 3)

    def test_one_snapshot_survives_publish_during_composition_next_discovery_refreshes(self):
        self.two_procedures()
        self.publish(24000)
        original = self.f.skills.context_for_refs

        def compose(*args, **kwargs):
            self.assertEqual(kwargs['max_chars'], 24000)
            self.publish(12000)
            return original(*args, **kwargs)

        with patch.object(self.f.skills, 'context_for_refs', side_effect=compose):
            admitted, text = self.f.select()
        self.assertTrue(admitted.admitted, admitted)
        self.assertGreater(len(text), 12000)
        self.assertEqual(len(self.resolutions), 1)
        denied, text = self.f.select()
        self.assertFalse(denied.admitted)
        self.assertEqual(text, '')
        self.assertEqual(len(self.resolutions), 2)

    def test_bounds_scope_and_existing_editor_catalog_metadata(self):
        self.assertIn(self.spec, self.config.list_specs())
        self.assertTrue(self.spec.hot_reloadable)
        self.assertFalse(self.spec.startup_only)
        self.assertFalse(self.spec.grants_authority)
        self.assertEqual(self.spec.default, 12000)
        for value in (999, 48001, 0, -1, True, 24000.0, '24000', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.publish(value)
        self.assertEqual(self.spec.validate_value(1000), 1000)
        self.assertEqual(self.spec.validate_value(48000), 48000)
        for scope in ('global', 'organization', 'workspace', 'project', 'resource'):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                self.config.create_draft(ConfigurationDraftCreate(
                    key=self.spec.key, scope_type=scope, scope_id='foreign',
                    value=24000, actor=self.f.admin.identity_id,
                ))

    def test_invalid_effective_or_unavailable_configuration_refuses_before_registry_read(self):
        current = self.config.resolve(self.spec.key)
        invalids = [None, current.model_copy(update={'value': 48001}),
                    current.model_copy(update={'key': 'other'}),
                    current.model_copy(update={'scope_type': 'project', 'scope_id': 'foreign'})]
        for invalid in invalids:
            with self.subTest(invalid=invalid), patch.object(self.f.skills.definitions, 'list_records') as records:
                self.f.discovery.context_capacity_resolver = lambda: invalid
                result, text = self.f.select()
                self.assertFalse(result.admitted)
                self.assertEqual(result.outcome, 'failed')
                self.assertEqual(text, '')
                records.assert_not_called()
        def unavailable():
            raise RuntimeError('private configuration details')
        self.f.discovery.context_capacity_resolver = unavailable
        result, text = self.f.select()
        self.assertFalse(result.admitted)
        self.assertEqual(text, '')
        self.assertNotIn('private configuration details', str(result))

    def test_larger_capacity_preserves_required_skill_security_and_authority_refusal(self):
        self.two_procedures()
        self.publish(24000)
        allowed, _ = self.f.select()
        for changes in ({'policy_allows': False}, {'worker_capabilities': ()},
                        {'required_skill_refs': (allowed.selected[0].model_copy(update={'checksum': 'a' * 64}),)}):
            with self.subTest(changes=changes):
                result, text = self.f.select(**changes)
                self.assertFalse(result.admitted)
                if changes.get('policy_allows') is not False:
                    self.assertEqual(text, '')
        self.f.definitions.quarantine(allowed.selected[0].record_id,
                                     actor='security-operator', reason='Revoked trust')
        result, text = self.f.select()
        self.assertFalse(result.admitted)
        self.assertEqual(text, '')
        self.assertIn('procedure_quarantined', result.blockers)

    def test_support_or_final_trust_wrapper_cannot_exceed_same_configured_limit(self):
        self.f.publish(self.f.draft())
        self.publish(24000)
        self.f.discovery.supporting_context = lambda *args: SimpleNamespace(
            text='x' * 24000, source_refs=())
        result, text = self.f.select()
        self.assertFalse(result.admitted)
        self.assertEqual(text, '')
        self.assertIn('procedure_context_limit', result.blockers)
        self.f.discovery.supporting_context = None
        with patch('codex_web.security.render_untrusted_content', return_value='x' * 24001):
            result, text = self.f.select()
        self.assertFalse(result.admitted)
        self.assertEqual(text, '')
        self.assertIn('procedure_context_limit', result.blockers)
