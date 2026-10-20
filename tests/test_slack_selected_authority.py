"""Selected-route capture and typed pre-routing recovery, using real retention."""
import tempfile
import threading
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codex_web.conversation_channels import ConversationProjectionOutcome
from codex_web.definitions import DefinitionReference
from codex_web.models import ThreadRunSettings
from codex_web.services.conversation_ingress_authority import capture_conversation_ingress_authority
from tests.test_retained_bot_routing import routing_fixture
from tests import test_slack_ingress_retention_identity as retained_fixtures
from tests.slack_ingress_recovery_fixture import payload


class SelectedAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_unrelated_binding_changes_do_not_suppress_actual_selected_queue_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            f = routing_fixture(Path(directory), scratch=True)
            actor = f.connections.runtime_actor('fixture')
            selected = f.connections.load_bindings()[0]
            unrelated = selected.model_copy(update={'id':'other', 'thread_id':'other-thread'})
            f.connections.save_bindings([selected, unrelated])
            profile = {'profile_id':'orchestrator', 'revision':7, 'execution_profile_id':'restricted-execution'}
            profile_object = SimpleNamespace(execution_profile_id='restricted-execution', model_dump=lambda **_:dict(profile))
            execution = SimpleNamespace(repository_access='none', model_dump=lambda **_:{'id':'restricted-execution', 'repository_access':'none'})
            definition = DefinitionReference.model_validate(f.captured['definition'])
            inspected = []
            def settings(thread):
                inspected.append(thread)
                if thread != selected.thread_id:
                    raise AssertionError('unselected settings must never be read')
                return ThreadRunSettings.model_validate(f.captured['settings'])
            f.routing.presentation.binding_prefix = lambda binding: binding.route_prefix or binding.id
            def authority(event, current_actor):
                return capture_conversation_ingress_authority(event, current_actor,
                    connections=f.connections, routing=f.routing,
                    bindings=SimpleNamespace(by_id=lambda ident:next((row for row in f.connections.load_bindings() if row.id==ident),None)),
                    settings_service=SimpleNamespace(get=settings),
                    profile_for_binding=lambda binding, **_:(profile_object, None),
                    execution_profiles=SimpleNamespace(resolve=lambda *_,**kw:(execution, definition)), resources=None)
            f.service.authority_snapshot = authority
            f.service.source_authority_snapshot = f.connections.ingress_snapshot
            original = (await f.service.retain_raw('slack',f.connection.id,payload(),actor=actor,
                connection_id=f.connection.id,project_id='fixture'))[0]
            f.connections.save_bindings([selected,unrelated.model_copy(update={'thread_id':'replacement-other','updated_at':9})])
            event = f.service.pending_events(actor=actor,connection_id=f.connection.id,project_id='fixture')[0]
            receipt = await f.service.ingest(event,actor=actor)
            self.assertEqual(receipt.outcome.value,'routed')
            self.assertEqual(receipt.thread_id,selected.thread_id)
            self.assertEqual(len(f.queue.get(selected.thread_id)),1)
            self.assertEqual(set(inspected),{selected.thread_id})
            self.assertEqual(f.queue.get(selected.thread_id)[0].agent_profile_revision,7)
            # The original remains immutable, including its exact selected authority.
            content = f.service.retained_input(original,actor)
            self.assertEqual(len(content['authority_snapshot']['routing']),1)
            self.assertIsNone(content['authority_snapshot']['new_binding'])
            # Relevant role/Profile, repository settings and published definition
            # drift all remain fail-closed; none borrows the previous authority.
            for index,component in enumerate(('profile','settings','definition')):
                raw = payload()
                raw['event_id'] = f'changed-{component}'
                raw['event']['ts'] = str(200+index)
                original = (await f.service.retain_raw('slack',f.connection.id,raw,actor=actor,
                    connection_id=f.connection.id,project_id='fixture'))[0]
                event = (await f.service.registry.resolve('slack',f.connection.id).normalize_events(
                    raw,connection_id=f.connection.id,project_id='fixture'))[0]
                if component == 'profile':
                    profile['revision'] = 8
                elif component == 'settings':
                    f.captured['settings']['model'] = 'changed-model'
                else:
                    definition = definition.model_copy(update={'revision':2})
                refused = await f.service.ingest(event,actor=actor)
                self.assertEqual(refused.outcome.value,'requires_reconciliation')
                self.assertTrue(refused.routing_result['preRoutingAuthorityRefused'])
                self.assertEqual(len(f.queue.get(selected.thread_id)),1)
                profile['revision'] = 7
                f.captured['settings']['model'] = 'restricted-model'
                definition = definition.model_copy(update={'revision':1})


class AuthorityRefusalRecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = retained_fixtures.SlackRetentionIdentityTests.setUp
    tearDown = retained_fixtures.SlackRetentionIdentityTests.tearDown
    service = retained_fixtures.SlackRetentionIdentityTests.service
    payload = staticmethod(retained_fixtures.SlackRetentionIdentityTests.payload)
    retain_and_ack = retained_fixtures.SlackRetentionIdentityTests.retain_and_ack

    async def original(self):
        receipt = await self.retain_and_ack(self.first,self.payload())
        event = self.first.pending_events(actor=self.actor,connection_id='connection-a',project_id='project-a')[0]
        return receipt,event

    def pending(self):
        return self.first.pending_events(actor=self.actor,connection_id='connection-a',project_id='project-a')

    async def test_exact_original_authority_restoration_recovers_once_without_input_rewrite(self):
        original,event = await self.original()
        self.authority_revision = 2
        refused = await self.first.ingest(event,actor=self.actor)
        self.assertTrue(refused.routing_result['preRoutingAuthorityRefused'])
        self.assertIn('authority_changed:revision',refused.reason)
        self.assertEqual(self.pending(),())
        self.authority_revision = 1
        self.assertEqual(self.pending(),(event,))
        recovered = self.first.store.load().receipts[original.canonical_event_id]
        self.assertEqual(recovered.routing_result['authorityRecoveryCount'],1)
        self.assertEqual(recovered.protected_input,original.protected_input)
        self.assertEqual(recovered.authority_digest,original.authority_digest)
        self.authority_revision = 3
        second = await self.first.ingest(event,actor=self.actor)
        self.assertTrue(second.routing_result['preRoutingAuthorityRefused'])
        self.assertEqual(second.routing_result['authorityRecoveryCount'],1)
        self.authority_revision = 1
        self.assertEqual(self.pending(),())
        self.first.route_handler.assert_not_called()

    async def test_recovered_input_delivers_once_and_preserves_queue_identity(self):
        original,event = await self.original()
        self.authority_revision = 2
        await self.first.ingest(event,actor=self.actor)
        self.authority_revision = 1
        self.pending()
        self.first.route_handler = AsyncMock(return_value={'ok':True,'threadId':'selected','queuedId':'one-input'})
        receipt = await self.first.ingest(event,actor=self.actor)
        await self.first.ingest(event,actor=self.actor)
        self.first.route_handler.assert_awaited_once()
        self.assertEqual(receipt.queued_id,'one-input')
        self.assertEqual(self.pending(),())

    async def test_historical_generic_and_post_route_unknowns_never_recover(self):
        original,event = await self.original()
        for reason in ('retained authority no longer valid: ConversationChannelConflictError',
                       'routing claim is durable but downstream routing outcome is uncertain: TimeoutError'):
            refused = self.first._finish(original.canonical_event_id,actor=self.actor,
                outcome=ConversationProjectionOutcome.REQUIRES_RECONCILIATION,reason=reason)
            before = refused.model_dump(mode='json')
            self.assertEqual(self.pending(),())
            self.assertEqual(self.first.store.load().receipts[original.canonical_event_id].model_dump(mode='json'),before)
        self.first.route_handler.assert_not_called()

    async def test_authority_blocked_original_does_not_starve_new_pending_human(self):
        original,event = await self.original()
        self.authority_revision = 2
        await self.first.ingest(event,actor=self.actor)
        fresh = self.payload('fresh human input')
        fresh['event_id'] = 'fresh-delivery'
        fresh['event']['ts'] = '101.1'
        await self.retain_and_ack(self.first,fresh)
        pending = self.first.pending_events(actor=self.actor,connection_id='connection-a',project_id='project-a',limit=1)
        self.assertEqual(len(pending),1)
        self.assertEqual(pending[0].delivery_id,'fresh-delivery')

    async def test_concurrent_routing_claim_cannot_be_reclassified_as_unstarted(self):
        original,event = await self.original()
        original_snapshot = self.first.authority_snapshot
        entered,release = threading.Event(),threading.Event()
        def snapshot(event,actor):
            entered.set()
            if not release.wait(3):
                raise AssertionError('barrier expired')
            return original_snapshot(event,actor)
        self.first.authority_snapshot = snapshot
        task = asyncio.create_task(self.first.ingest(event,actor=self.actor))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,3))
            claimed,created = self.second._claim(canonical_event_id=original.canonical_event_id,event=event,actor=self.actor)
            self.assertTrue(created)
            self.authority_revision = 2
        finally:
            release.set()
        result = await task
        self.assertEqual(result.outcome.value,'routing')
        self.assertNotIn('preRoutingAuthorityRefused',result.routing_result)
        self.assertEqual(self.pending(),())
        self.first.route_handler.assert_not_called()
