from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from codex_web.services.turn_queue_policy import TurnQueuePolicy, install_turn_queue_policy


class TurnQueuePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queues = {"thread-1": [object(), object()]}
        self.host = SimpleNamespace(
            _load_turn_queues=lambda: self.queues,
        )
        self.policy = TurnQueuePolicy(self.host._load_turn_queues)

    def test_queue_and_depth_handle_missing_thread_ids(self) -> None:
        self.assertEqual(self.policy.queue(None), [])
        self.assertEqual(self.policy.depth(None), 0)
        self.assertEqual(len(self.policy.queue("thread-1")), 2)
        self.assertEqual(self.policy.depth("thread-1"), 2)
        self.assertEqual(self.policy.queue("missing"), [])

    def test_defaults_invalid_values_and_clamps_match_legacy_policy(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_MAX_THREAD_QUEUE_DEPTH": "",
                "CODEX_WEB_STEER_WINDOW_SECONDS": "",
                "CODEX_WEB_MAX_STEERS_PER_WINDOW": "",
            },
            clear=False,
        ):
            self.assertEqual(self.policy.max_depth(), 12)
            self.assertEqual(self.policy.steer_window_seconds(), 60.0)
            self.assertEqual(self.policy.max_steers_per_window(), 4)

        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_MAX_THREAD_QUEUE_DEPTH": "bad",
                "CODEX_WEB_STEER_WINDOW_SECONDS": "bad",
                "CODEX_WEB_MAX_STEERS_PER_WINDOW": "bad",
            },
            clear=False,
        ):
            self.assertEqual(self.policy.max_depth(), 12)
            self.assertEqual(self.policy.steer_window_seconds(), 60.0)
            self.assertEqual(self.policy.max_steers_per_window(), 4)

        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_MAX_THREAD_QUEUE_DEPTH": "0",
                "CODEX_WEB_STEER_WINDOW_SECONDS": "0",
                "CODEX_WEB_MAX_STEERS_PER_WINDOW": "999",
            },
            clear=False,
        ):
            self.assertEqual(self.policy.max_depth(), 1)
            self.assertEqual(self.policy.steer_window_seconds(), 1.0)
            self.assertEqual(self.policy.max_steers_per_window(), 50)

        with patch.dict(os.environ, {"CODEX_WEB_MAX_THREAD_QUEUE_DEPTH": "999"}, clear=False):
            self.assertEqual(self.policy.max_depth(), 100)

    def test_record_steer_expires_old_entries_and_rate_limits_current_window(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_STEER_WINDOW_SECONDS": "60",
                "CODEX_WEB_MAX_STEERS_PER_WINDOW": "2",
            },
            clear=False,
        ):
            self.policy.record_steer("thread-1", now=100.0)
            self.policy.record_steer("thread-1", now=110.0)
            with self.assertRaises(HTTPException) as raised:
                self.policy.record_steer("thread-1", now=120.0)
            self.assertEqual(raised.exception.status_code, 429)
            self.assertEqual(raised.exception.detail["code"], "thread_steer_rate_limited")
            self.assertEqual(raised.exception.detail["threadId"], "thread-1")
            self.assertEqual(raised.exception.detail["retryAfterSeconds"], 40)

            self.policy.record_steer("thread-1", now=161.0)
            self.assertEqual(list(self.policy.steer_times["thread-1"]), [110.0, 161.0])

    def test_installer_rebinds_historical_queue_policy_names(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        host = SimpleNamespace(_load_turn_queues=lambda: {})
        policy = install_turn_queue_policy(app, host)

        self.assertIs(app.state.turn_queue_policy, policy)
        self.assertIs(host._thread_queue.__self__, policy)
        self.assertIs(host._thread_queue_depth.__self__, policy)
        self.assertIs(host._record_thread_steer.__self__, policy)
        self.assertIs(host._max_thread_queue_depth, policy.max_depth)
        self.assertIs(host._steer_window_seconds, policy.steer_window_seconds)
        self.assertIs(host._max_steers_per_window, policy.max_steers_per_window)


if __name__ == "__main__":
    unittest.main()
