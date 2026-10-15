import asyncio
import json
import logging
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from codex_web.execution_workers import AssignmentStatus, WorkerHeartbeatRequest
from codex_web.services.execution_workers import WorkerConflictError
from codex_web.observability import JsonFormatter
from tests.runner_failure_fixture import RunnerFailureFixture


class RunnerFailureCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_child_cleanup_diagnostic_unavailable_scoped_route_and_journal(self):
        fixture = await RunnerFailureFixture().start()
        try:
            with self.assertLogs('codex_web.services.agent_process_session', logging.ERROR) as logs:
                await fixture.fail()
            self.assertIsNotNone(fixture.process.poll())
            self.assertFalse(Path(f'/proc/{fixture.process.pid}').exists(), 'owned child must be reaped')
            self.assertFalse(fixture.session._executor_work)
            self.assertEqual(fixture.session.diagnostic_state, 'unavailable')
            self.assertEqual(fixture.evidence.list_evidence(fixture.admin), [])
            self.assertEqual(fixture.worker_service.store.assignment(fixture.session.assignment_id).status,
                             AssignmentStatus.RUNNING, 'failed diagnostic cannot fabricate canonical completion')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture.app), base_url='http://fixture') as client:
                response = await client.get('/api/threads/failure-thread/runner?project_id=home')
                self.assertEqual(response.status_code, 200, response.text)
                row = response.json()
                runner = row['runner']
                self.assertEqual((runner['state'], runner['live'], runner['diagnosticState']), ('stopped', False, 'unavailable'))
                self.assertEqual(row['activity']['state'], 'unknown')
                observation = runner['failureObservation']
                self.assertEqual(observation['assignment_id'], runner['assignmentId'])
                self.assertEqual(observation['fence'], runner['fence'])
                self.assertEqual((observation['code'], observation['phase'], observation['cleanup_state']),
                                 ('resource_limit', 'validation', 'stopped'))
                self.assertEqual(observation['outcome'], 'unknown')
                self.assertIn('validation_wait_seconds', observation['timings'])
                self.assertIn('identity_seconds', observation['timings'])
                self.assertEqual(observation['resource_ids'], ['repo-1'])
                self.assertEqual(observation['requester_identity_id'], fixture.admin.identity_id)
                self.assertEqual((await client.get('/api/threads/failure-thread/runner?project_id=foreign')).status_code, 404)
                fixture.actor = fixture.admin.model_copy(update={'organization_id': 'foreign'})
                self.assertEqual((await client.get('/api/threads/failure-thread/runner?project_id=home')).status_code, 404)
            self.assertEqual(fixture.service.thread_history.thread('failure-thread')['turns'][0]['status'], 'inProgress')
            self.assertEqual(fixture.git_status(), fixture.git_status_before)
            for name, content in fixture.files.items():
                self.assertEqual((fixture.workspace_path / name).read_text(), content)
            entries = [json.loads(JsonFormatter().format(record)) for record in logs.records]
            final = entries[-1]
            self.assertEqual(final['tenant_id'], fixture.admin.organization_id)
            self.assertEqual(final['workspace_id'], fixture.admin.workspace_id)
            self.assertEqual(final['diagnostic_state'], 'unavailable')
            self.assertEqual(final['observation']['cleanup_state'], 'stopped')
            self.assertNotIn('lease_token', json.dumps(entries))
            self.assertNotIn(fixture.delegation.secret, json.dumps(entries))
            self.assertNotIn('time.sleep', json.dumps(entries))
        finally:
            await fixture.close()

    async def test_failure_persists_existing_evidence_and_sink_failure_cannot_prevent_retirement(self):
        fixture = await RunnerFailureFixture().start()
        try:
            with patch('codex_web.services.agent_process_session.log_event', side_effect=OSError('sink unavailable')):
                await fixture.fail(diagnostic_unavailable=False)
            self.assertIsNotNone(fixture.process.poll())
            evidence = fixture.evidence.list_evidence(fixture.admin)
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0].metadata['cleanup_state'], 'stopped')
            self.assertEqual(evidence[0].metadata['outcome'], 'unknown')
            self.assertEqual(evidence[0].metadata['validation_custody_state'], 'unavailable')
            events = [event for event in fixture.worker_service.events(fixture.admin)
                      if event.event_type == 'agent_runtime_supervision_failed']
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].details['phase'], 'validation')
            self.assertEqual(events[0].details['cleanup_state'], 'stopped')
            with self.assertRaises(WorkerConflictError):
                fixture.worker_service.record_runtime_observation(fixture.worker.id, fixture.session.assignment_id,
                    observed_fence=fixture.session.fence, failure_code='resource_limit', timings={}, evidence_id=None,
                    phase='raw-exception-or-command', actor=fixture.worker_actor)
            self.assertEqual(fixture.session.status().failure_observation['evidence_id'], evidence[0].id)
            self.assertEqual(fixture.worker_service.store.assignment(fixture.session.assignment_id).status, AssignmentStatus.FAILED)
        finally:
            await fixture.close()

    async def test_slow_scope_request_cannot_starve_real_watchdog_or_canonical_heartbeat(self):
        fixture = await RunnerFailureFixture().start()
        entered, release = threading.Event(), threading.Event()
        original = fixture.sqlite.record_page
        def blocked(*args, **kwargs):
            if kwargs.get('key_prefix') == 'native:failure-thread:':
                entered.set()
                if not release.wait(4): raise TimeoutError('controlled read deadline')
            return original(*args, **kwargs)
        try:
            with patch.object(fixture.sqlite, 'record_page', side_effect=blocked):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture.app), base_url='http://fixture') as client:
                    request = asyncio.create_task(client.get('/api/threads/failure-thread/context?project_id=home'))
                    try:
                        async with asyncio.timeout(1):
                            while not entered.is_set(): await asyncio.sleep(.002)
                        started = time.monotonic()
                        await asyncio.wait_for(asyncio.to_thread(fixture.worker_service.heartbeat,
                            fixture.worker.id, WorkerHeartbeatRequest(version=fixture.worker.version),
                            actor=fixture.worker_actor), 1)
                        await fixture.fail(diagnostic_unavailable=False)
                        self.assertLess(time.monotonic()-started, 1.5)
                        self.assertIsNotNone(fixture.process.poll())
                        self.assertFalse(request.done(), 'scope must still be held while supervision progresses')
                    finally:
                        release.set()
                    self.assertEqual((await asyncio.wait_for(request, 2)).status_code, 200)
        finally:
            release.set()
            await fixture.close()

    async def test_unavailable_diagnostic_executor_still_cleans_up_and_retains_observation(self):
        fixture = await RunnerFailureFixture().start()
        try:
            fixture.session._diagnostic_executor.shutdown(wait=True)
            with self.assertLogs('codex_web.services.agent_process_session', logging.ERROR) as logs:
                await fixture.fail(diagnostic_unavailable=False)
            self.assertIsNotNone(fixture.process.poll())
            self.assertEqual(fixture.session.diagnostic_state, 'unavailable')
            self.assertEqual(logs.records[-1].structured['diagnostic_state'], 'unavailable')
            self.assertEqual(fixture.session.status().failure_observation['cleanup_state'], 'stopped')
            self.assertFalse(fixture.session._executor_work)
        finally:
            await fixture.close()
