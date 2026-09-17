from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from codex_web import application
from codex_web.runtime import core
from codex_web.services.threads import ThreadService


class ThreadMessageLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ThreadService(object())

    def test_default_limit_and_environment_overrides(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_WEB_THREAD_MESSAGE_LIMIT", None)
            os.environ.pop("CODEX_WEB_THREAD_TURN_LIMIT", None)
            self.assertEqual(self.service.default_message_limit(), 100)

        with patch.dict(os.environ, {"CODEX_WEB_THREAD_MESSAGE_LIMIT": "250"}, clear=False):
            self.assertEqual(self.service.default_message_limit(), 250)
        with patch.dict(os.environ, {"CODEX_WEB_THREAD_MESSAGE_LIMIT": "5000"}, clear=False):
            self.assertEqual(self.service.default_message_limit(), 1000)
        with patch.dict(os.environ, {"CODEX_WEB_THREAD_MESSAGE_LIMIT": "bad"}, clear=False):
            self.assertEqual(self.service.default_message_limit(), 100)

    def test_coerce_limit_clamps_explicit_values(self) -> None:
        self.assertEqual(self.service.coerce_message_limit(0), 1)
        self.assertEqual(self.service.coerce_message_limit(12), 12)
        self.assertEqual(self.service.coerce_message_limit(5000), 1000)

    def test_trim_messages_keeps_newest_items_across_turns(self) -> None:
        response = {
            "thread": {
                "turns": [
                    {"id": "t1", "items": [{"id": "1"}, {"id": "2"}]},
                    {"id": "t2", "items": [{"id": "3"}, {"id": "4"}, {"id": "5"}]},
                ]
            }
        }
        result = self.service.trim_messages(response, 3)
        thread = result["thread"]
        self.assertEqual([item["id"] for turn in thread["turns"] for item in turn["items"]], ["3", "4", "5"])
        self.assertTrue(thread["messagesTruncated"])
        self.assertEqual(thread["messagesOmitted"], 2)
        self.assertEqual(thread["messageLimit"], 3)

    def test_application_rebinds_legacy_helpers_to_thread_service(self) -> None:
        service = application.thread_service
        self.assertIs(core._default_thread_message_limit.__self__, service)
        self.assertIs(core._coerce_thread_message_limit.__self__, service)
        self.assertIs(core._trim_thread_messages, service.trim_messages)


if __name__ == "__main__":
    unittest.main()
