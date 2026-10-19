import asyncio
from pathlib import Path
import unittest

from fastapi import HTTPException

from codex_web.identity import TenantScope
from codex_web.models import WorkItemAckCreate, WorkItemHandoff, WorkItemHandoffDelivery, WorkItemState, TaskSourceIdentity
from codex_web.owner_routing import OwnerRoutingError
from codex_web.services.gitlab_task_source import GitLabTaskSource
from codex_web.services.task_sources import TaskSourceSnapshot
from codex_web.services.task_source_work_items import TaskSourceWorkItemProjector
from codex_web.services.work_item_dependencies import WorkItemRuntimeDependencies
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.services.work_items import WorkItemService
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.work_item_list_index import WorkItemListIndex
from tests import test_owner_routing as fixtures


class OwnerRoutingStateTests(unittest.TestCase):
    def setUp(self):
        self.env = fixtures.OwnerRoutingTests()
        self.env.setUp()
        self.addCleanup(self.env.doCleanups)
        root = Path(self.env.temp.name)
        self.states = ModelMapRepository(self.env.state, namespace='work_item_states',
            legacy_path=root / 'work_items.json', model=WorkItemState)
        self.dependencies = WorkItemRuntimeDependencies(data_dir=root, events_file=root / 'events.jsonl',
            load_states=self.states.load, save_states=self.states.save, load_projects=self.env.projects.list,
            resource_ids_for_project=lambda *args, **kwargs: [], leading_owner_cue_in_action=lambda value: None,
            default_validation_owner='quinn', default_release_owner='release manager',
            non_implementation_owners=frozenset({'quinn', 'release manager', 'orchestrator'}),
            get_state=self.states.get, save_state=lambda value: self.states.put(value.ref, value),
            owner_routing=lambda state: self.env.roles.owner_routing(project_id=state.project_id,
                scope=TenantScope(organization_id=state.organization_id, workspace_id=state.workspace_id)))
        self.machine = WorkItemStateMachine(store=self.env.state, dependencies=self.dependencies)
        self.reference = self.env.publish()

    def pending(self, *, qualified=False):
        state = WorkItemState(ref='acme/widgets#1', organization_id='org-a', workspace_id='workspace-a',
            project_id='project-a', current_owner='carl', next_owner='operator', implementation_owner='carl',
            validation_owner='quinn', release_owner='release manager', current_stage='implementation_active',
            status_label='status::awaiting confirmation', artifact_state='branch',
            handoff=WorkItemHandoff(from_agent='carl', to_agent='operator', requested_at=1, status='pending'),
            created_at=1, updated_at=1, last_meaningful_update_at=1)
        if qualified:
            state.handoff.owner_routing = self.machine._owner_routing(state).binding('operator')
            state.handoff.delivery = WorkItemHandoffDelivery(status='delivered', started_at=2,
                thread_id='private-thread', turn_id='private-turn', execution_id='private-execution',
                scope_verified=True, execution_profile_id='orchestration-only')
        self.states.put(state.ref, state)
        return state

    def test_real_ack_records_actual_actor_and_keeps_original_handoff_and_claims(self):
        original = self.pending(qualified=True)
        result = self.machine._structured_ack(original.ref, WorkItemAckCreate(actor='orchestrator', expected_handoff_requested_at=1))
        self.assertEqual(result.current_owner, 'orchestrator')
        self.assertEqual(result.next_owner, 'orchestrator')
        self.assertEqual(result.implementation_owner, 'carl')
        self.assertEqual(result.validation_owner, 'quinn')
        self.assertEqual(result.handoff.to_agent, 'operator')
        self.assertEqual(result.handoff_history[-1].to_agent, 'operator')
        event = self.machine.event_store.load()[-1]
        self.assertEqual(event.actor, 'orchestrator')
        self.assertEqual(event.payload['owner_routing']['configured_owner'], 'operator')
        self.assertEqual(event.payload['owner_routing']['definition_record_id'], self.reference.record_id)
        self.assertFalse(self.machine._work_item_split_brain_findings(result))

    def test_alias_cannot_be_used_to_impersonate_the_ack_actor(self):
        state = self.pending()
        with self.assertRaises(HTTPException):
            self.machine._structured_ack(state.ref, WorkItemAckCreate(actor='operator'))
        self.assertEqual(self.states.get(state.ref), state)
        self.assertFalse(self.machine.event_store.load())

    def test_new_mapping_cannot_retrofit_a_legacy_pending_ack(self):
        state = self.pending()
        self.env.publish({'operator': 'james'})
        with self.assertRaises(HTTPException):
            self.machine._structured_ack(state.ref, WorkItemAckCreate(actor='orchestrator'))
        self.assertEqual(self.states.get(state.ref), state)
        self.assertFalse(self.machine.event_store.load())

    def test_bound_pending_requires_real_delivery_and_exact_handoff(self):
        state = self.pending(qualified=True)
        for timestamp in (None, 99):
            with self.assertRaises(HTTPException) as caught:
                self.machine._structured_ack(state.ref, WorkItemAckCreate(actor='orchestrator',
                    expected_handoff_requested_at=timestamp))
            self.assertEqual(caught.exception.detail['code'], 'owner_ack_handoff_changed')
        state.handoff.delivery = WorkItemHandoffDelivery()
        self.states.put(state.ref, state)
        with self.assertRaises(HTTPException) as caught:
            self.machine._structured_ack(state.ref, WorkItemAckCreate(actor='orchestrator',
                expected_handoff_requested_at=1))
        self.assertEqual(caught.exception.detail['code'], 'owner_alias_delivery_unproven')
        self.assertEqual(self.states.get(state.ref), state)
        self.assertFalse(self.machine.event_store.load())

    def test_old_delivery_without_original_binding_cannot_prove_alias_ack(self):
        state = self.pending(qualified=True)
        state.handoff.owner_routing = None
        self.states.put(state.ref, state)
        with self.assertRaises(HTTPException) as caught:
            self.machine._structured_ack(state.ref, WorkItemAckCreate(actor='orchestrator',
                expected_handoff_requested_at=1))
        self.assertEqual(caught.exception.detail['code'], 'owner_alias_delivery_unproven')
        self.assertEqual(self.states.get(state.ref), state)

    def test_accepted_recipient_keeps_original_definition_after_mapping_change(self):
        state = self.pending(qualified=True)
        accepted = self.machine._structured_ack(state.ref, WorkItemAckCreate(actor='orchestrator',
            expected_handoff_requested_at=1))
        original = accepted.handoff.model_dump(mode='json')
        self.env.publish({'operator': 'james'})
        retained = self.states.get(state.ref)
        self.assertEqual(retained.handoff.model_dump(mode='json'), original)
        self.assertFalse(self.machine._work_item_split_brain_findings(retained))
        self.assertTrue(self.machine._preserve_accepted_handoff_recipient(retained,
            incoming_owner='operator', incoming_stage='implementation_active',
            incoming_status_label='status::in progress'))
        with self.assertRaises(HTTPException):
            self.machine._structured_ack(state.ref, WorkItemAckCreate(actor='james',
                expected_handoff_requested_at=1))
        self.assertEqual(self.states.get(state.ref), retained)

    def test_legacy_accepted_receipt_is_not_renamed_by_current_alias(self):
        state = self.pending()
        state.current_owner = state.next_owner = 'operator'
        state.handoff.status = 'accepted'
        state.handoff.acknowledged_at = 2
        state.status_label = 'status::in progress'
        self.states.put(state.ref, state)
        legacy = state.handoff.model_dump(mode='json')
        self.assertNotIn('owner_routing', legacy)
        self.assertFalse(self.machine._work_item_split_brain_findings(state))
        with self.assertRaises(HTTPException):
            self.machine._structured_ack(state.ref, WorkItemAckCreate(actor='orchestrator'))
        self.assertEqual(self.states.get(state.ref).handoff.model_dump(mode='json'), legacy)

    def test_alias_never_promotes_a_sender_into_the_orchestrator_lane_override(self):
        state = self.pending()
        denied = self.machine._validate_handoff_edge(state, from_agent='operator',
            to_agent='quinn', artifact_state='merged_main')
        self.assertEqual(denied['code'], 'wrong_lane')
        self.assertIsNone(self.machine._validate_handoff_edge(state, from_agent='orchestrator',
            to_agent='quinn', artifact_state='merged_main'))

    def test_foreign_project_scope_is_a_typed_refusal_without_persistent_changes(self):
        state = self.pending().model_copy(update={'workspace_id': 'foreign'})
        self.states.put(state.ref, state)
        with self.assertRaises(HTTPException) as caught:
            self.machine._structured_ack(state.ref, WorkItemAckCreate(actor='orchestrator'))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail['code'], 'owner_routing_unavailable')
        self.assertEqual(self.states.get(state.ref), state)
        self.assertFalse(self.machine.event_store.load())

    def test_contract_role_data_and_reference_refresh_together_after_external_publication(self):
        from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
        from codex_web.services.definitions import DefinitionRegistryService
        from codex_web.services.execution_role_definitions import ExecutionRoleDefinitionService
        from codex_web.services.work_item_contracts import WorkItemContractService
        from codex_web.storage.definition_registry import DefinitionRegistryStore
        from codex_web.storage.sqlite_state import SQLiteStateStore
        reader = ExecutionRoleDefinitionService(DefinitionRegistryService(
            DefinitionRegistryStore(SQLiteStateStore(self.env.state.path)), schemas=self.env.registry.schemas))
        service = WorkItemContractService(None, lambda state: state.ref, reader)
        state = self.pending()
        self.assertEqual(service.contract_for_state(state).definition_refs[0].record_id, self.reference.record_id)
        payload = dict(self.env.registry.get_record(self.reference.record_id).payload)
        payload['roles'] = [{**role, 'description': 'Fresh coordination contract'} if role['id'] == 'orchestrator'
                            else role for role in payload['roles']]
        draft = self.env.registry.create_draft(DefinitionDraftCreate(definition_id='execution-roles.default',
            kind='execution-role-catalog', definition_schema_version='1.0', scope_type='project',
            scope_id='project-a', payload=payload, actor='fixture'))
        self.env.registry.approve_publication(draft.record_id, actor='fixture-reviewer',
            reference='PRIVATE', reason='private role refresh regression')
        current = self.env.registry.publish(draft.record_id, DefinitionPublishRequest(actor='fixture'))
        self.assertEqual(service.role_for_state(state).description, 'Fresh coordination contract')
        self.assertEqual(service.contract_for_state(state).definition_refs[0].record_id, current.record_id)

    def test_public_projection_explains_alias_without_rewriting_historical_fields(self):
        state = self.pending()
        public = self.machine._work_item_state_public(state)
        self.assertEqual(public['next_owner'], 'operator')
        self.assertEqual(public['owner_routing']['canonical_owner'], 'orchestrator')
        self.assertTrue(public['owner_routing']['alias_applied'])
        self.assertFalse(public['routingErrors'])
        foreign = state.model_copy(update={'workspace_id': 'foreign'})
        denied = self.machine._work_item_state_public(foreign)
        self.assertEqual(denied['owner_routing']['status'], 'blocked')
        self.assertEqual(denied['next_owner'], 'operator')

    def test_pages_retain_conflicting_claim_and_continue_without_rewriting_history(self):
        for claim in ('implementation_owner', 'validation_owner', 'release_owner'):
            with self.subTest(claim=claim):
                normal = self.pending()
                original = normal.model_copy(update={claim: 'operator'})
                self.states.put(original.ref, original)
                neighbors = [normal.model_copy(update={
                    'ref': 'acme/widgets#' + str(number),
                    'updated_at': timestamp,
                }) for number, timestamp in ((2, 2.0), (3, 0.5))]
                for neighbor in neighbors:
                    self.states.put(neighbor.ref, neighbor)
                before = self.states.load()
                index = WorkItemListIndex(self.env.state)
                index.rebuild(before)
                service = object.__new__(WorkItemService)
                service.work_items = self.dependencies
                service.state_machine = self.machine
                service.work_item_list_index = index

                async def pages():
                    rows, cursor = [], None
                    for _ in range(4):
                        page = await service.list(project_id='project-a', owner=None,
                            stage=None, release_gate=None, scope=self.env.scope,
                            limit=1, cursor=cursor)
                        rows.extend(page['items'])
                        cursor = page['nextCursor']
                    self.assertIsNone(cursor)
                    return rows

                rows = asyncio.run(pages())
                self.assertEqual([row['ref'] for row in rows],
                    [neighbors[0].ref, original.ref, neighbors[1].ref])
                self.assertEqual(rows[1]['routingError'], 'owner_alias_cannot_change_claimed_lane')
                self.assertEqual(rows[1]['current_owner'], original.current_owner)
                self.assertEqual(rows[1]['next_owner'], original.next_owner)
                self.assertEqual(self.machine._work_item_state_public(original)['routingError'],
                    rows[1]['routingError'])
                service.work_item_list_index = None
                fallback = asyncio.run(service.list(project_id='project-a', owner=None,
                    stage=None, release_gate=None, scope=self.env.scope, limit=3))
                self.assertEqual([row['ref'] for row in fallback['items']],
                    [row['ref'] for row in rows])
                with self.assertRaises(HTTPException) as denied:
                    self.machine._structured_ack(original.ref, WorkItemAckCreate(actor='orchestrator'))
                self.assertEqual(denied.exception.status_code, 409)
                self.assertEqual(self.states.load(), before)
                self.assertFalse(self.machine.event_store.load())

    def test_intake_keeps_source_alias_and_does_not_invent_an_implementation_claim(self):
        self.env.publish({'coordinator': 'orchestrator'})
        source = GitLabTaskSource('https://gitlab.example/api/v4', 'private-fixture-value')
        snapshot = TaskSourceSnapshot(identity=TaskSourceIdentity(source_type='gitlab',
            source_instance=source.source_instance, external_id='acme/widgets#2'),
            source_state='opened', labels=('owner::coordinator', 'status::in progress'))
        projector = TaskSourceWorkItemProjector(None, self.machine, dependencies=self.dependencies)
        state = projector.upsert(source, snapshot, project_id='project-a')
        self.assertEqual(state.current_owner, 'coordinator')
        self.assertIsNone(state.implementation_owner)
        self.assertEqual(self.machine._work_item_state_public(state)['owner_routing']['canonical_owner'], 'orchestrator')
        self.env.publish({'coordinator': 'james'})
        reread = projector.upsert(source, snapshot, project_id='project-a')
        self.assertIsNone(reread.implementation_owner)
        self.assertEqual(reread.current_owner, 'coordinator')
        self.assertNotIn('private-fixture-value', self.machine.event_store.load()[-1].model_dump_json())

    def test_unmapped_owner_does_not_become_implementation_after_alias_quarantine(self):
        self.env.registry.quarantine(self.reference.record_id, actor='fixture', reason='Private fixture')
        state = self.pending().model_copy(update={'current_owner': 'unknown', 'next_owner': None,
            'implementation_owner': None, 'handoff': None})
        self.machine._ensure_work_item_lane_defaults(state)
        self.assertIsNone(state.implementation_owner)

    def test_intake_retains_source_facts_when_role_definitions_are_unavailable(self):
        global_record = self.env.registry.resolve(definition_id='execution-roles.default', kind='execution-role-catalog')
        for record in (global_record, self.reference):
            self.env.registry.quarantine(record.record_id, actor='fixture', reason='Private fixture')
        source = GitLabTaskSource('https://gitlab.example/api/v4', 'private-fixture-value')
        snapshot = TaskSourceSnapshot(identity=TaskSourceIdentity(source_type='gitlab',
            source_instance=source.source_instance, external_id='acme/widgets#3'),
            source_state='opened', labels=('owner::operator', 'status::in progress'))
        projector = TaskSourceWorkItemProjector(None, self.machine, dependencies=self.dependencies)
        state = projector.upsert(source, snapshot, project_id='project-a')
        self.assertEqual(self.states.get(state.ref).current_owner, 'operator')
        self.assertIsNone(state.implementation_owner)
        self.assertEqual(self.machine._work_item_state_public(state)['owner_routing']['status'], 'blocked')
