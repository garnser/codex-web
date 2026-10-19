"""Canonical SQLite/provider/worker fixtures, real concurrency and HTTP boundary."""
import asyncio
import json
import unittest
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from tests import test_procedure_execution as execution_fixtures
from codex_web.api.procedure_outcomes import build_procedure_outcomes_router
from codex_web.services.procedure_outcomes import ProcedureOutcomeService
from codex_web.services.organizational_memory import OrganizationalMemoryService
from codex_web.storage.organizational_memory import OrganizationalMemoryStore
from codex_web.services.definitions import DefinitionConflictError
from codex_web.services.skills import SkillConflict
from codex_web.skills import SkillUpdate, SkillPublish
from codex_web.data_governance import GovernanceActionRequest


class _ProcedureOutcomeFixture:
    asyncSetUp = execution_fixtures.ProcedureExecutionTests.asyncSetUp
    tearDown = execution_fixtures.ProcedureExecutionTests.tearDown
    configure = execution_fixtures.ProcedureExecutionTests.configure
    rebuild = execution_fixtures.ProcedureExecutionTests.rebuild
    artifact = execution_fixtures.ProcedureExecutionTests.artifact
    proof = execution_fixtures.ProcedureExecutionTests.proof
    advance = execution_fixtures.ProcedureExecutionTests.advance
    finish = execution_fixtures.ProcedureExecutionTests.finish

    def outcomes(self):
        memory = OrganizationalMemoryService(OrganizationalMemoryStore(self.sqlite),
            self.artifacts.governance, self.authority, artifact_evidence=self.artifacts)
        self.artifacts.organizational_memory = memory
        for kind in ('artifact','evidence'):
            self.artifacts.governance.register_action_handler(kind,self.artifacts.apply_governance_action)
        self.outcomes_service = ProcedureOutcomeService(self.runner, memory)
        return self.outcomes_service

    def scan(self,draft):
        from codex_web.skill_security import SkillScanRequest
        return self.skills.security.scan(draft,SkillScanRequest(),actor=self.actor)

class ProcedureOutcomeTests(_ProcedureOutcomeFixture, unittest.IsolatedAsyncioTestCase):
    def configure(self, content):
        # Outcome policy needs real provider effects and verification, not a new
        # diagnostic/rollback qualification before every publication or erasure.
        content = super().configure(content)
        steps = {step['id']: step for step in content['steps']}
        steps['change']['preconditions'] = [
            {'source': 'step', 'name': 'backup', 'predicate': 'succeeded'}]
        steps['ready']['failure'].update(outcome='escalate')
        steps['ready']['failure'].pop('recovery_step_id')
        content['steps'] = [steps[name] for name in ('backup', 'change', 'ready')]
        return content

    async def test_preseeded_memory_cannot_supply_unverified_observations(self):
        from codex_web.organizational_memory import KnowledgeCreate, KnowledgeProvenance
        await self.finish()
        service = self.outcomes()
        assignment, _, report, _, _ = service.extract(self.assignment.id, actor=self.actor)
        report['observations'] = [{'cause': 'Unverified library hypothesis'}]
        service.memory.create(KnowledgeCreate(
            logical_key='procedure-outcome/' + service._identity(assignment),
            object_type='procedure', title='Operator opinion', summary='Unverified opinion',
            content=json.dumps(report, sort_keys=True), project_id=assignment.project_id,
            provenance=KnowledgeProvenance(source_kind='manual', source_ref='operator-opinion')),
            actor=self.actor)
        with self.assertRaisesRegex(SkillConflict, 'Memory source changed'):
            service.convert(self.assignment.id, actor=self.actor)
        self.assertEqual(len(self.skills.revisions(self.pin.definition_id, actor=self.actor)), 1)
        self.assertFalse([r for r in service.memory.governance.store.load().records
                          if r.object_type == 'procedure_skill'])

    async def test_revised_memory_conflicts_even_with_identical_content(self):
        from codex_web.organizational_memory import KnowledgeRevise
        await self.finish()
        service = self.outcomes()
        result = service.convert(self.assignment.id, actor=self.actor)
        knowledge = service.memory.get(result['knowledge_id'], actor=self.actor)
        service.memory.revise(knowledge.id, KnowledgeRevise(
            provenance=knowledge.provenance, reason='Operator review'), actor=self.actor)
        with self.assertRaisesRegex(SkillConflict, 'Memory source changed'):
            service.convert(self.assignment.id, actor=self.actor)
        self.assertEqual(len(self.skills.revisions(self.pin.definition_id, actor=self.actor)), 2)

    async def test_concurrent_edit_and_newer_publication_conflict(self):
        await self.finish()
        service = self.outcomes()
        edited = self.skills.update(self.pin.definition_id, SkillUpdate(instructions='Local reviewed change.',
            reason='operator edit'), actor=self.actor)
        with self.assertRaises(DefinitionConflictError):
            service.convert(self.assignment.id, actor=self.actor)
        self.skills.publish(self.pin.definition_id, edited['recordId'], SkillPublish(), actor=self.actor)
        with self.assertRaises(DefinitionConflictError):
            service.convert(self.assignment.id, actor=self.actor)
        self.assertEqual(self.skills.get(self.pin.definition_id,actor=self.actor)['recordId'],edited['recordId'])

    async def test_unverified_and_failed_steps_require_review(self):
        service = self.outcomes()
        result = service.convert(self.assignment.id, actor=self.actor)
        self.assertIsNone(result['draft'])
        self.provider.fail = True
        await self.finish()
        result = service.convert(self.assignment.id, actor=self.actor)
        self.assertEqual(result['status'],'review_required')
        self.assertIsNone(result['draft'])
        self.assertEqual(len(self.skills.revisions(self.pin.definition_id,actor=self.actor)),1)

    async def test_terminal_checkpoint_cannot_prove_an_unverified_provider_effect(self):
        await self.finish()
        service=self.outcomes()
        intent_id=self.runner.get(self.assignment.id,self.actor).procedure_checkpoint.steps['change'].action_intent_id
        def unverify(state):
            from codex_web.action_intents import ActionIntentStatus
            for intent in state.intents:
                if intent.id==intent_id:intent.status=ActionIntentStatus.UNCERTAIN
            return state
        self.intents.store.update(unverify,intent_ids=(intent_id,))
        result=service.convert(self.assignment.id,actor=self.actor)
        self.assertEqual(result['status'],'review_required')
        self.assertIn('effect_not_verified:change',result['report']['review_needs'])
        self.assertIsNone(result['draft'])

    async def test_quarantined_source_requires_review_even_for_explicit_fork(self):
        await self.finish()
        service=self.outcomes()
        self.skills.definitions.quarantine(self.pin.record_id,actor=self.actor.identity_id,reason='source revoked')
        result=service.convert(self.assignment.id,actor=self.actor,skill_id='explicit-new-procedure')
        self.assertEqual(result['status'],'review_required')
        self.assertIn('procedure_quarantined',result['report']['review_needs'])
        self.assertIsNone(result['draft'])

    async def test_secret_log_text_not_copied_and_deleted_source_erases_descendants(self):
        await self.finish()
        service = self.outcomes()
        marker='token=super-sensitive-log-value-never-copy'
        def logs(state):
            for proof in state.evidence:
                proof.summary=marker+'\n'+('irrelevant log line\n'*100)
                proof.metadata['diagnostic_signature']='SIGABRT-library-crash-unconfirmed'
            return state
        self.artifacts.store.update(logs)
        result=service.convert(self.assignment.id,actor=self.actor)
        self.assertNotIn(marker,json.dumps(result))
        self.assertTrue(result['report']['diagnostic_signatures'])
        self.assertNotIn('SIGABRT-library-crash-unconfirmed',json.dumps(result))
        knowledge=service.memory.get(result['knowledge_id'],actor=self.actor)
        self.assertNotIn(marker,knowledge.content)
        draft=result['draft']
        self.scan(draft)
        self.skills.publish(draft['skillId'],draft['recordId'],SkillPublish(),actor=self.actor)
        edited=self.skills.update(draft['skillId'],SkillUpdate(instructions='Derived local edits',reason='edit'),actor=self.actor)
        source=self.artifacts.get_artifact(self.snapshot.id,actor=self.actor)
        governance=self.artifacts.governance
        request=governance.request_action(GovernanceActionRequest(record_id=source.governance_record_id,
            action='delete',reason='source erasure'),actor=self.actor)
        governance.execute_request(request.id,actor=self.actor)
        removed=service.memory.store.get(knowledge.id)
        self.assertEqual(removed.provenance.evidence_ids,())
        self.assertNotIn('fixture-execution',removed.provenance.source_ref)
        for record_id in [draft['recordId'],edited['recordId']]:
            record=self.skills.definitions.get_record(record_id)
            self.assertEqual(record.lifecycle,'quarantined')
            self.assertIsNone(record.payload['operational_procedure'])
            self.assertNotIn(self.assignment.id,json.dumps(record.payload))
            self.assertEqual(record.payload['provenance']['evidence_ids'],[])
        from codex_web.skills import SkillLifecycle, SkillLifecycleChange
        with self.assertRaises(DefinitionConflictError):
            self.skills.lifecycle(draft['skillId'],SkillLifecycle.ACTIVE,
                SkillLifecycleChange(reason='cannot resurrect erased source'),actor=self.actor)

    async def test_stale_evidence_blocks_publication_through_generic_registry(self):
        await self.finish()
        service=self.outcomes()
        result=service.convert(self.assignment.id,actor=self.actor)
        proof_id=result['draft']['skill']['provenance']['evidence_ids'][0]
        def stale(state):
            for proof in state.evidence:
                if proof.id==proof_id:
                    proof.retention_expires_at=1
            return state
        self.artifacts.store.update(stale)
        from codex_web.definitions import DefinitionPublishRequest
        from codex_web.services.definitions import DefinitionGateDeniedError
        with self.assertRaises(DefinitionGateDeniedError):
            self.skills.definitions.publish(result['draft']['recordId'],DefinitionPublishRequest(actor=self.actor.identity_id))
        self.assertEqual(service.convert(self.assignment.id,actor=self.actor)['status'],'review_required')

    async def test_api_scope_inspection_conversion_and_conflict(self):
        await self.finish()
        service=self.outcomes()
        app=FastAPI()
        actor=self.actor
        @app.middleware('http')
        async def authenticate(request:Request,call_next):
            request.state.identity_actor=actor
            return await call_next(request)
        app.include_router(build_procedure_outcomes_router(service))
        base=f'/api/execution-workers/assignments/{self.assignment.id}/procedure'
        with TestClient(app) as client:
            response=client.get(base+'/outcome')
            self.assertEqual(response.status_code,200,response.text)
            response=client.post(base+'/outcome-draft',json={})
            self.assertEqual(response.status_code,200,response.text)
            inspected=client.get(base+'/outcome').json()
            self.assertEqual(len(inspected['drafts']),1)
            self.assertTrue(inspected['publication_required'])
            self.assertEqual(response.json()['draft']['definitionLifecycle'],'draft')
            response=client.post(base+'/outcome-draft',json={})
            self.assertEqual(response.status_code,200,response.text)
            actor=actor.model_copy(update={'workspace_id':'other'})
            self.assertEqual(client.get(base+'/outcome').status_code,409)

    async def test_completion_event_retry_and_failure_attention(self):
        service=self.outcomes()
        self.runner.outcome_drafts=service
        await self.finish()
        await self.advance()
        self.assertEqual(len(self.skills.revisions(self.pin.definition_id,actor=self.actor)),2)

    async def test_process_crash_after_sql_draft_write_rolls_back_all_domains_and_retry(self):
        await self.finish()
        service=self.outcomes()
        original=self.sqlite._upsert
        def crash():
            def write(connection, namespace, payload):
                original(connection,namespace,payload)
                if namespace==self.skills.definitions.store.namespace:
                    os._exit(73)  # actual SQL INSERT/UPDATE, before transaction commit
            self.sqlite._upsert=write
            service.convert(self.assignment.id,actor=self.actor)
        process=multiprocessing.get_context('fork').Process(target=crash)
        process.start()
        await asyncio.to_thread(process.join,15)
        self.assertEqual(process.exitcode,73)
        self.assertEqual(len(self.skills.revisions(self.pin.definition_id,actor=self.actor)),1)
        self.assertFalse(service.memory.store.load().records)
        self.assertFalse([r for r in service.memory.governance.store.load().records
            if r.object_type=='procedure_skill'])
        result=service.convert(self.assignment.id,actor=self.actor)
        self.assertEqual(result['status'],'draft')
        service.convert(self.assignment.id,actor=self.actor)
        self.assertEqual(len([r for r in service.memory.governance.store.load().records
            if r.object_type=='procedure_skill']),1)

    async def test_independent_processes_many_deliveries_one_draft_and_one_governance(self):
        await self.finish()
        service=self.outcomes()
        def deliver():
            # Every process has independent domain services and SQLite connections.
            for _ in range(3):
                service.convert(self.assignment.id,actor=self.actor)
        processes=[multiprocessing.get_context('fork').Process(target=deliver) for _ in range(4)]
        for process in processes:process.start()
        for process in processes:
            await asyncio.to_thread(process.join,20)
            self.assertEqual(process.exitcode,0)
        self.assertEqual(len(self.skills.revisions(self.pin.definition_id,actor=self.actor)),2)
        self.assertEqual(len(service.memory.store.load().records),1)
        self.assertEqual(len([r for r in service.memory.governance.store.load().records
            if r.object_type=='procedure_skill']),1)

    async def test_source_erasure_during_conversion_cannot_escape_dependency(self):
        await self.finish()
        service=self.outcomes()
        snapshot=self.artifacts.get_artifact(self.snapshot.id,actor=self.actor)
        original=self.sqlite._upsert
        context=multiprocessing.get_context('fork')
        sql_written=context.Event()
        def convert():
            def write(connection,namespace,payload):
                original(connection,namespace,payload)
                if namespace==self.skills.definitions.store.namespace:
                    sql_written.set()
            self.sqlite._upsert=write
            service.convert(self.assignment.id,actor=self.actor)
        def erase():
            if not sql_written.wait(15):os._exit(74)
            g=self.artifacts.governance
            request=g.request_action(GovernanceActionRequest(record_id=snapshot.governance_record_id,
                action='redact',reason='concurrent source erasure'),actor=self.actor)
            g.execute_request(request.id,actor=self.actor)
        processes=[context.Process(target=erase),context.Process(target=convert)]
        for p in processes:p.start()
        for p in processes:
            await asyncio.to_thread(p.join,20)
            self.assertEqual(p.exitcode,0)
        records=self.skills.revisions(self.pin.definition_id,actor=self.actor)
        self.assertEqual(len(records),2)
        self.assertEqual(records[-1]['definitionLifecycle'],'quarantined')
        self.assertNotIn(self.assignment.id,json.dumps(records[-1]['skill']))
        self.assertEqual(service.convert(self.assignment.id,actor=self.actor)['status'],'review_required')

    async def test_pending_source_erasure_prevents_atomic_draft_commit(self):
        await self.finish()
        service=self.outcomes()
        proof_id=next(iter(self.runner.get(self.assignment.id,self.actor).procedure_checkpoint.steps['ready'].evidence.values()))
        proof=self.artifacts.get_evidence(proof_id,actor=self.actor)
        service.memory.governance.request_action(GovernanceActionRequest(record_id=proof.governance_record_id,
            action='delete',reason='erasure pending'),actor=self.actor)
        with self.assertRaises(SkillConflict):
            service.convert(self.assignment.id,actor=self.actor)
        self.assertEqual(len(self.skills.revisions(self.pin.definition_id,actor=self.actor)),1)
        self.assertFalse(service.memory.store.load().records)

    async def test_lost_commit_response_retries_existing_revision_and_governance(self):
        await self.finish()
        service=self.outcomes()
        original=self.sqlite.update_many
        def lose_response(defaults,updater):
            original(defaults,updater)
            raise ConnectionError('controlled response lost after commit')
        self.sqlite.update_many=lose_response
        try:
            with self.assertRaises(ConnectionError):service.convert(self.assignment.id,actor=self.actor)
        finally:
            self.sqlite.update_many=original
        result=service.convert(self.assignment.id,actor=self.actor)
        self.assertEqual(result['status'],'draft')
        self.assertEqual(len(self.skills.revisions(self.pin.definition_id,actor=self.actor)),2)
        self.assertEqual(len([r for r in service.memory.governance.store.load().records
            if r.object_type=='procedure_skill']),1)

    async def test_publication_is_explicit_and_newer_draft_cannot_be_silently_replaced(self):
        await self.finish()
        service=self.outcomes()
        draft=service.convert(self.assignment.id,actor=self.actor)['draft']
        from codex_web.services.skill_security import SkillSecurityBlocked
        with self.assertRaises(SkillSecurityBlocked):
            self.skills.publish(draft['skillId'],draft['recordId'],SkillPublish(),actor=self.actor)
        self.scan(draft)
        published=self.skills.publish(draft['skillId'],draft['recordId'],SkillPublish(),actor=self.actor)
        self.assertEqual(published['definitionLifecycle'],'published')
        self.assertFalse(service.inspect(self.assignment.id,actor=self.actor)['publication_required'])
        self.assertEqual(self.runner.get(self.assignment.id,self.actor).procedure_checkpoint.definition,self.pin)
        newer=self.skills.update(draft['skillId'],SkillUpdate(instructions='Operator revision',reason='edit'),actor=self.actor)
        with self.assertRaises(DefinitionConflictError):
            service.convert(self.assignment.id,actor=self.actor)
        from codex_web.definitions import DefinitionPublishRequest
        from codex_web.services.definitions import DefinitionGateDeniedError
        with self.assertRaises(DefinitionConflictError):
            self.skills.definitions.publish(draft['recordId'],DefinitionPublishRequest(actor=self.actor.identity_id))
        self.assertEqual(self.skills.get(draft['skillId'],actor=self.actor)['recordId'],newer['recordId'])

    async def test_edit_cannot_drop_source_scan_gate_and_reviewed_descendant_can_publish(self):
        await self.finish()
        service=self.outcomes()
        draft=service.convert(self.assignment.id,actor=self.actor)['draft']
        self.scan(draft)
        self.skills.publish(draft['skillId'],draft['recordId'],SkillPublish(),actor=self.actor)
        edited=self.skills.update(draft['skillId'],SkillUpdate(instructions='Explicitly reviewed change',
            provenance={'source_type':'manual','origin':'local'},reason='operator edit'),actor=self.actor)
        with self.assertRaises(DefinitionConflictError):
            self.skills.publish(edited['skillId'],edited['recordId'],SkillPublish(),actor=self.actor)
        self.scan(edited)
        published=self.skills.publish(edited['skillId'],edited['recordId'],SkillPublish(),actor=self.actor)
        self.assertEqual(published['definitionLifecycle'],'published')

    async def test_incident_postmortem_event_exact_links_and_hypotheses(self):
        await self.finish()
        service=self.outcomes()
        from codex_web.services.incidents import IncidentService
        from codex_web.storage.incidents import IncidentStore
        from codex_web.incidents import IncidentDetect,IncidentTriage,IncidentResolve,IncidentPostmortemCreate,IncidentStatus
        incidents=IncidentService(IncidentStore(self.sqlite),attention=self.attention,
            artifacts=self.artifacts,action_intents=self.intents,resources=self.resources,memory=service.memory)
        service.incidents=incidents
        incidents.procedure_outcomes=service
        incident=await incidents.detect(IncidentDetect(title='Controlled service outage',severity='sev3',
            source='controlled-test',project_id='home',affected_resource_ids=(self.resource.id,)),actor=self.actor)
        await incidents.triage(incident.id,IncidentTriage(commander_identity_id=self.actor.identity_id),actor=self.actor)
        await incidents.transition(incident.id,IncidentStatus.ACTIVE,actor=self.actor,summary='Controlled investigation')
        proof_ids=tuple(self.runner.get(self.assignment.id,self.actor).procedure_checkpoint.steps['ready'].evidence.values())
        await incidents.resolve(incident.id,IncidentResolve(evidence_ids=proof_ids,summary='Restoration observed'),actor=self.actor)
        incident,postmortem=await incidents.create_postmortem(incident.id,IncidentPostmortemCreate(
            summary='Successful restart is observed; crash cause remains unknown.',
            contributing_factors=('Unconfirmed library crash hypothesis',),evidence_ids=proof_ids,
            procedure_assignment_ids=(self.assignment.id,)),actor=self.actor)
        result=service.convert(self.assignment.id,actor=self.actor,incident_id=incident.id)
        self.assertEqual(result['report']['incident_id'],incident.id)
        self.assertEqual(result['report']['postmortem_id'],postmortem.id)
        self.assertEqual(result['report']['postmortem_knowledge']['id'],postmortem.knowledge_id)
        self.assertIn('unconfirmed',result['draft']['skill']['instructions'])
        self.assertNotIn('Unconfirmed library crash hypothesis',result['draft']['skill']['instructions'])
        self.assertEqual(len(self.skills.revisions(self.pin.definition_id,actor=self.actor)),2)

    async def test_classification_retention_and_deny_model_context_inherited(self):
        await self.finish()
        service=self.outcomes()
        source=self.artifacts.get_artifact(self.snapshot.id,actor=self.actor)
        def classify(state):
            for record in state.records:
                if record.id==source.governance_record_id:
                    from codex_web.data_governance import DataClassification
                    record.classification=DataClassification.RESTRICTED
                    record.deny_model_context=True
            return state
        service.memory.governance.store.update(classify)
        result=service.convert(self.assignment.id,actor=self.actor)
        knowledge=service.memory.get(result['knowledge_id'],actor=self.actor)
        self.assertEqual(knowledge.classification,'restricted')
        self.assertTrue(knowledge.deny_model_context)
        derived=next(r for r in service.memory.governance.store.load().records if r.object_type=='procedure_skill')
        self.assertEqual(derived.classification,'restricted')
        self.assertTrue(derived.deny_model_context)
        self.scan(result['draft'])
        with self.assertRaises(DefinitionConflictError):
            self.skills.publish(result['draft']['skillId'],result['draft']['recordId'],SkillPublish(),actor=self.actor)


class FullProcedureOutcomeTests(_ProcedureOutcomeFixture, unittest.IsolatedAsyncioTestCase):
    """Full diagnostic/recovery contract followed by real concurrent conversion."""
    async def test_concurrent_duplicate_delivery_one_inactive_revision_exact_sources(self):
        checkpoint = await self.finish()
        self.assertEqual(set(checkpoint.steps),
            {'backup', 'disk', 'change', 'ready', 'rollback', 'restored'})
        self.assertEqual(checkpoint.steps['disk'].status, 'succeeded')
        self.assertEqual(self.provider.calls,
            ['fixture.backup', 'fixture.change', 'fixture.readiness'])
        service = self.outcomes()
        def deliver(_):
            return service.convert(self.assignment.id, actor=self.actor)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(deliver, range(8)))
        drafts = {r['draft']['recordId'] for r in results}
        self.assertEqual(len(drafts), 1)
        derived=[r for r in service.memory.governance.store.load().records if r.object_type=='procedure_skill']
        self.assertEqual(len(derived),1)
        self.assertEqual(len(self.skills.revisions(self.pin.definition_id, actor=self.actor)), 2)
        draft = results[0]['draft']
        self.assertEqual(draft['definitionLifecycle'], 'draft')
        self.assertEqual(results[0]['report']['prior_definition'], self.pin.model_dump(mode='json'))
        self.assertIn(self.pin.record_id, results[0]['diff'])
        self.assertIn('unconfirmed', draft['skill']['instructions'])
        self.assertEqual(draft['skill']['provenance']['upstream_digest'], self.pin.checksum)
        self.assertTrue(results[0]['publication_required'])
        knowledge=service.memory.get(results[0]['knowledge_id'],actor=self.actor)
        self.assertEqual(knowledge.provenance.origin,'secondary_research')
        from codex_web.definitions import DefinitionReference
        with self.assertRaises(SkillConflict):
            self.skills.validate_reference(DefinitionReference.model_validate(draft['definitionReference']), actor=self.actor)
        self.assertEqual(self.runner.get(self.assignment.id,self.actor).procedure_checkpoint.definition,self.pin)
        self.runner.models.invoke.assert_not_called()
