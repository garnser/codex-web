"""Shared Resource facts never bypass current action/credential contracts."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_web.action_providers import ActionProviderBindingCreate, ActionRequest
from codex_web.identity import TenantScope
from codex_web.resources import ResourceCreate, ResourceType
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.services.action_providers import (
    ActionExecutionService, ActionProviderRegistry, ActionRequirementError, ActionResolutionError,
)
from codex_web.services.code_host_action_contract import (
    CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_GUARDED_ACTION_ID, CodeHostActionContract,
)
from codex_web.services.github_action_provider import GitHubActionProvider
from codex_web.services.gitlab_action_provider import GitLabActionProvider
from codex_web.services.identity import IdentityService, TenantIsolationError
from codex_web.services.resources import ResourceCatalogError, ResourceCatalogService
from codex_web.services.secrets import SecretBroker
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class ActionContractReadObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backend = SQLiteStateStore(self.root / 'state.sqlite3')
        identity = IdentityService(IdentityStateStore(self.backend))
        self.actor = identity.local_trusted_actor()
        self.resources = ResourceCatalogService(ResourceCatalogStore(self.backend))
        self.repository = self.resources.create(ResourceCreate(
            resource_type=ResourceType.REPOSITORY, name='Repository'), actor=self.actor)
        self.broker = SecretBroker(SecretStateStore(self.backend),
            {'local': LocalFileSecretBackend(self.root / 'secrets')})
        self.secret = self.broker.create(SecretCreate(name='Synthetic', value='synthetic-test-value'), actor=self.actor)
        self.provider = self.gitlab()
        self.registry = ActionProviderRegistry(ActionProviderStateStore(self.backend))
        self.registry.register(self.provider)
        self.binding = self.registry.bind(ActionProviderBindingCreate(provider_type='gitlab',
            provider_instance='gitlab.example', resource_ids=(self.repository.id,),
            credential_ref=self.secret.id), actor=self.actor, resources=self.resources)
        self.execution = ActionExecutionService(self.registry, self.resources, secret_broker=self.broker)
        self.request = ActionRequest(action_id=CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_GUARDED_ACTION_ID,
            organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id,
            resource_ids=(self.repository.id,), parameters={}, idempotency_key='same-original-contract')
        self.refs = (('gitlab', 'gitlab.example'),)

    def tearDown(self):
        self.temp.cleanup()

    def gitlab(self, resources=None):
        # No provider operation is called; the client object deliberately has no HTTP methods.
        return GitLabActionProvider(resources or self.resources, object(),
            provider_instance='gitlab.example', api_base='https://gitlab.example/api/v4')

    def eligible(self, refs=None):
        return self.execution.can_observe_contract_reads(resources=self.resources,
            provider_references=self.refs if refs is None else refs)

    def test_stock_gitlab_and_github_actions_are_eligible_and_contract_is_unchanged(self):
        github = GitHubActionProvider(self.resources, object(), provider_instance='github.example',
            api_base='https://github.example', web_base='https://github.example')
        self.registry.register(github)
        self.assertTrue(self.eligible(self.refs + (('github', 'github.example'),)))
        expected = self.execution.resolve_contract(self.binding.id, self.request, actor=self.actor)
        observed = self.resources.observe_reads(lambda view:
            self.execution.resolve_contract(self.binding.id, self.request, actor=self.actor, _resources=view))
        self.assertEqual(observed, expected)
        self.assertEqual(observed[3].credential_ref, self.secret.id)
        self.assertEqual(observed[3].idempotency_key, self.request.idempotency_key)

    def test_provider_actions_instance_class_and_rebound_overrides_decline_without_calling(self):
        other = self.gitlab()
        calls = []
        def forbidden(*args, **kwargs):
            calls.append('actions')
            raise AssertionError('custom actions called during eligibility')
        for replacement in (forbidden, other.actions):
            with self.subTest(replacement=replacement), patch.object(self.provider, 'actions', replacement):
                self.assertFalse(self.eligible())
        with patch.object(GitLabActionProvider, 'actions', forbidden):
            self.assertFalse(self.eligible())
        self.assertEqual(calls, [])

    def test_contract_actions_instance_class_rebound_and_foreign_resources_decline(self):
        other = self.gitlab()
        with patch.object(self.provider.contract, 'actions', other.contract.actions):
            self.assertFalse(self.eligible())
        with patch.object(self.provider.contract, 'actions', side_effect=AssertionError('custom contract')):
            self.assertFalse(self.eligible())
        with patch.object(CodeHostActionContract, 'actions', side_effect=AssertionError('class contract')):
            self.assertFalse(self.eligible())
        foreign = ResourceCatalogService(ResourceCatalogStore(self.backend))
        with patch.object(self.provider.contract, 'resources', foreign):
            self.assertFalse(self.eligible())

    def test_missing_and_custom_tenant_provider_do_not_become_eligible(self):
        self.assertFalse(self.eligible((('gitlab', 'missing'),)))
        other = self.gitlab()
        self.registry.register(other, tenant_scope=TenantScope(organization_id='foreign', workspace_id='other'))
        with patch.object(other, 'actions', side_effect=AssertionError('foreign custom actions')):
            self.assertFalse(self.eligible())

    def test_unknown_provider_contract_descriptor_is_not_inspected(self):
        calls = []
        class CustomProvider:
            @property
            def contract(self):
                calls.append('contract')
                raise AssertionError('custom contract descriptor entered')
        self.registry.providers[('gitlab', 'gitlab.example')] = CustomProvider()
        self.assertFalse(self.eligible())
        self.assertEqual(calls, [])

    def test_custom_legacy_resolution_keeps_original_callback_order(self):
        order = []
        binding = self.registry.binding
        actions = self.provider.actions
        resource = self.resources.get
        metadata = self.broker.metadata
        def read_binding(*args, **kwargs):
            order.append('binding')
            return binding(*args, **kwargs)
        def read_actions():
            order.append('actions')
            return actions()
        def read_resource(*args, **kwargs):
            order.append('resource')
            return resource(*args, **kwargs)
        def read_secret(*args, **kwargs):
            order.append('secret')
            return metadata(*args, **kwargs)
        with patch.object(self.registry, 'binding', read_binding), \
                patch.object(self.provider, 'actions', read_actions), \
                patch.object(self.resources, 'get', read_resource), \
                patch.object(self.broker, 'metadata', read_secret):
            self.assertFalse(self.eligible())
            self.execution.resolve_contract(self.binding.id, self.request, actor=self.actor)
        self.assertEqual(order, ['binding', 'actions', 'resource', 'secret'])

    def test_private_reader_refuses_custom_resolver_and_secret_hooks_before_effects(self):
        def inspect(view):
            for owner, name in ((self.execution, '_resolve'), (self.registry, 'binding'),
                                (self.broker, 'metadata'), (self.broker.store, 'reference_state')):
                with self.subTest(name=name), patch.object(owner, name,
                        side_effect=AssertionError('custom callback entered')):
                    self.assertFalse(self.eligible())
                    with self.assertRaises(ActionRequirementError):
                        self.execution.resolve_contract(self.binding.id, self.request,
                            actor=self.actor, _resources=view)
        self.resources.observe_reads(inspect)

    def test_private_reader_refuses_custom_actions_before_invoking_them(self):
        def inspect(view):
            for owner, name in ((self.provider, 'actions'), (self.provider.contract, 'actions')):
                with self.subTest(owner=type(owner).__name__), patch.object(owner, name,
                        side_effect=AssertionError('custom actions entered')):
                    with self.assertRaises(ActionRequirementError):
                        self.execution.resolve_contract(self.binding.id, self.request,
                            actor=self.actor, _resources=view)
        self.resources.observe_reads(inspect)

    def test_store_and_secret_policy_overrides_decline_without_callbacks(self):
        for owner, name in ((self.registry.store, 'load'), (self.registry.store, '_decode'),
                            (self.registry, 'provider_configuration'),
                            (self.broker.store, 'load'), (self.broker.store, '_decode'),
                            (self.broker, '_reference'), (self.broker, '_scope'),
                            (self.broker, '_can_use'), (self.broker, '_admin')):
            with self.subTest(name=name), patch.object(owner, name,
                    side_effect=AssertionError('custom callback entered')):
                self.assertFalse(self.eligible())

    def test_current_binding_is_not_cached_inside_resource_observation(self):
        def inspect(view):
            self.execution.resolve_contract(self.binding.id, self.request, actor=self.actor, _resources=view)
            self.registry.set_enabled(self.binding.id, False, actor=self.actor)
            with self.assertRaisesRegex(ActionResolutionError, 'disabled'):
                self.execution.resolve_contract(self.binding.id, self.request, actor=self.actor, _resources=view)
        self.resources.observe_reads(inspect)

    def test_foreign_reader_refuses_and_legacy_tenant_denial_still_precedes_hooks(self):
        other = ResourceCatalogService(ResourceCatalogStore(self.backend))
        with self.assertRaises(ResourceCatalogError):
            other.observe_reads(lambda view: self.execution.resolve_contract(
                self.binding.id, self.request, actor=self.actor, _resources=view))
        foreign_request = self.request.model_copy(update={'organization_id': 'foreign'})
        with patch.object(self.registry, 'binding', side_effect=AssertionError('binding called')):
            with self.assertRaises(TenantIsolationError):
                self.execution.resolve_contract(self.binding.id, foreign_request, actor=self.actor)


if __name__ == '__main__':
    unittest.main()
