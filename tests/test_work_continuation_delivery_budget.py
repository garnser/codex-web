"""Canonical Work selection through real native composition; no app import.

The old-selector fallback allows this test-only file to reproduce on released
M7 before the delivery selector exists. No runtime authority is fabricated.
"""
import copy
import json
import unittest

from codex_web.models import Project, WorkItemHandoff, WorkItemHandoffDelivery
from codex_web.services.codex_context import render_codex_context
from codex_web.services.work_item_contracts import assignment_control_plane_instructions
from codex_web.skills import SkillCreate
from codex_web.definitions import reference_for
from codex_web.work_item_execution_models import WorkItemCheckpointCreate, WorkItemExecutionUpdate
from tests import test_work_item_checkpoint_provenance as work_fixture
from tests import test_turn_execution as turn_fixture
from tests import test_skills as skill_fixture
from tests import test_procedure_discovery as procedure_fixture
from tests.test_codex_context_budget import Native0160State, source_text


class WorkContinuationDeliveryBudgetTests(unittest.IsolatedAsyncioTestCase):
    def work(self):
        env = work_fixture.WorkItemCheckpointProvenanceTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        state = env.host.states[env.ref]
        state.notes = [f'Historical observation {i}: ' + 'retained history is not authority. ' * 55 for i in range(20)]
        state.resource_ids = ['repo-required']
        state.current_owner = state.implementation_owner = 'james'
        state.validation_owner = 'quinn'
        state.next_owner = 'james'
        state.blocker = 'Receiver readability remains unqualified; old outcomes retained.'
        state.next_action = 'Read only the admitted Artifact and exact provenance; do not replay old requests.'
        state.handoff = WorkItemHandoff(from_agent='orchestrator', to_agent='james', requested_at=2,
            expected_action='Verify artifact-exact and evidence-exact, SHA256' + 'a' * 64,
            reason='New bounded receiver read; preserve the previous original.',
            delivery=WorkItemHandoffDelivery())
        env.service.checkpoint(env.ref, WorkItemCheckpointCreate(source='operator-input-admission',
            reason='governed_input_admitted', summary='Artifact artifact-exact; INFO evidence-exact.',
            current_state='Immutable source commit' + 'b' * 40 + '; 6187 bytes.',
            next_actions=['Read artifact-exact and evidence-exact; verify SHA256' + 'a' * 64]))
        return env

    @staticmethod
    def selector(env):
        return getattr(env.service, 'continuation_delivery', env.service.continuation_delta)

    async def test_real_current_work_procedure_and_skill_fit_first_and_resumed_native_context(self):
        env = self.work()
        skills = skill_fixture.SkillServiceTests(); skills.setUp(); self.addCleanup(skills.tearDown)
        published = skills._publish(skills.skills.create(SkillCreate(skill_id='delivery-budget', name='Required Skill',
            instructions='Selected Skill cannot grant authority. ' * 220), actor=skills.admin))
        ref = reference_for(skills.definitions.get_record(published['recordId']))
        selected = skills.skills.context_for_refs((ref,), actor=skills.admin, objective='read', max_chars=12000)
        procedures = procedure_fixture.DiscoveryTests(); procedures.setUp(); self.addCleanup(procedures.tearDown)
        content = procedure_fixture.procedure()
        for step in content['steps']:
            step['description'] = 'Inspect exact current evidence. ' * 35
            step['inputs'][0]['description'] = 'Canonical source provenance. ' * 35
            step['expected_evidence'][0]['description'] = 'Actual result, never authority. ' * 35
        record = procedures.publish(procedures.draft(content=content))
        procedures.discovery.max_context_characters = 40000
        result, text = procedures.select()
        self.assertTrue(result.admitted, result)
        self.assertGreater(len(text), 20000)
        async def procedure_context(**kwargs):
            # Actual published/discovered procedure text at the composition seam;
            # no pretend native tool-enforcement witness or provider execution.
            return None, text
        for first in (True, False):
            with self.subTest(first=first):
                host, _, sessions, service = turn_fixture.TurnExecutionStartTests()._service(
                    skill_context_resolver=lambda *a: selected,
                    work_item_context_resolver=self.selector(env))
                service.procedure_admission = procedure_context
                old_settings = host._thread_run_settings
                def settings(thread):
                    value = old_settings(thread); value.skill_refs = (ref,); return value
                host._thread_run_settings = settings
                if first:
                    service.bootstrap_bindings.thread_id = 't1'
                    service.bootstrap_bindings.initial_turn_pending = True
                    await sessions.session.create_initial_native_session('t1')
                await service.start_thread_turn_now('t1', project=Project(id='p1', name='Project', path='/workspace'),
                    message='Perform the NEW scoped read only.', sandbox='workspace-write',
                    approval_policy='on-request', work_item_ref=env.ref)
                values = next(p['additionalContext'] for m, p in sessions.session.requests if m == 'turn/start')
                rendered = Native0160State().merge(values)
                self.assertTrue(all('tokens truncated' not in value for _, _, value in rendered))
                self.assertEqual(source_text(values, 'assignment-protocol')[0], assignment_control_plane_instructions())
                self.assertEqual(source_text(values, 'selected-skills'), (selected.text.strip(), {'untrusted'}))
                self.assertEqual(source_text(values, 'procedure-discovery'), (text, {'untrusted'}))
                self.assertIn(record['recordId'], text)
                work, kinds = source_text(values, 'work-continuation')
                self.assertEqual(kinds, {'untrusted'})
                delivered = json.loads(work.split('\n', 1)[1])['work_item_context']
                self.assertEqual(delivered['handoff'], env.host.states[env.ref].handoff.model_dump(mode='json'))
                self.assertIn('artifact-exact', work)
                self.assertNotIn('Historical observation 0', work)
                self.assertEqual(delivered['_projection']['notes']['count'], 20)

    async def test_current_fields_checkpoint_and_raw_history_are_preserved_without_trusting_operator_anchor(self):
        env = self.work(); state = copy.deepcopy(env.host.states[env.ref])
        public = env.service.continuation_snapshot(env.ref)
        selection = self.selector(env)(env.ref)
        self.assertEqual(selection['mode'], 'full')
        self.assertEqual(selection['reason'], 'checkpoint_provenance_incomplete')
        context = selection['snapshot']['work_item_context']
        for key, value in public['work_item_context'].items():
            if key != 'notes': self.assertEqual(context[key], value)
        self.assertNotIn('notes', context)
        self.assertEqual(context['_projection']['notes']['sha256'], env.service._hash_value(state.notes))
        self.assertFalse(context['checkpoint_context']['delivery_proven'])
        self.assertIn('artifact-exact', context['checkpoint_context']['summary'])
        self.assertEqual(env.host.states[env.ref], state)
        self.assertEqual(env.service.continuation_snapshot(env.ref), public)

    async def test_legacy_full_anchor_moves_once_to_exact_projected_baseline_then_verified_delta(self):
        env = self.work(); env._record_trusted_snapshot_checkpoint()
        first = self.selector(env)(env.ref)
        self.assertEqual(first['mode'], 'full')
        self.assertEqual(first['reason'], 'checkpoint_delivery_projection_missing')
        context = first['snapshot']['work_item_context']
        delivered = {'mode': 'full', 'work_item_context': context}
        env.service.record_continuation_delivery(env.ref, 'new-forward', first, delivered)
        checkpoint = env.host.states[env.ref].execution.latest_checkpoint
        self.assertEqual(checkpoint.delivered_work_item_context, context)
        self.assertEqual(checkpoint.work_item_hash, env.service._hash_value(context))
        self.assertNotIn('notes', checkpoint.delivered_work_item_context)
        env.service.record_continuation_outcome(env.ref, 'new-forward', 'succeeded')
        env.host.states[env.ref].blocker = 'New actual blocker'
        env.host.states[env.ref].notes.append('New retained history')
        delta = self.selector(env)(env.ref)
        self.assertEqual(delta['mode'], 'delta')
        self.assertEqual(delta['changed_fields']['blocker'], 'New actual blocker')
        self.assertEqual(delta['snapshot']['work_item_context']['_projection']['notes']['count'], 21)
        self.assertNotIn('notes', delta['changed_fields'])
        self.assertTrue(all('payload' not in event for event in delta['events']))
        self.assertTrue(all(event['_projection']['payload_sha256'] for event in delta['events']))
        _, _, _, runtime = turn_fixture.TurnExecutionStartTests()._service(
            work_item_context_resolver=self.selector(env))
        text, _, delivered_delta = runtime._work_item_continuation_context(env.ref)
        self.assertEqual(delivered_delta['work_item_context']['handoff'], env.host.states[env.ref].handoff.model_dump(mode='json'))
        self.assertEqual(delivered_delta['work_item_context']['resource_ids'], ['repo-required'])
        self.assertIn('artifact-exact', text)

    async def test_delivery_bookkeeping_does_not_displace_substantive_input_checkpoint(self):
        env = self.work(); first = self.selector(env)(env.ref)
        env.service.record_continuation_delivery(env.ref, 'new-forward', first,
            {'mode': 'full', 'work_item_context': first['snapshot']['work_item_context']})
        later = self.selector(env)(env.ref)
        context = later['snapshot']['work_item_context']
        self.assertIn('artifact-exact', context['checkpoint_context']['summary'])
        self.assertEqual(context['checkpoint_context']['source'], 'operator-input-admission')
        self.assertFalse(env.service.continuation_anchor(env.ref)['trusted'])

    async def test_non_dict_legacy_projection_falls_back_without_rewriting_baseline(self):
        env = self.work()
        snapshot = env.service.continuation_snapshot(env.ref)
        snapshot['work_item_context']['_projection'] = 'legacy marker, not a schema'
        snapshot['work_item_hash'] = env.service._hash_value(snapshot['work_item_context'])
        selection = {'mode': 'full', 'snapshot': snapshot}
        env.service.record_continuation_delivery(env.ref, 'legacy-marker', selection,
            {'mode': 'full', 'work_item_context': snapshot['work_item_context']})
        env.service.record_continuation_outcome(env.ref, 'legacy-marker', 'succeeded')
        before = copy.deepcopy(env.host.states[env.ref])
        result = self.selector(env)(env.ref)
        self.assertEqual(result['mode'], 'full')
        self.assertEqual(result['reason'], 'checkpoint_delivery_projection_missing')
        self.assertEqual(env.host.states[env.ref], before)

    async def test_oversized_current_required_state_still_refuses_before_any_native_write(self):
        env = self.work()
        env.host.states[env.ref].handoff.expected_action = '漢🙂' * 20000
        state = copy.deepcopy(env.host.states[env.ref])
        host, _, sessions, service = turn_fixture.TurnExecutionStartTests()._service(
            work_item_context_resolver=self.selector(env))
        before_active, before_queue = dict(host.active), dict(host.queues)
        with self.assertRaises(turn_fixture.ExecutionPrestartRefusal) as caught:
            await service.start_thread_turn_now('t1', project=Project(id='p1', name='Project', path='/workspace'),
                message='New scoped request', sandbox='workspace-write', approval_policy='on-request', work_item_ref=env.ref)
        self.assertEqual(caught.exception.detail['code'], 'native_context_delivery_limit')
        self.assertFalse(caught.exception.detail['retryable'])
        self.assertEqual(sessions.session.requests, [])
        self.assertEqual(host.active, before_active)
        self.assertEqual(host.queues, before_queue)
        self.assertEqual(env.host.states[env.ref], state)

    async def test_delivery_event_page_is_bounded_and_raw_event_payloads_remain_retrievable(self):
        env = self.work(); first = self.selector(env)(env.ref)
        env.service.record_continuation_delivery(env.ref, 'new-forward', first,
            {'mode': 'full', 'work_item_context': first['snapshot']['work_item_context']})
        env.service.record_continuation_outcome(env.ref, 'new-forward', 'succeeded')
        for index in range(12):
            env.service.update(env.ref, WorkItemExecutionUpdate(actor='operator', timeout_seconds=120 + index))
        raw = env.service.history(env.ref, limit=100)
        selected = self.selector(env)(env.ref)
        self.assertEqual(selected['mode'], 'delta')
        self.assertEqual(len(selected['events']), 10)
        self.assertTrue(selected['requires_progressive_retrieval'])
        self.assertEqual(selected['next_event_offset'], 10)
        self.assertTrue(all('payload' not in event for event in selected['events']))
        self.assertTrue(any('payload' in event for event in raw['items']))
        self.assertEqual(env.service.history(env.ref, limit=100), raw)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(WorkContinuationDeliveryBudgetTests)
