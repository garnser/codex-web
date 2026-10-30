"""Separate finite guarded execution from cumulative provider HTTP time."""
import asyncio
import unittest
from unittest.mock import patch

import httpx

from codex_web.code_host_read_budget import ReadExecutionBudget
from codex_web.integrations.github_client import GitHubClient
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.github_merge_requirements import GitHubMergeRequirementsReader
from codex_web.services.gitlab_merge_requirements import GitLabMergeRequirementsReader
from tests import test_github_merge_requirements as github
from tests import test_gitlab_merge_requirements as gitlab


class Clocks:
    def __init__(self): self.wall, self.monotonic = 1000., 0.
    def advance(self, seconds):
        self.wall += seconds
        self.monotonic += seconds
    def budget(self, deadline=1120.):
        return ReadExecutionBudget(deadline, clock=lambda:self.wall,
                                   monotonic=lambda:self.monotonic)


class ReadExecutionBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_cumulative_io_over_thirty_refuses_without_resetting_per_request(self):
        clocks = Clocks(); budget = clocks.budget()
        calls = []
        async def request(seconds):
            calls.append(seconds); clocks.advance(seconds); return 'observed'
        self.assertEqual(await budget.request(request(15.5)), 'observed')
        with self.assertRaises(TimeoutError): await budget.request(request(15.5))
        self.assertEqual(budget.network_seconds_used, 31)
        pending = request(1)
        with self.assertRaises(TimeoutError): await budget.request(pending)
        self.assertIsNone(pending.cr_frame)
        self.assertEqual(calls, [15.5, 15.5])

    async def test_failed_io_is_charged_and_cancellation_is_preserved(self):
        clocks = Clocks(); budget = clocks.budget()
        failure = OSError('synthetic transport failure')
        async def fail(): clocks.advance(20); raise failure
        with self.assertRaises(OSError) as raised: await budget.request(fail())
        self.assertIs(raised.exception, failure)
        self.assertEqual(budget.network_seconds_used, 20)
        async def cancel(): clocks.advance(2); raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await budget.request(cancel())
        self.assertEqual(budget.network_seconds_used, 22)
        async def finish(): clocks.advance(9)
        with self.assertRaises(TimeoutError): await budget.request(finish())

    async def test_wall_deadline_refuses_before_or_after_io_without_exposing_result(self):
        clocks = Clocks(); budget = clocks.budget(deadline=1001.)
        called = []
        async def request(): called.append(True); clocks.advance(2); return 'private result'
        with self.assertRaises(TimeoutError): await budget.request(request())
        self.assertEqual(called, [True])
        pending = request()
        with self.assertRaises(TimeoutError): await budget.request(pending)
        self.assertIsNone(pending.cr_frame)
        self.assertEqual(called, [True])
        for value in (float('inf'), float('nan'), True):
            with self.subTest(deadline=value), self.assertRaises(ValueError): ReadExecutionBudget(value)


class GuardedProviderReaderTests(unittest.IsolatedAsyncioTestCase):
    def reader(self, provider, clocks, *, guard=None, network=None, guarded=True):
        if provider == 'gitlab':
            fixture = gitlab.GitLabMergeRequirementsTests()
            fixture.setUp()
            response, binding, resource = fixture.response, fixture.binding, fixture.resource
            secret, client_type, reader_type = 'fixture-secret', GitLabClient, GitLabMergeRequirementsReader
        else:
            fixture = github._Provider()
            response, binding, resource = fixture.response, fixture.binding, fixture.resource
            secret, client_type, reader_type = 'fixture-token', GitHubClient, GitHubMergeRequirementsReader
        def transport(request):
            if network is not None: network()
            return response(request)
        budget = clocks.budget() if guarded else None
        reader = reader_type(client_type(transport=httpx.MockTransport(transport)),
            binding, resource, 'acme/widgets', secret, checkpoint=guard,
            clock=lambda:clocks.wall, observation_seconds=120, execution_budget=budget)
        return reader, fixture, budget

    async def test_real_readers_allow_guard_work_over_thirty_inside_shared_wall(self):
        for provider in ('gitlab', 'github'):
            clocks = Clocks()
            async def guard(): clocks.advance(2.5)
            reader, fixture, budget = self.reader(provider, clocks, guard=guard)
            with self.subTest(provider=provider):
                fact = await reader.read('7')
                self.assertEqual(fact.external_id, '7')
                self.assertGreater(clocks.wall - 1000, 30)
                self.assertLess(clocks.wall, budget.deadline_at)
                self.assertEqual(budget.network_seconds_used, 0)
                self.assertNotIn('observation_stale', fact.blockers)
                self.assertTrue(all(call.method in {'GET','POST'} for call in fixture.calls))

    async def test_real_rest_and_graphql_reads_share_cumulative_io_budget(self):
        for provider in ('gitlab', 'github'):
            clocks = Clocks(); count = 0
            def network():
                nonlocal count
                count += 1; clocks.advance(11 if count == 3 else 10)
            reader, fixture, budget = self.reader(provider, clocks, network=network)
            with self.subTest(provider=provider), self.assertRaises(TimeoutError):
                await reader.read('7')
            self.assertEqual(len(fixture.calls), 3)  # Third await completed but must not expose its value.
            self.assertEqual(reader.calls, 3)
            self.assertEqual(budget.network_seconds_used, 31)

    async def test_current_before_and_after_guards_still_refuse_without_further_io(self):
        for provider in ('gitlab', 'github'):
            for fail_at in (1,2):
                clocks = Clocks(); count = 0; refusal = ValueError('canonical authority revoked')
                async def guard():
                    nonlocal count
                    count += 1
                    if count == fail_at: raise refusal
                reader, fixture, _ = self.reader(provider, clocks, guard=guard)
                with self.subTest(provider=provider, fail_at=fail_at), self.assertRaises(ValueError) as raised:
                    await reader.read('7')
                self.assertIs(raised.exception, refusal)
                self.assertEqual(len(fixture.calls), fail_at // 2)

    async def test_final_projection_cannot_return_after_parent_deadline(self):
        for provider in ('gitlab', 'github'):
            clocks = Clocks()
            reader, _, _ = self.reader(provider, clocks)
            method = 'result' if provider == 'gitlab' else '_result'
            original = getattr(reader, method)
            def expire():
                value = original(); clocks.advance(121); return value
            with self.subTest(provider=provider), patch.object(reader, method, side_effect=expire):
                with self.assertRaises(TimeoutError): await reader.read('7')

    async def test_final_guard_remains_inside_absolute_wall_timeout(self):
        clocks = Clocks(); reader, _, _ = self.reader('gitlab', clocks)
        entered = asyncio.Event()
        async def blocked():
            entered.set()
            await asyncio.Event().wait()
        real_timeout = asyncio.timeout
        # Scale the existing wall timer only; no provider or canonical guard is waived.
        with patch.object(reader, 'guard_final', side_effect=blocked), \
                patch('codex_web.services.gitlab_merge_requirements.asyncio.timeout',
                      side_effect=lambda _: real_timeout(.1)):
            with self.assertRaises(TimeoutError): await reader.read('7')
        self.assertTrue(entered.is_set())
