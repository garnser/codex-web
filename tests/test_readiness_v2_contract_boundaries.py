"""Canonical publication/admission boundaries for additive guarded reads.

No action execution, provider request, native process or model invocation occurs.
The separate composed tests qualify execute/verification parent deadlines.
"""
import unittest

from codex_web.action_intents import ActionIntentCreate, ActionIntentWorkItemSuccess
from codex_web.action_providers import ActionRequest
from codex_web.definitions import DefinitionReference
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.action_providers import ActionRequirementError, ActionResolutionError
from codex_web.services.code_host_action_contract import (
    CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_ACTION_ID as LEGACY,
    CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_GUARDED_ACTION_ID as GUARDED,
)
from codex_web.services.definitions import DefinitionGateDeniedError
from codex_web.services.gitlab_action_provider import GitLabActionProvider
from codex_web.services.skills import SkillConflict
from codex_web.skills import SkillCreate, SkillPublish
from tests import test_skill_procedures as publication
from tests import test_premerge_readiness_observation as execution


class ReadinessPublicationBoundaryTests(unittest.TestCase):
    setUp = publication.ProcedureTests.setUp
    tearDown = publication.ProcedureTests.tearDown

    def content(self, action=GUARDED, *, version='1.3', protocol='code-host-mr-procedure-input.v2', timeout=120):
        content = publication.procedure()
        content.update(schema_version=version, inputs=[], service_versions=[],
                       required_worker_capabilities=[], max_duration_seconds=max(120, timeout),
                       operation_triggers=[action])
        if protocol is not None:
            content['input_protocol'] = protocol
        step = content['steps'][0]
        step.update(timeout_seconds=timeout, inputs=[], max_attempts=1,
                    permissions=[{'capability':'repository.read.metadata', 'level':'read', 'resource_selector':'service'}])
        step['action'].update(provider_type='gitlab', provider_instance='gitlab.example', action_id=action)
        content['steps'] = [step]
        content['resource_selectors'] = [{'id':'service', 'resource_types':['repository']}]
        return content

    def register(self):
        provider = GitLabActionProvider(self.resources, GitLabClient(), provider_instance='gitlab.example',
                                       api_base='https://gitlab.example/api/v4')
        self.actions.register(provider, tenant_scope=self.admin.tenant)
        return provider

    def publish(self, content, suffix):
        draft = self.skills.create(SkillCreate(skill_id='guarded-contract-' + suffix,
            name='Private exact contract boundary', instructions='Observe through canonical read authority.',
            operational_procedure=content), actor=self.admin)
        return self.skills.publish(draft['skillId'], draft['recordId'], SkillPublish(), actor=self.admin)

    def test_exact_protocol_can_publish_but_old_or_missing_protocol_cannot_borrow_guarded_budget(self):
        self.register()
        accepted = self.publish(self.content(), 'current')
        ref = DefinitionReference.model_validate(accepted['definitionReference'])
        self.assertEqual(self.skills.validate_required_procedure(ref, actor=self.admin), ref)
        for suffix, version, protocol in [('legacy','1.2','code-host-mr-procedure-input.v1'),
                                          ('missing','1.3',None)]:
            with self.subTest(protocol=protocol), self.assertRaises((SkillConflict, DefinitionGateDeniedError)):
                self.publish(self.content(version=version, protocol=protocol), suffix)
        self.assertEqual(self.actions.list_bindings(self.admin), [])  # Publication creates no grant.

    def test_registered_ceiling_applies_to_publication_and_legacy_stays_thirty(self):
        provider = self.register()
        definitions = {row.action_id: row for row in provider.actions()}
        self.assertEqual(provider.contract_version, '1.2')
        self.assertEqual(definitions[LEGACY].timeout_seconds, 30)
        self.assertEqual(definitions[GUARDED].timeout_seconds, 300)
        self.publish(self.content(LEGACY, version='1.2', protocol='code-host-mr-procedure-input.v1', timeout=30), 'legacy-valid')
        supported = self.content(timeout=240)
        supported['max_duration_seconds'] = 300
        self.publish(supported, 'finite-host-capacity')
        self.publish(self.content(timeout=300), 'exact-ceiling')
        for suffix, content in [('new-over',self.content(timeout=301)),
                                ('old-over',self.content(LEGACY, version='1.2', protocol='code-host-mr-procedure-input.v1', timeout=31))]:
            # Keep overall duration valid so the actual registered-operation gate decides.
            content['max_duration_seconds'] = 301
            with self.subTest(action=content['steps'][0]['action']['action_id']), self.assertRaises((SkillConflict, DefinitionGateDeniedError)):
                self.publish(content, suffix)

    def test_unknown_action_and_unadvertised_guarded_action_fail_closed(self):
        provider = self.register()
        with self.assertRaises((SkillConflict, DefinitionGateDeniedError)):
            self.publish(self.content(GUARDED + '.unknown'), 'unknown')
        # An adapter/catalog without the additive operation cannot accept a
        # published reference merely because another version knows its ID.
        original = provider.actions
        provider.actions = lambda: tuple(row for row in original() if row.action_id != GUARDED)
        with self.assertRaises((SkillConflict, DefinitionGateDeniedError)):
            self.publish(self.content(), 'unsupported')
        self.publish(self.content(LEGACY, version='1.2', protocol='code-host-mr-procedure-input.v1', timeout=30), 'supported-legacy')


class ReadinessIntentBoundaryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = execution.PremergeReadinessProcedureTests.asyncSetUp
    tearDown = execution.PremergeReadinessProcedureTests.tearDown
    configure = execution.PremergeReadinessProcedureTests.configure
    configure_project = execution.PremergeReadinessProcedureTests.configure_project
    assignment_options = execution.PremergeReadinessProcedureTests.assignment_options
    skill_create_options = execution.PremergeReadinessProcedureTests.skill_create_options
    artifact = execution.PremergeReadinessProcedureTests.artifact
    proof = execution.PremergeReadinessProcedureTests.proof
    rebuild = execution.PremergeReadinessProcedureTests.rebuild
    respond = execution.PremergeReadinessProcedureTests.respond

    def payload(self, action=GUARDED, **overrides):
        return ActionIntentCreate(binding_id=self.binding.id, request=ActionRequest(
            action_id=action, organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id, project_id=self.project.id,
            resource_ids=(self.repo.id,), parameters={'pull_request_number':33,'expected_head_sha':'a'*40}),
            **overrides)

    async def test_secret_reference_new_intent_cannot_inflate_or_bypass_published_step(self):
        before = self.intents.store.load().model_dump(mode='json')
        for timeout, message in [(301, 'registered execution limit'), (240, 'exact current published procedure step'), (120, 'exact current published procedure step')]:
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ActionRequirementError, message):
                self.intents.create(self.payload(timeout_seconds=timeout), actor=self.actor)
        with self.assertRaisesRegex(ActionRequirementError, 'cannot declare Work success'):
            self.intents.create(self.payload(work_item_success=ActionIntentWorkItemSuccess(current_stage='done')), actor=self.actor)
        self.assertEqual(self.intents.store.load().model_dump(mode='json'), before)
        self.assertEqual(self.observed_calls, [])
        self.runner.models.invoke.assert_not_called()

    async def test_secret_reference_legacy_default_and_unknown_action_keep_existing_catalog_contract(self):
        intent = self.intents.create(self.payload(LEGACY), actor=self.actor)
        self.assertEqual(intent.timeout_seconds, 30)
        self.assertEqual(intent.action_definition.timeout_seconds, 30)
        before = self.intents.store.load().model_dump(mode='json')
        with self.assertRaises(ActionResolutionError):
            self.intents.create(self.payload(GUARDED + '.unknown'), actor=self.actor)
        self.assertEqual(self.intents.store.load().model_dump(mode='json'), before)
        self.assertEqual(self.observed_calls, [])
        self.runner.models.invoke.assert_not_called()
