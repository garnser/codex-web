from __future__ import annotations

import unittest

from codex_web import application
from codex_web.runtime import core


class RuntimeStateWiringTests(unittest.TestCase):
    def test_high_churn_runtime_state_uses_sqlite_repositories(self) -> None:
        self.assertIs(
            getattr(core._load_thread_settings, "__self__", None),
            application.runtime_state.thread_settings,
        )
        self.assertIs(
            getattr(core._load_active_turns, "__self__", None),
            application.runtime_state.active_turns,
        )
        self.assertIs(
            getattr(core._load_work_item_states, "__self__", None),
            application.runtime_state.work_item_states,
        )
        self.assertIs(application.app.state.sqlite_state_store, application.state_store)


if __name__ == "__main__":
    unittest.main()
