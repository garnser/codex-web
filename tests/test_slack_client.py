from __future__ import annotations

import json
import unittest

import httpx

from codex_web.integrations.slack_client import SlackClient


class SlackClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_channels_paginates_asynchronously(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            cursor = request.url.params.get("cursor")
            if not cursor:
                payload = {
                    "ok": True,
                    "channels": [{"id": "C1", "name": "general"}],
                    "response_metadata": {"next_cursor": "next"},
                }
            else:
                payload = {
                    "ok": True,
                    "channels": [{"id": "C2", "name_normalized": "builds"}],
                    "response_metadata": {"next_cursor": ""},
                }
            return httpx.Response(200, json=payload)

        client = SlackClient(transport=httpx.MockTransport(handler))
        channels = await client.list_channels("xoxb-test")

        self.assertEqual([channel["id"] for channel in channels], ["C1", "C2"])
        self.assertEqual([channel["label"] for channel in channels], ["#general", "#builds"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].headers["authorization"], "Bearer xoxb-test")

    async def test_channel_info_handles_slack_error_without_fabricating_name(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps({"ok": False, "error": "channel_not_found"}))

        client = SlackClient(transport=httpx.MockTransport(handler))

        self.assertIsNone(await client.channel_info("token", "C404"))

    async def test_socket_url_uses_async_web_api(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "url": "wss://wss-primary.slack.com/link"})

        client = SlackClient(transport=httpx.MockTransport(handler))

        url = await client.socket_url("xapp-test")

        self.assertEqual(url, "wss://wss-primary.slack.com/link")
        self.assertEqual(requests[0].url.path, "/api/apps.connections.open")
        self.assertEqual(requests[0].headers["authorization"], "Bearer xapp-test")

    async def test_post_message_retries_without_custom_identity(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, json={"ok": False, "error": "invalid_arguments"})
            return httpx.Response(200, json={"ok": True, "ts": "123.45"})

        client = SlackClient(transport=httpx.MockTransport(handler))
        result = await client.post_message(
            "xoxb-test",
            "C1",
            "hello",
            username="Agent",
            icon_emoji=":robot_face:",
            thread_ts="100.00",
        )

        self.assertTrue(result["sent"])
        self.assertEqual(len(requests), 2)
        first = json.loads(requests[0].content)
        second = json.loads(requests[1].content)
        self.assertEqual(first["username"], "Agent")
        self.assertEqual(first["icon_emoji"], ":robot_face:")
        self.assertNotIn("username", second)
        self.assertNotIn("icon_emoji", second)
        self.assertEqual(second["thread_ts"], "100.00")


if __name__ == "__main__":
    unittest.main()
