from __future__ import annotations

import asyncio
import unittest

from codex_web.events import EventHub


class FakeWebSocket:
    def __init__(self, *, gate: asyncio.Event | None = None) -> None:
        self.accepted = False
        self.gate = gate
        self.messages: list[dict[str, object]] = []
        self.message_received = asyncio.Event()
        self.close_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, event: dict[str, object]) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.messages.append(event)
        self.message_received.set()

    async def close(self, *, code: int = 1000) -> None:
        self.close_code = code


class EventHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_client_does_not_block_other_clients(self) -> None:
        hub = EventHub()
        slow_gate = asyncio.Event()
        slow = FakeWebSocket(gate=slow_gate)
        fast = FakeWebSocket()

        await hub.connect(slow)  # type: ignore[arg-type]
        await hub.connect(fast)  # type: ignore[arg-type]

        event = {"type": "status", "value": 1}
        await hub.publish(event)

        await asyncio.wait_for(fast.message_received.wait(), timeout=0.5)
        self.assertEqual(len(fast.messages), 1)
        delivered = fast.messages[0]
        self.assertEqual(delivered["type"], "status")
        self.assertEqual(delivered["value"], 1)
        self.assertEqual(delivered["eventSequence"], 1)
        self.assertEqual(
            delivered["eventStreamId"],
            hub.stream_state()["streamId"],
        )
        self.assertIn("eventPublishedAt", delivered)
        self.assertEqual(slow.messages, [])

        slow_gate.set()
        await asyncio.wait_for(slow.message_received.wait(), timeout=0.5)
        self.assertEqual(slow.messages, [delivered])

        hub.disconnect(slow)  # type: ignore[arg-type]
        hub.disconnect(fast)  # type: ignore[arg-type]
        await asyncio.sleep(0)

    async def test_stream_sequence_is_monotonic(self) -> None:
        hub = EventHub()
        client = FakeWebSocket()
        await hub.connect(client)  # type: ignore[arg-type]

        await hub.publish({"type": "one"})
        await hub.publish({"type": "two"})
        await asyncio.sleep(0)

        self.assertEqual(
            [item["eventSequence"] for item in client.messages],
            [1, 2],
        )
        self.assertEqual(hub.stream_state()["sequence"], 2)
        self.assertEqual(
            len({item["eventStreamId"] for item in client.messages}),
            1,
        )
        hub.disconnect(client)  # type: ignore[arg-type]
        await asyncio.sleep(0)

    async def test_full_client_queue_disconnects_only_that_client(self) -> None:
        hub = EventHub(queue_size=1)
        gate = asyncio.Event()
        slow = FakeWebSocket(gate=gate)
        fast = FakeWebSocket()

        await hub.connect(slow)  # type: ignore[arg-type]
        await hub.connect(fast)  # type: ignore[arg-type]

        await hub.publish({"type": "one"})
        await asyncio.sleep(0)
        self.assertEqual(len(fast.messages), 1)

        fast.message_received.clear()
        await hub.publish({"type": "two"})
        await asyncio.wait_for(fast.message_received.wait(), timeout=0.5)
        self.assertEqual(len(fast.messages), 2)

        fast.message_received.clear()
        await hub.publish({"type": "three"})
        await asyncio.wait_for(fast.message_received.wait(), timeout=0.5)

        self.assertNotIn(slow, hub._clients)
        self.assertEqual(slow.close_code, 1013)
        self.assertIn(fast, hub._clients)
        self.assertEqual(len(fast.messages), 3)

        gate.set()
        hub.disconnect(fast)  # type: ignore[arg-type]
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
