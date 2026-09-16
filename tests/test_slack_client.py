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


if __name__ == "__main__":
    unittest.main()
