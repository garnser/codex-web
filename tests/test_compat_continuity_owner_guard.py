"""Actual historical factory/core wiring; canonical private owner routing, no app import."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from codex_web.services.work_item_continuity import build_work_item_continuity_compatibility_service
from tests.test_owner_routing_recipients import CompatibleRecipientTests


class CompatibilityContinuityOwnerGuardTests(unittest.IsolatedAsyncioTestCase):
    application_source = None
    builder = staticmethod(build_work_item_continuity_compatibility_service)

    def setUp(self):
        self.fixture=CompatibleRecipientTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture;c=f.continuity
        assignment=f.assignments['assignment-platform-thread']
        assignment.repository_scope=assignment.repository_scope.model_copy(update={'read_only_repository_ids':('core',)})
        self.host=SimpleNamespace(_actionable_owner_continuity_delay_seconds=lambda:0,
            _handoff_continuity_delay_seconds=lambda:0,_work_item_state=c.get_state,_coerce_owner=c.coerce_owner,
            _binding_for_agent=c.binding_for_agent,_replace_nonperforming_thread_if_needed=AsyncMock(side_effect=AssertionError('generic replacement bypass')),
            _dispatch_event_to_binding=f.fixture.dispatch,_work_item_dispatch_text=c.dispatch_text,
            _append_bot_event=c.append_event,_truncate_text=c.truncate_text,_thread_is_active=lambda thread:False,
            _thread_queue_depth=lambda thread:0,_thread_queue=lambda thread:[],_thread_recently_active=lambda thread:False,
            _watchdog_dispatch_allowed=lambda key:True,_record_watchdog_dispatch=Mock(),HANDOFF_COORDINATION_CHANNEL=c.coordination_channel)
        # Execute only the actual factory call and historical method assignments.
        # Importing application.py would create real runtime state and is forbidden.
        source=self.application_source or (Path(__file__).parents[1]/'codex_web/application.py').read_text()
        tree=ast.parse(source);selected=[]
        for node in tree.body:
            if not isinstance(node,ast.Assign) or len(node.targets)!=1:continue
            target=node.targets[0]
            if isinstance(target,ast.Name) and target.id=='work_item_continuity_compatibility':
                selected.append(node)
            elif (isinstance(target,ast.Attribute) and isinstance(target.value,ast.Name) and target.value.id=='core'
                    and isinstance(node.value,ast.Attribute) and isinstance(node.value.value,ast.Name)
                    and node.value.value.id=='work_item_continuity_compatibility'):
                selected.append(node)
        self.assertEqual(len(selected),10)
        namespace={'core':self.host,'owner_routing_service':f.service,
            'build_work_item_continuity_compatibility_service':self.builder}
        exec(compile(ast.Module(body=selected,type_ignores=[]),'application compatibility wiring','exec'),namespace)
        self.compat=namespace['work_item_continuity_compatibility']

    async def test_historical_continuity_uses_same_router_and_preserves_readonly_recipient(self):
        f=self.fixture
        await self.host._run_handoff_continuity_check(f.state.ref,expected_recipient='orchestrator',
            expected_requested_at=1,source='work-item-handoff-continuity')
        self.assertIs(self.compat.owner_routing,f.service)
        args,kwargs=f.fixture.dispatch.call_args
        pin=kwargs['owner_routing_pin']
        self.assertEqual(pin.recipient.thread_id,'platform-thread');self.assertEqual(pin.original_thread_id,'existing-thread')
        self.assertEqual(f.service.repository_scope_for_pin(pin,args[0]).read_only_repository_ids,('core',))
        self.host._replace_nonperforming_thread_if_needed.assert_not_awaited()
        self.assertEqual(f.f.binding.model_dump(mode='json'),f.binding_before)
        self.assertEqual(f.assignments['assignment-existing-thread'].model_dump(mode='json'),f.source_before)
        f.validate(pin)

    async def test_historical_structured_dispatch_refuses_cancelled_compatible_lane(self):
        f=self.fixture;f.assignments['assignment-platform-thread'].status='cancelled'
        result=await self.host._dispatch_structured_handoff_to_recipient(f.state,source='work-item-handoff')
        self.assertEqual(result['code'],'owner_recipient_compatible_lane_unavailable')
        f.fixture.dispatch.assert_not_awaited();self.host._replace_nonperforming_thread_if_needed.assert_not_awaited()

    async def test_historical_actionable_dispatch_refuses_unknown_original(self):
        f=self.fixture;f.f.preflight.put(f.f.uncertain())
        f.state.handoff=None;f.state.current_owner='orchestrator';f.state.next_owner='orchestrator'
        f.state.current_stage='validation_running';f.f.env.states.put(f.state.ref,f.state)
        result=await self.host._dispatch_actionable_owner_to_responsible_thread(f.state,source='owner-work-watchdog')
        self.assertEqual(result['code'],'owner_routing_native_outcome_unresolved')
        f.fixture.dispatch.assert_not_awaited();self.host._replace_nonperforming_thread_if_needed.assert_not_awaited()

    async def test_historical_selected_busy_custody_is_not_replaced(self):
        f=self.fixture
        self.host._thread_is_active=lambda thread:thread=='platform-thread'
        f.state.handoff=None;f.state.current_owner='orchestrator';f.state.next_owner='orchestrator'
        f.state.current_stage='validation_running';f.f.env.states.put(f.state.ref,f.state)
        result=await self.host._dispatch_actionable_owner_to_responsible_thread(f.state,source='owner-work-watchdog')
        f.fixture.dispatch.assert_not_awaited();self.host._replace_nonperforming_thread_if_needed.assert_not_awaited()
        self.assertEqual(f.f.binding.model_dump(mode='json'),f.binding_before)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(CompatibilityContinuityOwnerGuardTests(name)
        for name in CompatibilityContinuityOwnerGuardTests.__dict__ if name.startswith('test_'))
