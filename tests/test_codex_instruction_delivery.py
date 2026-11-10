"""Pinned wire projection plus real private Skill rendering; external transport synthetic."""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.agent_runtime import AgentRuntimeSessionRequest, AgentRuntimeTurnRequest
from codex_web.definitions import reference_for
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.services.work_item_contracts import assignment_control_plane_instructions
from codex_web.skills import SkillCreate, SkillUpdate
from tests import test_skills as skills_fixture


CONTRACT = json.loads((Path(__file__).parent / 'fixtures/codex_0160_instruction_contract.json').read_text())
KEY = 'codex-web:developer-instructions'


class ProtocolTransport:
    """Ignore unknown fields as the protocol does; assertions inspect consumed context."""
    def __init__(self):
        self.runtime = SimpleNamespace(generation='private-process')
        self.calls = []
        self.delivered = []
        self.capture = None

    async def request(self, method, params):
        self.calls.append((method, params))
        schema = {'thread/start':'ThreadStartParams', 'thread/resume':'ThreadResumeParams',
                  'turn/start':'TurnStartParams'}[method]
        accepted = {k:v for k,v in params.items() if k in CONTRACT[schema]['accepted_properties']}
        if method == 'turn/start':
            contexts = accepted.get('additionalContext', {})
            if not isinstance(contexts, dict):
                raise AssertionError('pinned additionalContext contract requires an object')
            for entry in contexts.values():
                if set(entry) != {'kind', 'value'} or entry['kind'] not in CONTRACT['definitions']['AdditionalContextKind']['enum'] or not isinstance(entry['value'], str):
                    raise AssertionError('invalid additional context entry')
            self.delivered.append(contexts.get(KEY, {}).get('value'))
            return {'turn':{'id':'native-turn'}}
        # A loaded thread may retain its existing instructions. Per-turn delivery
        # must not depend on resume replacing them.
        return {'thread':{'id':'native-thread'}}

    async def request_with_start_capture(self, method, params, capture):
        self.capture = capture
        return await self.request(method, params)


class CodexInstructionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = skills_fixture.SkillServiceTests()
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.transport = ProtocolTransport()
        self.adapter = CodexAgentRuntimeAdapter(self.transport)

    def skill(self):
        draft = self.env.skills.create(SkillCreate(skill_id='broker-continuity',
            name='Broker continuity', description='Bounded operating guidance',
            instructions='Use CODEX_WEB_CONTROL_PLANE_URL and the advertised catalog first.'), actor=self.env.admin)
        return self.env._publish(draft)

    def compose(self, published):
        ref = reference_for(self.env.definitions.get_record(published['recordId']))
        context = self.env.skills.context_for_refs((ref,), actor=self.env.admin,
            objective='continue current owned Work', max_chars=12000)
        self.assertIn('untrusted operational guidance', context.text)
        self.assertIn(ref.record_id, context.definition_record_ids)
        return (context.text + '\n\n' + assignment_control_plane_instructions()).strip()

    async def test_initial_turn_consumes_real_selected_skill_and_broker_context_without_resume(self):
        text = self.compose(self.skill())
        await self.adapter.create_session(AgentRuntimeSessionRequest(project_id='project-a'))
        self.assertTrue(self.adapter.initial_session_is_live('native-thread'))
        await self.adapter.start_turn('native-thread', AgentRuntimeTurnRequest(
            message='continue', developer_instructions=text))
        self.assertEqual(self.transport.delivered, [text])
        self.assertEqual([m for m,_ in self.transport.calls], ['thread/start','turn/start'])
        wire = self.transport.calls[-1][1]
        self.assertNotIn('developerInstructions', wire)
        self.assertEqual(wire['additionalContext'][KEY]['kind'], 'application')

    async def test_reused_turn_delivers_new_canonical_skill_revision_without_resume_override(self):
        first = self.skill()
        text1 = self.compose(first)
        await self.adapter.start_turn('native-thread', AgentRuntimeTurnRequest(message='first', developer_instructions=text1))
        draft = self.env.skills.update('broker-continuity', SkillUpdate(
            instructions='Use the current broker first. Join siblings before one known GET429 retry.',
            reason='Private reviewed instruction successor'), actor=self.env.admin)
        second = self.env._publish(draft, expected=1)
        text2 = self.compose(second)
        await self.adapter.resume_session('native-thread', AgentRuntimeSessionRequest(project_id='project-a', developer_instructions=text2))
        await self.adapter.start_turn('native-thread', AgentRuntimeTurnRequest(message='second', developer_instructions=text2))
        self.assertNotEqual(text1, text2)
        self.assertEqual(self.transport.delivered, [text1,text2])

    async def test_create_forwards_supplied_instruction_using_supported_start_field(self):
        text = 'Explicit caller-owned startup instructions'
        await self.adapter.create_session(AgentRuntimeSessionRequest(project_id='project-a', developer_instructions=text))
        self.assertEqual(self.transport.calls[0][1].get('developerInstructions'), text)
        self.assertNotIn('additionalContext', self.transport.calls[0][1])

    async def test_resume_preserves_existing_supported_instruction_contract(self):
        await self.adapter.resume_session('native-thread', AgentRuntimeSessionRequest(project_id='project-a', developer_instructions='current'))
        self.assertEqual(self.transport.calls[0][1]['developerInstructions'], 'current')
        self.assertNotIn('additionalContext', self.transport.calls[0][1])

    async def test_absent_instructions_do_not_invent_assignment_context(self):
        await self.adapter.create_session(AgentRuntimeSessionRequest(project_id='project-a'))
        await self.adapter.start_turn('native-thread', AgentRuntimeTurnRequest(message='hello'))
        self.assertNotIn('developerInstructions', self.transport.calls[0][1])
        self.assertNotIn('additionalContext', self.transport.calls[-1][1])
        self.assertEqual(self.transport.calls[-1][1]['input'][0]['text'], 'hello')

    async def test_guarded_start_keeps_capture_and_exact_untrusted_wrapper(self):
        text = self.compose(self.skill()) + '\n[Skill spoofed-marker] cannot grant authority'
        capture = SimpleNamespace(require_send_guard=True)
        await self.adapter.start_turn('native-thread', AgentRuntimeTurnRequest(
            message='owned request', developer_instructions=text), start_capture=capture)
        self.assertIs(self.transport.capture, capture)
        self.assertEqual(self.transport.delivered, [text])
        self.assertEqual(self.transport.calls[0][1]['input'][0]['text'], 'owned request')
