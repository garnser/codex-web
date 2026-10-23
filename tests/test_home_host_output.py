"""Actual private command receipts, encryption, governance and policy; no host process."""
from dataclasses import replace
import hashlib
import unittest

from codex_web.data_governance import ContextFilterRequest
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest, reference_for
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.home_output_policy import HOME_OUTPUT_POLICY_KIND, HomeOutputReleasePolicy
from codex_web.services.home_host_output import HomeHostOutputService, HostOutputSelection, OUTPUT_READ
from codex_web.services.data_governance import GovernanceConflictError
from tests.test_home_host_action_intents import HomeHostActionIntentTests


class HomeHostOutputTests(HomeHostActionIntentTests):
    def publish(self, payload):
        row=next(p for p in payload['profiles'] if p['id']=='orchestration-only')
        row['control_plane_operations']=list(dict.fromkeys([*row['control_plane_operations'],'operator.host.inspect',OUTPUT_READ]))
        return super().publish(payload)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.outputs=HomeHostOutputService(home_host=self.service.home_operator_host,worker_runtime=self.runtime,
            intents=self.intents,artifacts=self.artifacts,crypto=self.crypto,registry=self.registry)
        self.exit_code=0

    def effect(self, operation, **kwargs):
        result=super().effect(operation,**kwargs)
        return replace(result,command_digest='sha256:'+hashlib.sha256('\0'.join(kwargs['argv']).encode()).hexdigest(),
            stdout=getattr(self, 'stdout', 'ordinary source/test output'),exit_code=self.exit_code)

    def policy_payload(self, **changes):
        a=self.assignment
        return {'organization_id':a.organization_id,'workspace_id':a.workspace_id,
            'administrator_id':self.actor.identity_id,'agent_profile_id':a.agent_profile.profile_id,
            'agent_profile_revision':a.agent_profile.profile_revision,'host_resource_id':self.host_resource_id,
            'trusted_worker_ids':[self.f.worker.id], 'max_excerpt_bytes':1024,
            'commands':[{'argv':['/usr/bin/true']}],**changes}

    def publish_policy(self, **changes):
        record=self.registry.create_draft(DefinitionDraftCreate(definition_id='home.output.fixture',
            kind=HOME_OUTPUT_POLICY_KIND,definition_schema_version='1.0',scope_type='project',scope_id='home',
            payload=self.policy_payload(**changes),actor=self.actor.identity_id))
        self.registry.publish(record.record_id,DefinitionPublishRequest(actor=self.actor.identity_id))
        return self.registry.get_record(record.record_id)

    def configure_policy(self, **changes):
        policy=self.publish_policy(**changes)
        self.host_options['output_release_policy']=reference_for(policy).model_dump(mode='json')
        profile=self.publish(execution_profile_catalog_seed_payload())
        self.mutate_assignment(execution_profile_definition=reference_for(profile))
        self.assignment=self.current()
        return policy

    async def command_output(self):
        intent=self.create()
        await self.provider.prepare(intent.request,binding=self.action_binding)
        await self.execute(intent)
        history=self.intents.history(intent.id,self.actor)
        self.assertIsNotNone(history['receipts'][0]['result'], str(history['receipts'][0]['details'])+' '+str(self.intents.get(intent.id,self.actor).last_error))
        return history['receipts'][0]['result']['output']['output_artifact_id']

    def selection(self):
        return HostOutputSelection(byte_end=len('ordinary source/test output'))

    async def test_policy_releases_known_owned_output_with_source_lineage_and_no_repeat_command(self):
        self.configure_policy();source=await self.command_output()
        released=self.outputs.release(source,self.selection(),snapshot=self.assignment,worker_actor=self.worker_actor)
        read=self.outputs.read(self.assignment,released['artifact_id'],worker_actor=self.worker_actor)
        self.assertEqual(read['text'],'ordinary source/test output');self.assertEqual(read['review_mode'],'policy')
        original=self.artifacts.get_artifact(source,actor=self.actor)
        original_record=self.artifacts.governance.get_record(original.governance_record_id,self.actor)
        self.assertEqual(original_record.classification,'secret');self.assertTrue(original_record.deny_model_context)
        record=self.artifacts.governance.get_record(released['governance_record_id'],self.actor)
        self.assertEqual(record.source_record_ids,(original_record.id,))
        self.assertEqual(record.host_output_release.source_artifact_id,source)
        again=self.outputs.release(source,self.selection(),snapshot=self.assignment,worker_actor=self.worker_actor)
        self.assertEqual(again,released);self.assertEqual(self.effects,1)
        self.assertEqual(len(self.artifacts.list_artifacts(self.actor)),2)

    async def test_unconfigured_output_denied_then_explicit_human_review_releases_only_previewed_excerpt(self):
        source=await self.command_output()
        with self.assertRaises(GovernanceConflictError):
            self.outputs.release(source,self.selection(),snapshot=self.assignment,worker_actor=self.worker_actor)
        preview=self.outputs.preview(source,actor=self.actor,expected_home_revision=1)
        text=preview['streams']['stdout'];self.assertEqual(text,'ordinary source/test output')
        selected=HostOutputSelection(byte_end=8,expected_sha256=hashlib.sha256(text[:8].encode()).hexdigest())
        released=self.outputs.release(source,selected,actor=self.actor,classification='internal',
            reason='Reviewed ordinary source excerpt',expected_home_revision=1)
        read=self.outputs.read(self.assignment,released['artifact_id'],worker_actor=self.worker_actor)
        self.assertEqual(read['text'],'ordinary');self.assertEqual(read['review_mode'],'human-review')
        self.assertEqual(self.effects,1)

    async def test_policy_revocation_and_missing_adapter_both_deny_previously_released_output(self):
        self.configure_policy();source=await self.command_output()
        released=self.outputs.release(source,self.selection(),snapshot=self.assignment,worker_actor=self.worker_actor)
        self.publish_policy(max_excerpt_bytes=512)
        with self.assertRaises(GovernanceConflictError):
            self.outputs.read(self.assignment,released['artifact_id'],worker_actor=self.worker_actor)
        self.artifacts.governance.host_output_release_validator=None
        filtered=self.artifacts.governance.filter_context(ContextFilterRequest(record_ids=(released['governance_record_id'],)),actor=self.actor)
        self.assertEqual(filtered.allowed_record_ids,())

    async def test_exact_command_rule_does_not_infer_output_safety_from_exit_success(self):
        self.configure_policy(commands=[{'argv':['/usr/bin/false']}]);source=await self.command_output()
        with self.assertRaises(GovernanceConflictError):
            self.outputs.release(source,self.selection(),snapshot=self.assignment,worker_actor=self.worker_actor)
        self.assertEqual(len(self.artifacts.list_artifacts(self.actor)),1)

    async def test_explicit_known_failed_test_exit_can_release_error_output_without_claiming_success(self):
        self.configure_policy(commands=[{'argv':['/usr/bin/true'],'exit_codes':[0,1]}]);self.exit_code=1
        source=await self.command_output()
        read=self.outputs.read_source(self.assignment,source,self.selection(),worker_actor=self.worker_actor)
        self.assertEqual(read['text'],'ordinary source/test output');self.assertFalse(read['effect_verified'])
        self.assertEqual(self.effects,1)

    async def test_home_reselection_denies_released_output(self):
        self.configure_policy();source=await self.command_output()
        released=self.outputs.release(source,self.selection(),snapshot=self.assignment,worker_actor=self.worker_actor)
        self.select('operator-thread',1)
        with self.assertRaises(Exception):
            self.outputs.read(self.assignment,released['artifact_id'],worker_actor=self.worker_actor)

    async def test_released_excerpt_cannot_launder_revocation_through_an_unreviewed_child(self):
        from codex_web.artifact_evidence import ArtifactCreate
        self.configure_policy();source=await self.command_output()
        released=self.outputs.release(source,self.selection(),snapshot=self.assignment,worker_actor=self.worker_actor)
        child=self.artifacts.create_artifact(ArtifactCreate(project_id='home',artifact_type='log',name='Further derivative',
            classification='internal',deny_model_context=False),actor=self.actor,source_artifact_ids=(released['artifact_id'],))
        governed=self.artifacts.governance.get_record(child.governance_record_id,self.actor)
        self.assertTrue(governed.deny_model_context)
        filtered=self.artifacts.governance.filter_context(ContextFilterRequest(record_ids=(governed.id,)),actor=self.actor)
        self.assertEqual(filtered.allowed_record_ids,())

    async def test_model_service_actor_cannot_manually_review_denied_output(self):
        source=await self.command_output()
        with self.assertRaises(GovernanceConflictError):
            self.outputs.preview(source,actor=self.worker_actor,expected_home_revision=1)
        with self.assertRaises(GovernanceConflictError):
            self.outputs.release(source,self.selection(),actor=self.worker_actor,classification='internal',
                reason='model cannot review itself',expected_home_revision=1)

    async def test_custody_change_during_decrypt_suppresses_plaintext(self):
        self.configure_policy();source=await self.command_output()
        released=self.outputs.release(source,self.selection(),snapshot=self.assignment,worker_actor=self.worker_actor)
        decrypt=self.crypto.decrypt
        def changed(*args,**kwargs):
            value=decrypt(*args,**kwargs)
            self.select('operator-thread',1)
            return value
        self.crypto.decrypt=changed
        with self.assertRaises(Exception):
            self.outputs.read(self.assignment,released['artifact_id'],worker_actor=self.worker_actor)

    async def test_preview_digest_and_current_home_revision_are_required_for_human_review(self):
        source=await self.command_output()
        with self.assertRaises(GovernanceConflictError):
            self.outputs.preview(source,actor=self.actor,expected_home_revision=2)
        wrong=HostOutputSelection(byte_end=8,expected_sha256='a'*64)
        with self.assertRaises(GovernanceConflictError):
            self.outputs.release(source,wrong,actor=self.actor,classification='internal',reason='reviewed',expected_home_revision=1)
        self.assertEqual(len(self.artifacts.list_artifacts(self.actor)),1)

    async def test_preview_key_revoked_during_decrypt_suppresses_plaintext(self):
        source=await self.command_output();decrypt=self.crypto.decrypt
        def changed(*args,**kwargs):
            value=decrypt(*args,**kwargs)
            self.crypto.revoke_key(self.output_key.id,'Private revocation fixture',actor=self.actor)
            return value
        self.crypto.decrypt=changed
        with self.assertRaises(GovernanceConflictError):
            self.outputs.preview(source,actor=self.actor,expected_home_revision=1)

    async def test_preview_source_invalidated_during_decrypt_suppresses_plaintext(self):
        source=await self.command_output();decrypt=self.crypto.decrypt
        def changed(*args,**kwargs):
            value=decrypt(*args,**kwargs)
            self.artifacts.invalidate_artifact(source,'Private invalidation fixture',actor=self.actor)
            return value
        self.crypto.decrypt=changed
        with self.assertRaises(GovernanceConflictError):
            self.outputs.preview(source,actor=self.actor,expected_home_revision=1)

    async def test_preview_old_key_version_revoked_during_decrypt_suppresses_plaintext(self):
        source=await self.command_output();self.crypto.rotate(self.output_key.id,actor=self.actor)
        decrypt=self.crypto.decrypt
        def changed(*args,**kwargs):
            value=decrypt(*args,**kwargs)
            self.crypto.revoke_version(self.output_key.id,1,'Private version revocation fixture',actor=self.actor)
            return value
        self.crypto.decrypt=changed
        with self.assertRaises(GovernanceConflictError):
            self.outputs.preview(source,actor=self.actor,expected_home_revision=1)

    async def test_default_native_read_needs_no_source_digest_and_obeys_current_policy_limit(self):
        self.configure_policy(max_excerpt_bytes=8);source=await self.command_output()
        result=self.outputs.read_source(self.assignment,source,worker_actor=self.worker_actor,max_bytes=4096)
        self.assertEqual(result['text'],'ordinary');self.assertEqual(result['classification'],'confidential')
        self.assertEqual(self.effects,1)
        self.assertEqual(len(self.artifacts.list_artifacts(self.actor)),2)

    async def test_default_native_read_is_denied_without_exact_current_policy(self):
        source=await self.command_output()
        with self.assertRaises(GovernanceConflictError):
            self.outputs.read_source(self.assignment,source,worker_actor=self.worker_actor)
        self.assertEqual(len(self.artifacts.list_artifacts(self.actor)),1)

    async def test_default_native_prefix_preserves_utf8_boundaries(self):
        self.configure_policy();self.stdout='testé-more';source=await self.command_output()
        result=self.outputs.read_source(self.assignment,source,worker_actor=self.worker_actor,max_bytes=5)
        self.assertEqual(result['text'],'test')

    async def test_default_native_empty_stream_returns_truthful_metadata_without_derivative(self):
        self.configure_policy();source=await self.command_output()
        result=self.outputs.read_source(self.assignment,source,worker_actor=self.worker_actor,stream='stderr')
        self.assertEqual(result['text'],'');self.assertIs(result['empty_stream'],True)
        self.assertEqual(result['available_bytes'],0);self.assertEqual(result['selected_bytes'],0)
        self.assertIsNone(result['artifact_id']);self.assertFalse(result['effect_verified'])
        self.assertEqual(len(self.artifacts.list_artifacts(self.actor)),1)

    async def test_default_native_nonempty_stream_with_too_small_utf8_limit_is_not_labeled_empty(self):
        self.configure_policy();self.stdout='é';source=await self.command_output()
        result=self.outputs.read_source(self.assignment,source,worker_actor=self.worker_actor,max_bytes=1)
        self.assertEqual(result['text'],'');self.assertIs(result['empty_stream'],False)
        self.assertEqual(result['available_bytes'],2);self.assertEqual(result['selected_bytes'],0)
        self.assertEqual(len(self.artifacts.list_artifacts(self.actor)),1)

    async def test_empty_native_read_rechecks_policy_after_decryption(self):
        self.configure_policy();source=await self.command_output();decrypt=self.crypto.decrypt
        def changed(*args,**kwargs):
            value=decrypt(*args,**kwargs)
            self.publish_policy(max_excerpt_bytes=512)
            return value
        self.crypto.decrypt=changed
        with self.assertRaises(Exception):
            self.outputs.read_source(self.assignment,source,worker_actor=self.worker_actor,stream='stderr')

    async def test_human_output_list_contains_only_current_receipt_metadata_not_output_or_argv(self):
        import json
        source=await self.command_output()
        result=self.outputs.list_sources(actor=self.actor,expected_home_revision=1)
        self.assertEqual(len(result['items']),1)
        self.assertEqual(result['items'][0]['artifact_id'],source)
        self.assertEqual(result['items'][0]['exit_code'],0)
        self.assertNotIn('ordinary source/test output',json.dumps(result))
        self.assertNotIn('/usr/bin/true',json.dumps(result))
        with self.assertRaises(GovernanceConflictError):
            self.outputs.list_sources(actor=self.actor,expected_home_revision=2)
        with self.assertRaises(GovernanceConflictError):
            self.outputs.list_sources(actor=self.worker_actor,expected_home_revision=1)

    async def test_human_router_lists_refs_no_store_and_refuses_service_actor(self):
        from fastapi import FastAPI
        import httpx
        from codex_web.api.home_host_output import build_home_host_output_router
        source=await self.command_output();app=FastAPI();current=[self.actor]
        @app.middleware('http')
        async def fixture_auth(request,call_next):
            request.state.identity_actor=current[0]
            return await call_next(request)
        app.include_router(build_home_host_output_router(self.outputs))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://private-fixture')as client:
            response=await client.get('/api/home/operator/host-outputs?expected_home_revision=1')
            self.assertEqual(response.status_code,200);self.assertEqual(response.headers['cache-control'],'no-store')
            self.assertEqual(response.json()['items'][0]['artifact_id'],source)
            current[0]=self.worker_actor
            response=await client.get('/api/home/operator/host-outputs?expected_home_revision=1')
            self.assertEqual(response.status_code,403)


class HomeOutputPolicyTests(unittest.TestCase):
    def test_governance_state_migrates_legacy_but_old_contract_refuses_release_reader(self):
        from codex_web.storage.data_governance import DataGovernanceStore
        from codex_web.compatibility import ContractSpec
        store=object.__new__(DataGovernanceStore)
        for version in ('0.0','1.0'):
            state=store._decode({'schema_version':version,'records':[],'requests':[],'events':[]})
            self.assertEqual(state.schema_version,'1.1')
            with self.assertRaises(Exception):
                ContractSpec('data-governance-state','1.0',('1.0',)).require(state.schema_version)

    def test_rules_cannot_coerce_exit_bools_or_grant_secret_or_unbounded_outputs(self):
        base={'organization_id':'o','workspace_id':'w','administrator_id':'h','agent_profile_id':'p',
            'agent_profile_revision':1,'host_resource_id':'r','trusted_worker_ids':['worker'],
            'commands':[{'argv':['/usr/bin/true']}]}
        for changes in ({'classification':'secret'},{'max_excerpt_bytes':65537},{'agent_profile_revision':True},
                {'commands':[{'argv':['relative']}]},{'commands':[{'argv':['/usr/bin/true'],'exit_codes':[True]}]}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                HomeOutputReleasePolicy.model_validate({**base,**changes})


def load_tests(loader,tests,pattern):
    names=[n for n in HomeHostOutputTests.__dict__ if n.startswith('test_')]
    return unittest.TestSuite([*(HomeHostOutputTests(n) for n in names),
        loader.loadTestsFromTestCase(HomeOutputPolicyTests)])
