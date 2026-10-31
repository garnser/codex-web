"""Versioned validity pins and real governed read-only execution; no native/model I/O."""
from contextlib import ExitStack
import json
import time
import unittest
from unittest.mock import patch

from codex_web.configuration import ConfigurationDraftCreate, ConfigurationPublishRequest, ConfigurationResetRequest
from codex_web.definitions import DefinitionReference
from codex_web.mr_observation_policy import (OBSERVATION_WINDOW, OBSERVATION_V2, CONTINUATION_V2,
    INPUT_V2, READINESS_V2, ObservationPolicyError, resolve_policy, validate_policy)
from codex_web.services.configuration import ConfigurationService
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.skills import SkillUpdate, SkillPublish
from tests import test_governed_mr_procedure_runs as fixture


class MRObservationPolicyTests(fixture.GovernedMRProcedureRunTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.configuration = ConfigurationService(ConfigurationRegistryStore(self.sqlite))
        self.configuration.register_spec(OBSERVATION_WINDOW)
        self.service.mr_observation_configuration = self.configuration
        self.evidence.mr_observation_configuration = self.configuration
        self.runner.mr_observation_configuration = self.configuration
        self.intents.execution.observation_policy_resolver = self.runner.observation_policy_for_action

    def configure(self, seconds):
        draft = self.configuration.create_draft(ConfigurationDraftCreate(key=OBSERVATION_WINDOW.key,
            scope_type='deployment', value=seconds, actor=self.admin.identity_id))
        return self.configuration.publish(draft.id, ConfigurationPublishRequest(actor=self.admin.identity_id))

    async def v2_skill(self):
        record = self.registry.get_record(self.pin.record_id)
        procedure = dict(record.payload['operational_procedure'])
        procedure.update(schema_version='1.3', input_protocol=INPUT_V2)
        draft = self.skills.update(record.definition_id, SkillUpdate(operational_procedure=procedure,reason='Private versioned protocol opt-in'), actor=self.admin)
        published = self.skills.publish(draft['skillId'], draft['recordId'], SkillPublish(), actor=self.admin)
        self.pin = DefinitionReference.model_validate(published['definitionReference'])
        # Fixture is an unused canonical generic assignment; opt in to the
        # newly published Skill before opening its replacement broker scope.
        def bind(state):
            state.assignments[0].skill_refs = (self.pin,)
            return state
        self.workers.store.update_assignment(self.worker.id, self.assignment.id, bind)
        from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
        validator = self.broker.validator
        await self.broker.stop()
        current = self.workers.store.assignment(self.assignment.id)
        self.broker = AssignmentBoundControlPlaneBroker(self.service, assignment=current,
            worker_id=self.worker.id, worker_actor=self.worker_actor, fence=current.fence, validator=validator)
        await self.broker.start()

    async def publish_v2(self, request_id='d'):
        status, _, result = await self.post({**self.request, 'method':CONTINUATION_V2, 'request_id':request_id*32})
        self.assertEqual(status,200,result)
        return result

    def clock(self, base, offset):
        from codex_web.action_intents import ActionIntentReceipt, ActionIntentVerificationReceipt
        from codex_web.services.gitlab_merge_requirements import GitLabMergeRequirementsReader
        constructor = GitLabMergeRequirementsReader.__init__
        now = lambda: base + offset[0]
        def reader_clock(reader, *args, **kwargs):
            return constructor(reader, *args, **{**kwargs, 'clock':now})
        def receipt_clock(**values): return ActionIntentReceipt(received_at=now(), **values)
        def verification_clock(**values): return ActionIntentVerificationReceipt(verified_at=now(), **values)
        stack = ExitStack()
        session = getattr(self.broker.validator, '__self__', None)
        if session is not None:
            stack.enter_context(patch.object(session, '_clock', side_effect=now))
        for guard in (patch('time.time',side_effect=now), patch.object(self.service,'_clock',side_effect=now),
            patch.object(self.runner,'clock',side_effect=now),
            patch.object(GitLabMergeRequirementsReader,'__init__',reader_clock),
            patch('codex_web.services.action_intents.ActionIntentReceipt',receipt_clock),
            patch('codex_web.services.action_intents.ActionIntentVerificationReceipt',verification_clock)):
            stack.enter_context(guard)
        return stack

    async def test_v2_real_publish_start_readonly_receipt_evidence_after_47_seconds(self):
        publication = self.configure(120)
        await self.v2_skill()
        offset=[0];base=time.time()
        with self.clock(base,offset):
            result=await self.publish_v2()
            ids={p['id'] for p in result['evidence'].values()}
            for proof in result['evidence'].values():
                policy=json.loads(proof['metadata']['observation_policy'])
                self.assertEqual(policy['record_id'],publication.id)
                self.assertEqual(policy['seconds'],120)
                self.assertEqual(proof['metadata']['verification_method'],CONTINUATION_V2)
            offset[0]=47
            started=await self.start_snapshot(result['procedure_input']['id'])
            checkpoint=started.procedure_checkpoint
            advanced=await self.advance_run(checkpoint.id)
            step=advanced.procedure_checkpoint.steps['observe']
            self.assertEqual(step.status,'succeeded',advanced.procedure_checkpoint.failure_code)
            intent=self.intents.get(step.action_intent_id,self.admin)
            history=self.intents.store.load(intent_id=intent.id)
            self.assertEqual(len(history.receipts),1)
            receipt=history.receipts[0]
            self.assertEqual(receipt.result.output['observation_version'],'2.0')
            output_policy=receipt.result.output['observation_policy']
            self.assertEqual(output_policy['record_id'],publication.id)
            self.assertEqual(output_policy['resolved_at'],base+47)
            state=self.evidence.store.load()
            readonly=[e for e in state.evidence if e.metadata.get('verification_method')==READINESS_V2]
            self.assertTrue(readonly)
            self.assertTrue(ids.isdisjoint({e.id for e in readonly}))
            self.assertTrue(all(json.loads(e.metadata['observation_policy'])==output_policy for e in readonly))
            completed=await self.advance_run(checkpoint.id)
            self.assertEqual(completed.procedure_checkpoint.status,'completed')
            self.assertEqual(completed.procedure_checkpoint.input_snapshot_id,result['procedure_input']['id'])
            # Missing independent approval remains blocked; no merge/model call.
            self.assertEqual(json.loads(self.content(step.output_snapshot_id))['readiness'],'blocked')
            self.runner.models.invoke.assert_not_called()

    async def test_v1_stays_30_and_cannot_use_v2_input(self):
        self.configure(120)
        result=await self.publish('b')
        for proof in result['evidence'].values():
            self.assertNotIn('observation_policy',proof['metadata'])
            self.assertLessEqual(proof['metadata']['observation_expires_at']-proof['observed_at'],30)
        offset=[0];base=time.time()
        with self.clock(base,offset):
            v1=await self.publish('c')
            offset[0]=47
            status,_,body=await self._request('POST',f'/api/execution-workers/assignments/{self.assignment.id}/procedure/start',
                payload={'definition':self.pin.model_dump(mode='json'),'input_snapshot_id':v1['procedure_input']['id']})
            self.assertEqual(status,409,body)
            self.assertIsNone(self.workers.store.assignment(self.assignment.id).procedure_checkpoint)
        v2=await self.publish_v2()
        status,_,body=await self._request('POST',f'/api/execution-workers/assignments/{self.assignment.id}/procedure/start',
            payload={'definition':self.pin.model_dump(mode='json'),'input_snapshot_id':v2['procedure_input']['id']})
        self.assertEqual(status,409,body)
        self.assertIsNone(self.workers.store.assignment(self.assignment.id).procedure_checkpoint)

    async def test_policy_bounds_scope_default_pinning_supersession_and_revocation(self):
        from codex_web.services.configuration import ConfigurationError
        for value in (0,301,True,120.5,'120'):
            with self.assertRaises((ConfigurationError,ValueError)):
                self.configure(value)
        with self.assertRaises((ConfigurationError,ValueError)):
            self.configuration.create_draft(ConfigurationDraftCreate(key=OBSERVATION_WINDOW.key,
                scope_type='project',scope_id='project-a',value=120,actor=self.admin.identity_id))
        default=resolve_policy(self.configuration,now=time.time())
        self.assertEqual(default.seconds,30)
        first=self.configure(180)
        original=resolve_policy(self.configuration,now=time.time())
        self.configure(300)
        current=resolve_policy(self.configuration,now=time.time())
        self.assertEqual(current.seconds,300)
        self.assertNotEqual(current.record_id,original.record_id)
        self.assertEqual(validate_policy(original,self.configuration,now=time.time()).seconds,180)
        forged=original.model_dump();forged['seconds']=300
        with self.assertRaises(ObservationPolicyError):validate_policy(forged,self.configuration,now=time.time())
        forged=default.model_copy(update={'resolved_at':time.time()})
        with self.assertRaises(ObservationPolicyError):validate_policy(forged,self.configuration,now=time.time())
        self.configuration.reset_override(ConfigurationResetRequest(key=OBSERVATION_WINDOW.key,
            scope_type='deployment',actor=self.admin.identity_id))
        self.configure(300)
        with self.assertRaises(ObservationPolicyError):validate_policy(original,self.configuration,now=time.time())
        with self.assertRaises(ObservationPolicyError):validate_policy(default,self.configuration,now=time.time())
        self.assertEqual(original.record_id,first.id)
        self.assertEqual(original.seconds,180)

    async def test_v2_policy_revocation_before_start_no_new_provider_effects(self):
        self.configure(120);await self.v2_skill()
        result=await self.publish_v2()
        calls=len(self.observed_calls)
        self.configuration.reset_override(ConfigurationResetRequest(key=OBSERVATION_WINDOW.key,
            scope_type='deployment',actor=self.admin.identity_id))
        status,_,body=await self._request('POST',f'/api/execution-workers/assignments/{self.assignment.id}/procedure/start',
            payload={'definition':self.pin.model_dump(mode='json'),'input_snapshot_id':result['procedure_input']['id']})
        self.assertEqual(status,409,body)
        self.assertIsNone(self.workers.store.assignment(self.assignment.id).procedure_checkpoint)
        self.assertEqual(len(self.observed_calls),calls)

    async def test_unknown_or_spoofed_version_never_falls_through_generic_verification(self):
        self.configure(120)
        calls=len(self.observed_calls)
        for method in ('code-host-mr-observation.v99','code-host-readiness-observation.v2'):
            status,_,body=await self.post({**self.request,'method':method,'request_id':'e'*32})
            self.assertEqual(status,409,body)
        status,_,body=await self.post({**self.request,'method':OBSERVATION_V2,'request_id':'f'*32,
            'observation_policy':{'seconds':300}})
        self.assertEqual(status,403,body)
        self.assertEqual(body['error']['code'],'control_plane_broker_denied')
        self.assertEqual(len(self.observed_calls),calls)
        self.assertEqual(self.evidence.store.load().verifications,[])


    async def test_forward_reader_boundary_and_unknown_evidence_method(self):
        from codex_web.artifact_evidence import ArtifactEvidenceState, Evidence
        from codex_web.compatibility import ContractSpec, ContractCompatibilityError
        from codex_web.storage.artifact_evidence import ARTIFACT_EVIDENCE_MIGRATIONS
        from codex_web.mr_observation_evidence import current_observation
        original=ArtifactEvidenceState(evidence=[Evidence(organization_id='local',workspace_id='default',
            producer_identity_id='original',evidence_type='review',result='pass',observed_at=1,
            metadata={'original':'untouched'})]).model_dump(mode='json')
        original['schema_version']='1.4'
        migrated=ARTIFACT_EVIDENCE_MIGRATIONS.migrate(original,from_version='1.4',to_version='1.5')
        self.assertEqual(migrated,{**original,'schema_version':'1.5'})
        old=ContractSpec('artifact-evidence-state','1.4',('1.0','1.1','1.2','1.3','1.4'))
        with self.assertRaises(ContractCompatibilityError):old.require(migrated['schema_version'])
        proof=Evidence(organization_id='local',workspace_id='default',producer_identity_id='original',
            evidence_type='review',result='pass',observed_at=1,
            metadata={'verification_method':'code-host-mr-observation.v99','observation_expires_at':100})
        self.assertFalse(current_observation(proof,now=2,configuration=self.configuration))
        # The existing generated editor/schema surfaces expose canonical bounds
        # and protocols, without an independent UI value or published Skill.
        self.assertFalse(OBSERVATION_WINDOW.grants_authority)
        self.assertEqual((OBSERVATION_WINDOW.minimum,OBSERVATION_WINDOW.maximum),(1,300))
        from codex_web.skill_procedures import OperationalProcedure
        schema=OperationalProcedure.model_json_schema()
        self.assertIn('1.3',schema['properties']['schema_version']['enum'])
        self.assertIn(INPUT_V2,str(schema['properties']['input_protocol']))


    async def test_both_provider_readers_keep_io_30_and_identical_requests_with_long_validity(self):
        from tests import test_gitlab_merge_requirements as gitlab_fixture
        from tests import test_github_merge_requirements as github_fixture
        from codex_web.services.gitlab_merge_requirements import GitLabMergeRequirementsReader
        from codex_web.services.github_merge_requirements import GitHubMergeRequirementsReader
        from codex_web.integrations.gitlab_client import GitLabClient
        from codex_web.integrations.github_client import GitHubClient
        import asyncio
        import httpx
        io_timeout=asyncio.timeout
        for provider in ('gitlab','github'):
            timeout_calls=[];reads=[]
            if provider=='gitlab':
                f=gitlab_fixture.GitLabMergeRequirementsTests('test_native_gates_are_useful_without_claiming_full_paid_policy');f.setUp()
                reader=GitLabMergeRequirementsReader;client=GitLabClient(transport=httpx.MockTransport(f.response))
                token='fixture-secret';calls=f.calls
            else:
                f=github_fixture._Provider();reader=GitHubMergeRequirementsReader
                client=GitHubClient(transport=httpx.MockTransport(f.response));token='fixture-token';calls=f.calls
            def timeout(seconds):
                timeout_calls.append(seconds)
                return io_timeout(seconds)
            for duration in (30,300):
                calls.clear()
                with patch('asyncio.timeout',side_effect=timeout):
                    fact=await reader(client,f.binding,f.resource,'acme/widgets',token,
                        clock=lambda:1000,observation_seconds=duration).read('7')
                self.assertEqual(fact.expires_at-fact.observed_at,duration)
                reads.append([(r.method,str(r.url),r.content) for r in calls])
            self.assertEqual(reads[0],reads[1])
            self.assertEqual(timeout_calls,[30,30])


    async def test_v2_policy_reset_at_provider_boundary_stops_reads_and_result_evidence(self):
        self.configure(120);await self.v2_skill()
        result=await self.publish_v2()
        started=await self.start_snapshot(result['procedure_input']['id'])
        before_calls=len(self.observed_calls)
        before_evidence={e.id for e in self.evidence.store.load().evidence}
        def revoke():
            self.hook=None
            self.configuration.reset_override(ConfigurationResetRequest(key=OBSERVATION_WINDOW.key,
                scope_type='deployment',actor=self.admin.identity_id))
        self.hook=revoke
        advanced=await self.advance_run(started.procedure_checkpoint.id)
        self.assertNotEqual(advanced.procedure_checkpoint.steps['observe'].status,'succeeded')
        self.assertEqual(len(self.observed_calls),before_calls+1)
        self.assertEqual({e.id for e in self.evidence.store.load().evidence},before_evidence)
        self.assertIsNone(advanced.procedure_checkpoint.steps['observe'].output_snapshot_id)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(MRObservationPolicyTests(name)
        for name in MRObservationPolicyTests.__dict__ if name.startswith('test_'))
