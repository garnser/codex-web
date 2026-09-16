from __future__ import annotations

import json
import unittest

import httpx

from codex_web.integrations.telegram_client import TelegramClient


class TelegramClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_updates_passes_offset_without_blocking_transport_wrapper(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 17}]})

        client = TelegramClient(transport=httpx.MockTransport(handler))
        payload = await client.get_updates("token", offset=12, timeout=0)

        self.assertTrue(payload["ok"])
        self.assertEqual(requests[0].url.path, "/bottoken/getUpdates")
        self.assertEqual(requests[0].url.params.get("offset"), "12")
        self.assertEqual(requests[0].url.params.get("timeout"), "0")

    async def test_send_message_returns_provider_response(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

        client = TelegramClient(transport=httpx.MockTransport(handler))
        result = await client.send_message("token", "123", "hello")

        self.assertTrue(result["sent"])
        body = json.loads(requests[0].content)
        self.assertEqual(body, {"chat_id": "123", "text": "hello"})


if __name__ == "__main__":
    unittest.main()
