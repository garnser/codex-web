"""Canonical opt-in and caller provenance; no app/native/credential reads."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch

from codex_web.configuration import ConfigurationDraftCreate,ConfigurationPublishRequest
from codex_web.identity import AuthenticationAssurance
from codex_web.services.configuration import ConfigurationService
from codex_web.services.identity import IdentityService,AuthenticationError
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.services.native_reasoning_configuration import (PREFIX,FIELDS,
    install_native_reasoning_configuration,resolve_native_reasoning_settings,compose_native_reasoning)
from codex_web.services.codex_reasoning_policy import NativeReasoningPolicyError
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.runtime_credentials import runtime_authentication_preflight


class NativeReasoningConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=SQLiteStateStore(Path(self.temp.name)/'state.sqlite')
        self.identity=IdentityService(IdentityStateStore(self.store));self.identity.bootstrap_local()
        self.actor=self.identity.local_trusted_actor()
        self.config=ConfigurationService(ConfigurationRegistryStore(self.store))
        install_native_reasoning_configuration(self.config)

    def publish(self,name,value):
        record=self.config.create_draft(ConfigurationDraftCreate(key=PREFIX+name,
            scope_type='global',value=value,actor=self.actor.identity_id,reason='Private canonical opt-in'))
        self.config.validate_record(record.id)
        return self.config.publish(record.id,ConfigurationPublishRequest(actor=self.actor.identity_id,
            reason='Private canonical opt-in'))

    def configured(self):
        for name,value in dict(enabled=True,provider_id='native',model_id='model',concrete_model='model',
            agent_profile_id='thinker',agent_profile_revision=1,execution_profile_id='reasoning',
            worker_id='worker',catalog_path='/reviewed/public-model.json',catalog_sha256='a'*64).items():
            self.publish(name,value)

    def compose(self, authentication_preparation=None):
        return compose_native_reasoning(configuration=self.config,identity=self.identity,
            agent_profiles=NS(),execution_profiles=NS(),local_worker=NS(worker=NS(id='worker')),
            host=NS(),codex_home='/reference-only',executable='/reviewed/codex',threads=NS(),turns=NS(),
            sessions=NS(),usage=NS(),preflight=NS(),execution=NS(),hub=NS(),executives=NS(),gateway=NS(),
            registry=NS(),telemetry=NS(),session_managers={},
            authentication_preparation=authentication_preparation)

    def test_disabled_default_does_not_build_or_register_native_runtime(self):
        with patch('codex_web.services.native_reasoning_runtime.build_native_reasoning_components') as build:
            self.assertIsNone(self.compose())
        build.assert_not_called()
        self.assertIsNone(resolve_native_reasoning_settings(self.config))
        for name in FIELDS:
            self.assertFalse(self.config.specs.get(PREFIX+name).grants_authority)

    def test_enabled_composition_forwards_existing_authentication_owner(self):
        self.configured()
        refresh=Mock()
        with patch('codex_web.services.native_reasoning_runtime.build_native_reasoning_components',return_value=NS()) as build, \
             patch('codex_web.services.native_reasoning_runtime.register_native_reasoning_components'):
            self.compose(authentication_preparation=refresh)
        self.assertIs(build.call_args.kwargs['authentication_preparation'],refresh)

    def test_partial_configuration_never_admits(self):
        self.publish('enabled',True)
        with self.assertRaises(NativeReasoningPolicyError):resolve_native_reasoning_settings(self.config)

    def test_existing_configuration_and_identity_revocation_reach_registered_guard(self):
        self.configured()
        built=NS()
        with patch('codex_web.services.native_reasoning_runtime.build_native_reasoning_components',return_value=built) as build,\
             patch('codex_web.services.native_reasoning_runtime.register_native_reasoning_components') as register:
            self.assertIs(self.compose(),built)
        register.assert_called_once()
        guard=build.call_args.kwargs['invocation_authorizer'](NS(project_id='project',
            organization_id='local',workspace_id='default'),self.actor)
        guard()
        self.publish('enabled',False)
        with self.assertRaises(NativeReasoningPolicyError):guard()

    def test_original_session_revocation_and_credentialless_broker_guard(self):
        credentials=self.identity.create_session(identity_id=self.actor.identity_id,scope=self.actor.tenant,
            assurance=AuthenticationAssurance.PRIMARY)
        actor=self.identity.authenticate_session(credentials.session_token,touch=False).actor
        self.identity.revalidate_authenticated_origin(actor)
        self.identity.revoke_session(actor.session_id)
        with self.assertRaises(AuthenticationError):self.identity.revalidate_authenticated_origin(actor)
        delegated=self.identity.actor_for_identity(self.actor.identity_id,scope=self.actor.tenant)
        with self.assertRaises(AuthenticationError):self.identity.revalidate_authenticated_origin(delegated)
        original=Mock()
        current=self.identity.revalidate_authenticated_origin(delegated,origin_guard=original)
        self.assertEqual(current.assurance,AuthenticationAssurance.PRIMARY)
        original.assert_called_once()
        with self.assertRaises(AuthenticationError):
            self.identity.revalidate_authenticated_origin(delegated,origin_guard=Mock(side_effect=AuthenticationError('lost custody')))

    def test_native_reasoning_requires_actual_local_signin_probe_and_no_api_key_fallback(self):
        runtime=ExecutionRuntimeBinding(provider_id='openai',runtime_id='codex-reasoning',capability_revision=1)
        for available in (False,True):
            result=runtime_authentication_preflight(runtime,local_session_probe=lambda:available)
            self.assertEqual(result.available,available)
            self.assertEqual(result.requirement.codex_mode,'trusted_local_session')
            self.assertIsNone(result.requirement.credential_config_key)
        with self.assertRaises(ValueError):
            runtime_authentication_preflight(runtime.model_copy(update={'authentication_mode':'api_key'}),
                local_session_probe=lambda:True)
