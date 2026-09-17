from __future__ import annotations

import unittest

from codex_web import application
from codex_web.runtime import core
from codex_web.services.bot_connections import BotConnectionService


class BotConnectionServiceExtractionTests(unittest.TestCase):
    def test_application_installs_bot_connection_service(self) -> None:
        service = application.bot_connection_service
        self.assertIs(application.app.state.bot_connection_service, service)
        self.assertIsInstance(service, BotConnectionService)

    def test_legacy_bot_connection_entrypoints_are_service_aliases(self) -> None:
        service = application.bot_connection_service
        self.assertIs(core._bot_connection.__self__, service)
        self.assertIs(core._bot_connection_for_conversation.__self__, service)
        self.assertIs(core._bot_connection_public.__self__, service)
        self.assertIs(core._connection_matches_payload.__self__, service)
        self.assertIs(core._upsert_bot_connection.__self__, service)
        self.assertIs(core._dedupe_bot_integrations.__self__, service)
        self.assertIs(core._update_bot_connection.__self__, service)
        self.assertIs(core._mask_secret, service.mask_secret)
        self.assertIs(core._connection_identity, service.identity)


if __name__ == "__main__":
    unittest.main()
