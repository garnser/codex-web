from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.models import BotBinding, BotConnection, BotConnectionCreate
from codex_web.services.bot_connections import BotConnectionService, install_bot_connection_service


class Host:
    def __init__(self) -> None:
        self.connections: list[BotConnection] = []
        self.bindings: list[BotBinding] = []
        self.projects = {"home"}

    def _load_bot_connections(self) -> list[BotConnection]:
        return [connection.model_copy(deep=True) for connection in self.connections]

    def _save_bot_connections(self, connections: list[BotConnection]) -> None:
        self.connections = [connection.model_copy(deep=True) for connection in connections]

    def _load_bot_bindings(self) -> list[BotBinding]:
        return [binding.model_copy(deep=True) for binding in self.bindings]

    def _save_bot_bindings(self, bindings: list[BotBinding]) -> None:
        self.bindings = [binding.model_copy(deep=True) for binding in bindings]

    def _project(self, project_id: str) -> object:
        if project_id not in self.projects:
            raise AssertionError(f"unknown project: {project_id}")
        return object()

    @staticmethod
    def _binding_prefix(binding: BotBinding) -> str | None:
        return binding.route_prefix


def _service(host: Host) -> BotConnectionService:
    return BotConnectionService(
        load_connections=host._load_bot_connections,
        save_connections=host._save_bot_connections,
        load_bindings=host._load_bot_bindings,
        save_bindings=host._save_bot_bindings,
        projects=SimpleNamespace(get=host._project),
        binding_prefix=host._binding_prefix,
    )


class BotConnectionServiceTests(unittest.TestCase):
    def test_public_projection_masks_connection_secrets(self) -> None:
        host = Host()
        service = _service(host)
        connection = BotConnection(
            id="c1",
            provider="slack",
            name="Slack",
            project_id="home",
            bot_token="1234567890abcdef",
            slack_app_token="short",
            signing_secret=None,
            webhook_secret="abcdefghijkl",
            created_at=1,
            updated_at=1,
        )

        public = service.public(connection)

        self.assertEqual(public["bot_token"], "1234...cdef")
        self.assertEqual(public["slack_app_token"], "********")
        self.assertIsNone(public["signing_secret"])
        self.assertEqual(public["webhook_secret"], "abcd...ijkl")

    def test_upsert_reuses_connection_and_preserves_masked_secrets(self) -> None:
        host = Host()
        host.connections = [
            BotConnection(
                id="existing",
                provider="slack",
                name="Primary",
                project_id="home",
                bot_token="bot-secret",
                slack_app_token="app-secret",
                signing_secret="sign-secret",
                webhook_secret=None,
                default_external_conversation_id="C1",
                created_at=1,
                updated_at=1,
            )
        ]
        service = _service(host)

        updated = service.upsert(
            BotConnectionCreate(
                id="existing",
                provider="slack",
                name="Primary renamed",
                project_id="home",
                bot_token="********",
                slack_app_token=None,
                signing_secret="",
                default_external_conversation_id="C1",
            )
        )

        self.assertEqual(updated.id, "existing")
        self.assertEqual(updated.name, "Primary renamed")
        self.assertEqual(updated.bot_token, "bot-secret")
        self.assertEqual(updated.slack_app_token, "app-secret")
        self.assertEqual(updated.signing_secret, "sign-secret")
        self.assertEqual(len(host.connections), 1)

    def test_dedupe_rewrites_connection_ids_and_duplicate_binding_routes(self) -> None:
        host = Host()
        host.connections = [
            BotConnection(
                id="first",
                provider="slack",
                name="Primary",
                project_id="home",
                bot_token="token",
                default_external_conversation_id="C1",
                created_at=1,
                updated_at=1,
            ),
            BotConnection(
                id="duplicate",
                provider="slack",
                name="Primary",
                project_id="home",
                bot_token="token",
                default_external_conversation_id="C1",
                created_at=2,
                updated_at=2,
            ),
        ]
        host.bindings = [
            BotBinding(
                id="b1",
                connection_id="first",
                provider="slack",
                external_conversation_id="C1",
                thread_id="t1",
                project_id="home",
                route_prefix="agent",
                created_at=1,
                updated_at=1,
            ),
            BotBinding(
                id="b2",
                connection_id="duplicate",
                provider="slack",
                external_conversation_id="C1",
                thread_id="t1",
                project_id="home",
                route_prefix="agent",
                created_at=2,
                updated_at=2,
            ),
        ]

        _service(host).dedupe_integrations()

        self.assertEqual([connection.id for connection in host.connections], ["first"])
        self.assertEqual(len(host.bindings), 1)
        self.assertEqual(host.bindings[0].connection_id, "first")

    def test_installer_rebinds_historical_host_surface(self) -> None:
        host = Host()
        app = SimpleNamespace(state=SimpleNamespace())

        service = install_bot_connection_service(app, host)

        self.assertIs(app.state.bot_connection_service, service)
        self.assertIs(host._upsert_bot_connection.__self__, service)
        self.assertIs(host._update_bot_connection.__self__, service)
        self.assertIs(host._bot_connection_public.__self__, service)


if __name__ == "__main__":
    unittest.main()
