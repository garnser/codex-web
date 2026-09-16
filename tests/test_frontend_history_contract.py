from __future__ import annotations

import unittest
from pathlib import Path


class FrontendHistoryContractTests(unittest.TestCase):
    def test_thread_history_does_not_patch_global_fetch_or_scroll_accessor(self) -> None:
        history = Path("static/thread_history.js").read_text()

        self.assertNotIn("window.fetch =", history)
        self.assertNotIn("Object.defineProperty(messages, \"scrollTop\"", history)
        self.assertIn("window.codexThreadHistory", history)
        self.assertIn("requestBottomScroll", history)

    def test_app_requests_thread_history_explicitly(self) -> None:
        app = Path("static/app.js").read_text()

        self.assertIn('readQs.set("message_limit", String(messageLimit))', app)
        self.assertIn("history?.recordThread?.(threadId, thread)", app)
        self.assertIn("history?.afterThreadRendered?.(threadId, $(\"messages\"))", app)
        self.assertNotIn('$("messages").scrollTop = $("messages").scrollHeight;', app)


if __name__ == "__main__":
    unittest.main()
