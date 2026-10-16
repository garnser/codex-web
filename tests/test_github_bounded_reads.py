import gzip
import json
import unittest

import httpx

from codex_web.integrations.github_client import GitHubAPIError, GitHubClient, GitHubReadIncompleteError


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class GitHubBoundedReadTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, response, *, limit=512 * 1024):
        client = GitHubClient(transport=httpx.MockTransport(lambda request: response))
        return await client.read_json_bounded('https://api.github.example', 'fixed/read',
                                              token='private-fixture-token', max_bytes=limit)

    async def test_rest_and_fixed_graphql_queries_keep_scope_and_authentication(self):
        calls = []
        def transport(request):
            calls.append(request)
            return httpx.Response(200, json={'known': True})
        client = GitHubClient(transport=httpx.MockTransport(transport))
        self.assertEqual(await client.read_json_bounded('https://api.github.example/api/v3',
            'repos/acme/widgets/rules/branches/main', token='scoped-token', params={'per_page': 100, 'page': 2}), {'known': True})
        query = {'query': 'query { viewer { login } }', 'variables': {}}
        await client.read_json_bounded('https://api.github.example/api', 'graphql', token='scoped-token', query_body=query)
        self.assertEqual(calls[0].method, 'GET')
        self.assertEqual(calls[0].url.path, '/api/v3/repos/acme/widgets/rules/branches/main')
        self.assertEqual(dict(calls[0].url.params), {'per_page': '100', 'page': '2'})
        self.assertEqual(calls[1].method, 'POST')
        self.assertEqual(calls[1].url.path, '/api/graphql')
        self.assertEqual(json.loads(calls[1].content), query)
        self.assertTrue(all(call.headers['authorization'] == 'Bearer scoped-token' for call in calls))
        self.assertTrue(all(call.headers['accept-encoding'] == 'identity' for call in calls))

    async def test_byte_limit_stops_stream_and_never_returns_partial_empty_rules(self):
        stream = _Chunks([b'[', b' ' * 20, b']'])
        response = httpx.Response(200, headers={'content-type': 'application/json'}, stream=stream)
        with self.assertRaises(GitHubReadIncompleteError) as raised:
            await self.read(response, limit=16)
        self.assertEqual(raised.exception.reason, 'byte_limit')
        self.assertEqual(stream.reads, 2)
        self.assertTrue(stream.closed)

    async def test_exact_byte_limit_is_complete_only_after_real_end_of_stream(self):
        self.assertEqual(await self.read(httpx.Response(200, json=[]), limit=2), [])
        with self.assertRaises(GitHubReadIncompleteError) as raised:
            await self.read(httpx.Response(200, content=b'[]', headers={
                'content-type': 'application/json', 'content-length': '3'}))
        self.assertEqual(raised.exception.reason, 'content_length')

    async def test_invalid_ambiguous_and_non_container_json_cannot_be_empty_facts(self):
        bodies = [b'{', b'{"count":2,"count":0}', b'{"count":NaN}', b'{"count":Infinity}', b'{"count":1e9999}',
                  b'null', b'false', b'0', b'"empty"', b'\xff', b'[' * 2000 + b']' * 2000]
        bodies.extend([b'{"text":"\\ud800"}', b'{"\\udfff":0}'])
        for body in bodies:
            with self.subTest(body_prefix=body[:16]):
                with self.assertRaises(GitHubReadIncompleteError):
                    await self.read(httpx.Response(200, content=body, headers={'content-type': 'application/json'}))

    async def test_depth_bound_counts_containers_without_parsing_text_as_structure(self):
        await self.read(httpx.Response(200, content=b'[' * 64 + b']' * 64,
                                      headers={'content-type': 'application/json'}))
        with self.assertRaises(GitHubReadIncompleteError) as raised:
            await self.read(httpx.Response(200, content=b'[' * 65 + b']' * 65,
                                          headers={'content-type': 'application/json'}))
        self.assertEqual(raised.exception.reason, 'json_depth')
        value = {'text': '[' * 100, 'nested': []}
        self.assertEqual(await self.read(httpx.Response(200, json=value)), value)

    async def test_redirect_does_not_follow_to_another_route_or_send_credentials(self):
        calls = []
        def transport(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={'location': 'https://unrelated.example/facts'})
        client = GitHubClient(transport=httpx.MockTransport(transport))
        with self.assertRaises(GitHubReadIncompleteError) as raised:
            await client.read_json_bounded('https://api.github.example', 'fixed/read', token='private-token')
        self.assertEqual(raised.exception.reason, 'redirect')
        self.assertEqual(calls, ['https://api.github.example/fixed/read'])

    async def test_http_denial_and_absence_preserve_status_instead_of_becoming_zero(self):
        for status in (401, 403, 404, 429, 500):
            with self.subTest(status=status), self.assertRaises(GitHubAPIError) as raised:
                await self.read(httpx.Response(status, content=b'provider error detail'))
            self.assertEqual(raised.exception.status_code, status)

    async def test_unrequested_compression_is_rejected_before_decompressing_a_chunk(self):
        stream = _Chunks([gzip.compress(b'[' + b' ' * 2000 + b']')])
        response = httpx.Response(200, stream=stream,
                                 headers={'content-type': 'application/json', 'content-encoding': 'gzip'})
        with self.assertRaises(GitHubReadIncompleteError) as raised:
            await self.read(response, limit=16)
        self.assertEqual(raised.exception.reason, 'content_encoding')
        self.assertEqual(stream.reads, 0)
        self.assertTrue(stream.closed)

    async def test_wrong_media_and_invalid_limits_fail_before_interpreting_facts(self):
        with self.assertRaises(GitHubReadIncompleteError) as raised:
            await self.read(httpx.Response(200, content=b'[]', headers={'content-type': 'text/html'}))
        self.assertEqual(raised.exception.reason, 'media_type')
        for limit in (0, 512 * 1024 + 1):
            with self.assertRaises(ValueError):
                await self.read(httpx.Response(200, json=[]), limit=limit)

    async def test_only_bounded_unambiguous_error_metadata_establishes_unprotected_branch(self):
        with self.assertRaises(GitHubAPIError) as raised:
            await self.read(httpx.Response(404, json={'message': 'Branch not protected'}))
        self.assertEqual(raised.exception.provider_reason, 'branch_not_protected')
        for body, extra in [
            (b'{"message":"Not Found"}', {}),
            (b'{"message":"Branch not protected","message":"Not Found"}', {}),
            (b'{"message":"Branch not protected","count":NaN}', {}),
            (b'{"message":"Branch not protected"}', {'content-length': '1000'}),
            (b'{"message":"Branch not protected","extra":"' + b'x' * 9000 + b'"}', {}),
        ]:
            with self.subTest(prefix=body[:32]), self.assertRaises(GitHubAPIError) as raised:
                await self.read(httpx.Response(404, content=body, headers={'content-type': 'application/json', **extra}))
            self.assertIsNone(raised.exception.provider_reason)
