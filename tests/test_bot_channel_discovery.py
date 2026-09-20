from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace

from codex_web.models import BotBinding, BotConnection
from codex_web.services.bot_channels import BotChannelDiscoveryService


class _Projects:
    def get(self, project_id: str):
        if project_id not in {"home", "other"}:
            raise AssertionError(f"unexpected project: {project_id}")
        return SimpleNamespace(id=project_id)


class _Connections:
    def __init__(self, rows: list[BotConnection]) -> None:
        self.rows = rows
        self.loads = 0

    def load_connections(self) -> list[BotConnection]:
        self.loads += 1
        return [row.model_copy(deep=True) for row in self.rows]

    def runtime_actor(self, _project_id: str):
        return None


class _Bindings:
    def __init__(self, rows: list[BotBinding] | None = None) -> None:
        self.rows = rows or []
        self.loads = 0

    def load_bindings(self) -> list[BotBinding]:
        self.loads += 1
        return [row.model_copy(deep=True) for row in self.rows]


class _Slack:
    def __init__(self) -> None:
        self.list_calls: list[str] = []
        self.info_calls: list[tuple[str, str]] = []
        self.list_result: list[dict[str, str]] = []
        self.info_results: dict[str, dict[str, str] | None] = {}
        self.info_error: Exception | None = None
        self.info_delay = 0.0
        self.active_info = 0
        self.max_active_info = 0

    async def list_channels(self, token: str) -> list[dict[str, str]]:
        self.list_calls.append(token)
        return [dict(item) for item in self.list_result]

    async def channel_info(
        self,
        token: str,
        channel_id: str,
    ) -> dict[str, str] | None:
        self.info_calls.append((token, channel_id))
        self.active_info += 1
        self.max_active_info = max(
            self.max_active_info,
            self.active_info,
        )
        try:
            if self.info_delay:
                await asyncio.sleep(self.info_delay)
            if self.info_error is not None:
                raise self.info_error
            result = self.info_results.get(channel_id)
            return dict(result) if result is not None else None
        finally:
            self.active_info -= 1


def _connection(
    connection_id: str,
    *,
    token: str = "xoxb-shared",
    channel: str | None = None,
    name: str | None = None,
    project_id: str = "home",
) -> BotConnection:
    return BotConnection(
        id=connection_id,
        provider="slack",
        name=f"Slack {connection_id}",
        project_id=project_id,
        bot_token=token,
        default_external_conversation_id=channel,
        default_external_name=name,
        created_at=1.0,
        updated_at=1.0,
    )


def _binding(
    binding_id: str,
    *,
    connection_id: str,
    channel: str,
) -> BotBinding:
    return BotBinding(
        id=binding_id,
        connection_id=connection_id,
        provider="slack",
        external_conversation_id=channel,
        thread_id=f"thread-{binding_id}",
        project_id="home",
        created_at=1.0,
        updated_at=1.0,
    )


class BotChannelDiscoveryPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_equivalent_connections_share_one_list_and_one_info_per_channel(self) -> None:
        connections = _Connections(
            [
                _connection("c1", channel="C1"),
                _connection("c2", channel="C2"),
                _connection("c3", channel="C3"),
                _connection("c4", channel="C1"),
                _connection("c5", channel="C2"),
            ]
        )
        bindings = _Bindings()
        slack = _Slack()
        slack.info_results = {
            channel_id: {
                "provider": "slack",
                "id": channel_id,
                "name": f"name-{channel_id}",
                "label": f"#name-{channel_id}",
            }
            for channel_id in ("C1", "C2", "C3")
        }
        service = BotChannelDiscoveryService(
            connections=connections,
            bindings=bindings,
            projects=_Projects(),
            slack_client=slack,
        )

        result = await service.refresh("home")

        self.assertEqual(len(result), 3)
        self.assertEqual(slack.list_calls, ["xoxb-shared"])
        self.assertEqual(
            {channel_id for _token, channel_id in slack.info_calls},
            {"C1", "C2", "C3"},
        )
        self.assertEqual(len(slack.info_calls), 3)
        metrics = service.status("home")
        self.assertEqual(metrics["credentialGroups"], 1)
        self.assertEqual(metrics["listCalls"], 1)
        self.assertEqual(metrics["metadataLookupCount"], 3)
        self.assertEqual(metrics["providerCalls"], 4)
        self.assertEqual(connections.loads, 1)
        self.assertEqual(bindings.loads, 1)

    async def test_list_channel_names_avoid_metadata_fallback(self) -> None:
        connections = _Connections([_connection("c1", channel="C1")])
        slack = _Slack()
        slack.list_result = [
            {
                "provider": "slack",
                "id": "C1",
                "name": "general",
                "label": "#general",
            }
        ]
        service = BotChannelDiscoveryService(
            connections=connections,
            bindings=_Bindings(),
            projects=_Projects(),
            slack_client=slack,
        )

        result = await service.refresh("home")

        self.assertEqual(result[0]["label"], "#general")
        self.assertEqual(len(slack.list_calls), 1)
        self.assertEqual(slack.info_calls, [])
        self.assertEqual(service.status("home")["unresolvedCount"], 0)

    async def test_twenty_equivalent_connections_and_fifty_channels_stay_bounded(self) -> None:
        connections = _Connections(
            [
                _connection(
                    f"c{index}",
                    channel=f"C{index % 10:02d}",
                )
                for index in range(20)
            ]
        )
        bindings = _Bindings(
            [
                _binding(
                    f"b{index}",
                    connection_id=f"c{index % 20}",
                    channel=f"C{index:02d}",
                )
                for index in range(50)
            ]
        )
        slack = _Slack()
        slack.info_results = {
            f"C{index:02d}": {
                "provider": "slack",
                "id": f"C{index:02d}",
                "name": f"name-{index}",
                "label": f"#name-{index}",
            }
            for index in range(50)
        }
        service = BotChannelDiscoveryService(
            connections=connections,
            bindings=bindings,
            projects=_Projects(),
            slack_client=slack,
        )

        await service.refresh("home")

        metrics = service.status("home")
        self.assertEqual(metrics["credentialGroups"], 1)
        self.assertEqual(metrics["listCalls"], 1)
        self.assertLessEqual(metrics["metadataLookupCount"], 50)
        self.assertLessEqual(metrics["providerCalls"], 51)
        self.assertLess(len(slack.info_calls), 20 * 50)

    async def test_metadata_lookup_concurrency_is_bounded(self) -> None:
        connection = _connection("c1")
        bindings = _Bindings(
            [
                _binding(
                    f"b{index}",
                    connection_id="c1",
                    channel=f"C{index:02d}",
                )
                for index in range(24)
            ]
        )
        slack = _Slack()
        slack.info_delay = 0.01
        slack.info_results = {
            f"C{index:02d}": {
                "provider": "slack",
                "id": f"C{index:02d}",
                "name": f"name-{index}",
                "label": f"#name-{index}",
            }
            for index in range(24)
        }
        service = BotChannelDiscoveryService(
            connections=_Connections([connection]),
            bindings=bindings,
            projects=_Projects(),
            slack_client=slack,
        )
        service.MAX_METADATA_CONCURRENCY = 4

        await service.refresh("home")

        self.assertEqual(len(slack.info_calls), 24)
        self.assertLessEqual(slack.max_active_info, 4)
        self.assertEqual(
            service.status("home")["metadataLookupCount"],
            24,
        )

    async def test_negative_cache_prevents_repeated_missing_channel_lookup(self) -> None:
        connections = _Connections([_connection("c1", channel="C1")])
        slack = _Slack()
        service = BotChannelDiscoveryService(
            connections=connections,
            bindings=_Bindings(),
            projects=_Projects(),
            slack_client=slack,
        )

        await service.refresh("home")
        self.assertEqual(len(slack.info_calls), 1)

        service.cache.clear()
        await service.refresh("home")

        self.assertEqual(len(slack.list_calls), 2)
        self.assertEqual(len(slack.info_calls), 1)
        self.assertEqual(
            service.status("home")["negativeCacheHits"],
            1,
        )

    async def test_cached_discovery_returns_without_provider_calls(self) -> None:
        connections = _Connections(
            [_connection("c1", channel="C1", name="general")]
        )
        slack = _Slack()
        service = BotChannelDiscoveryService(
            connections=connections,
            bindings=_Bindings(),
            projects=_Projects(),
            slack_client=slack,
        )

        first = await service.refresh("home")
        list_calls = len(slack.list_calls)
        second = await service.list("home")

        self.assertEqual(first, second)
        self.assertEqual(len(slack.list_calls), list_calls)
        metrics = service.status("home")
        self.assertTrue(metrics["cacheHit"])
        self.assertEqual(metrics["providerCalls"], 0)

    async def test_rate_limit_sets_group_cooldown_and_prevents_retry_storm(self) -> None:
        connections = _Connections([_connection("c1", channel="C1")])
        slack = _Slack()
        response = SimpleNamespace(
            status_code=429,
            headers={"Retry-After": "120"},
        )
        error = RuntimeError("rate limited")
        error.response = response
        slack.info_error = error
        service = BotChannelDiscoveryService(
            connections=connections,
            bindings=_Bindings(),
            projects=_Projects(),
            slack_client=slack,
        )
        service.MAX_METADATA_CONCURRENCY = 1

        await service.refresh("home")
        self.assertEqual(len(slack.list_calls), 1)
        self.assertEqual(len(slack.info_calls), 1)
        self.assertEqual(service.status("home")["rateLimitEvents"], 1)

        service.cache.clear()
        await service.refresh("home")

        self.assertEqual(len(slack.list_calls), 1)
        self.assertEqual(len(slack.info_calls), 1)
        self.assertGreaterEqual(
            service.status("home")["cooldownSkips"],
            1,
        )


    async def test_normal_list_returns_known_channels_before_slow_provider_refresh(self) -> None:
        connections = _Connections(
            [_connection("c1", channel="C1", name="known")]
        )
        slack = _Slack()
        slack.info_delay = 0.2
        service = BotChannelDiscoveryService(
            connections=connections,
            bindings=_Bindings(),
            projects=_Projects(),
            slack_client=slack,
        )

        result = await asyncio.wait_for(
            service.list("home"),
            timeout=0.05,
        )

        self.assertEqual(result[0]["label"], "#known")
        self.assertEqual(slack.list_calls, [])
        self.assertTrue(service.status("home")["refreshScheduled"])
        await service.wait_for_refresh("home")
        self.assertEqual(slack.list_calls, ["xoxb-shared"])

    async def test_project_invalidation_keeps_unrelated_failure_caches(self) -> None:
        home = _connection("home-c", project_id="home")
        other = _connection("other-c", project_id="other", token="xoxb-other")
        service = BotChannelDiscoveryService(
            connections=_Connections([home, other]),
            bindings=_Bindings(),
            projects=_Projects(),
            slack_client=_Slack(),
        )
        home_group = service._credential_group_key(home)
        other_group = service._credential_group_key(other)
        assert home_group is not None
        assert other_group is not None
        service.negative_cache[
            ("home", home_group, "C1")
        ] = time.time() + 60
        service.negative_cache[
            ("other", other_group, "C2")
        ] = time.time() + 60
        service.cooldowns[("home", home_group)] = time.time() + 60
        service.cooldowns[("other", other_group)] = time.time() + 60

        service.invalidate("home")

        self.assertNotIn(("home", home_group, "C1"), service.negative_cache)
        self.assertNotIn(("home", home_group), service.cooldowns)
        self.assertIn(("other", other_group, "C2"), service.negative_cache)
        self.assertIn(("other", other_group), service.cooldowns)


if __name__ == "__main__":
    unittest.main()
