from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from codex_web.models import GitLabProjectRoutingSettings, GitLabRoutingSettings, WorkItemHandoff, WorkItemState
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.gitlab import GitLabService
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class OwnerFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ingestion = CanonicalEventIngestionService(CanonicalEventBus(
            CanonicalEventStore(SQLiteStateStore(Path(self.directory.name) / 'state.sqlite3'))))
        self.state = WorkItemState(ref='group/project!36', project_id='project',
            organization_id='org', workspace_id='workspace', resource_ids=['repo'],
            kind='merge_request', current_owner='nora', status_label='status::in progress',
            last_meaningful_update_at=1, updated_at=1, created_at=1)
        self.baseline = self.state.model_copy(deep=True)
        self.lookup = Mock(side_effect=lambda _:self.baseline.model_copy(deep=True))
        host = NS(_project=lambda _:NS(organization_id='org', workspace_id='workspace'),
            _load_gitlab_routing_settings=lambda:GitLabRoutingSettings(enabled=True),
            _gitlab_event_id=lambda request,payload:request.event_id,
            _work_item_state=self.lookup)
        self.service = GitLabService(host, canonical_events=self.ingestion)
        self.service.project_settings_for_payload = lambda *_:('project', GitLabProjectRoutingSettings(enabled=True))
        self.service.routing_bindings_for_agent = lambda *_:[NS(thread_id='owner-thread')]
        self.service.work_items = replace(self.service.work_items,
            split_brain_findings=lambda _:[], project_event=Mock(side_effect=lambda *_args,**_kw:self.state))
        self.dispatch=AsyncMock(return_value={'ok':True,'queued':True,'threadId':'owner-thread'})
        self.recovery=Mock();self.events=[]
        self.semantic_events = {}
        def save_semantics(values):
            self.semantic_events.clear()
            self.semantic_events.update(values)
        self.service.operations=replace(self.service.operations,dispatch_event=self.dispatch,
            dispatch_scoped_event=self.dispatch,send_event_notice=AsyncMock(return_value={'sent':False}),
            append_event=self.events.append,publish_event=AsyncMock(),schedule_recovery=self.recovery,
            load_semantic_events=lambda:dict(self.semantic_events),save_semantic_events=save_semantics)
        self.payload={'object_kind':'merge_request','project':{'id':8,'path_with_namespace':'group/project'},
            'object_attributes':{'iid':36,'action':'update','state':'opened','title':'Document changes'},
            'labels':[{'title':'owner::nora'},{'title':'status::in progress'}],
            'changes':{'labels':{'previous':[{'title':'owner::nora'},{'title':'status::blocked'}],
                'current':[{'title':'owner::nora'},{'title':'status::in progress'}]}}}

    async def handle(self, event_id='event-1'):
        return await self.service.handle_event(NS(event_id=event_id,json=AsyncMock(return_value=self.payload)))

    async def test_unchanged_workflow_feedback_is_retained_without_reasoning_or_recovery(self):
        self.state.updated_at=50;self.state.last_gitlab_event_at=50
        result=await self.handle()
        self.assertTrue(result['accepted'])
        self.assertTrue(result.get('deterministic'))
        self.assertEqual(result['reason'],'workflow_feedback_recorded')
        self.dispatch.assert_not_awaited();self.recovery.assert_not_called()
        self.assertEqual(len(self.ingestion.bus.store.load().events),1)
        self.assertEqual(self.baseline.current_owner,'nora')

    async def test_changed_canonical_owner_or_handoff_is_not_feedback(self):
        self.state.current_owner='quinn'
        await self.handle()
        self.dispatch.assert_awaited_once()

    async def test_pending_handoff_survives_feedback_without_recipient_ack(self):
        handoff=WorkItemHandoff(from_agent='nora',to_agent='quinn',requested_at=20)
        for state in (self.state,self.baseline):
            state.handoff=handoff.model_copy(deep=True);state.next_owner='quinn'
            state.status_label='status::awaiting confirmation'
        self.payload['labels']=[{'title':'owner::nora'},{'title':'status::awaiting confirmation'}]
        self.payload['changes']['labels']['current']=self.payload['labels']
        result=await self.handle()
        self.assertTrue(result.get('deterministic'))
        self.assertEqual(self.state.handoff.status,'pending')
        self.assertIsNone(self.state.handoff.acknowledged_at)
        self.dispatch.assert_not_awaited()

    async def test_material_content_and_nonworkflow_labels_still_reach_owner(self):
        for change in ({'description':{'previous':'old','current':'new'}},
                       {'labels':{'previous':[],'current':[{'title':'priority::P1'}]}}):
            with self.subTest(change=change):
                await self.asyncSetUp();self.payload['changes'].update(change)
                await self.handle();self.dispatch.assert_awaited_once()

    async def test_missing_baseline_or_conflicting_project_never_suppresses(self):
        for baseline in (None,self.baseline.model_copy(update={'project_id':'other'})):
            with self.subTest(baseline=baseline):
                self.lookup.side_effect=lambda _:baseline
                self.semantic_events.clear()
                self.dispatch.reset_mock();await self.handle(str(id(baseline)))
                self.dispatch.assert_awaited_once()

    async def test_routing_conflict_remains_orchestrator_exception(self):
        self.service.work_items=replace(self.service.work_items,split_brain_findings=lambda _:['owner drift'])
        result=await self.handle()
        self.assertFalse(result.get('deterministic',False));self.dispatch.assert_awaited_once()
        self.assertEqual(self.service.event_target_agents(self.payload,GitLabProjectRoutingSettings(),self.state),['orchestrator'])

    async def test_wrong_tenant_and_unavailable_baseline_remain_reasoning(self):
        for lookup in (lambda _:self.baseline.model_copy(update={'organization_id':'other'}),
                       Mock(side_effect=RuntimeError('observation unavailable'))):
            with self.subTest(lookup=lookup):
                await self.asyncSetUp()
                self.lookup.side_effect=lookup
                await self.handle();self.dispatch.assert_awaited_once()

    async def test_provider_owner_disagreement_is_not_unchanged_feedback(self):
        self.payload['changes']['labels']['current']=[{'title':'owner::quinn'},{'title':'status::in progress'}]
        await self.handle();self.dispatch.assert_awaited_once()

    async def test_semantic_duplicate_still_has_canonical_ingress_record(self):
        await self.handle()
        duplicate=await self.handle('event-2')
        self.assertEqual(duplicate['reason'],'semantic_duplicate')
        self.assertIn('canonicalEventId',duplicate)
        self.assertEqual(len(self.ingestion.bus.store.load().events),2)

    def test_blocked_lane_uses_action_owner_without_replacing_implementation_owner(self):
        self.state.current_stage='failed_with_action_owner';self.state.next_owner='carl'
        self.assertEqual(self.service.event_target_agents(self.payload,GitLabProjectRoutingSettings(),self.state),['carl'])
        self.assertEqual(self.state.current_owner,'nora')

    def test_healthy_owner_and_pending_recipient_bypass_global_route(self):
        routing=GitLabProjectRoutingSettings(route_agents=['orchestrator'])
        self.assertEqual(self.service.event_target_agents(self.payload,routing,self.state),['nora'])
        self.state.handoff=WorkItemHandoff(from_agent='nora',to_agent='quinn',requested_at=20)
        self.assertEqual(self.service.event_target_agents(self.payload,routing,self.state),['quinn'])

    def test_different_mr_commits_are_not_semantic_duplicates(self):
        self.payload['object_attributes']['last_commit']={'id':'a'*40}
        first=self.service.semantic_key(self.payload)
        self.payload['object_attributes']['last_commit']={'id':'b'*40}
        self.assertNotEqual(first,self.service.semantic_key(self.payload))

    def test_different_issue_content_is_not_a_semantic_duplicate(self):
        self.payload['object_kind']='issue'
        self.payload['object_attributes']['description']='first'
        first=self.service.semantic_key(self.payload)
        self.payload['object_attributes']['description']='second'
        self.assertNotEqual(first,self.service.semantic_key(self.payload))
