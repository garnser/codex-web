from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.services.watchdog_dispatch import (
    WatchdogDispatchPolicy,
    install_watchdog_dispatch_policy,
)


class WatchdogDispatchPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = SimpleNamespace(WATCHDOG_DISPATCH_TIMES={})
        self.policy = WatchdogDispatchPolicy(self.host)

    def test_cooldown_defaults_invalid_values_and_minimum_match_legacy_policy(self) -> None:
        with patch.dict(os.environ, {"CODEX_WEB_WATCHDOG_DISPATCH_COOLDOWN_SECONDS": ""}, clear=False):
            self.assertEqual(self.policy.cooldown_seconds(), 120.0)
        with patch.dict(os.environ, {"CODEX_WEB_WATCHDOG_DISPATCH_COOLDOWN_SECONDS": "bad"}, clear=False):
            self.assertEqual(self.policy.cooldown_seconds(), 120.0)
        with patch.dict(os.environ, {"CODEX_WEB_WATCHDOG_DISPATCH_COOLDOWN_SECONDS": "1"}, clear=False):
            self.assertEqual(self.policy.cooldown_seconds(), 5.0)
        with patch.dict(os.environ, {"CODEX_WEB_WATCHDOG_DISPATCH_COOLDOWN_SECONDS": "37.5"}, clear=False):
            self.assertEqual(self.policy.cooldown_seconds(), 37.5)

    def test_allowed_uses_recorded_timestamp_and_cooldown(self) -> None:
        with patch.dict(os.environ, {"CODEX_WEB_WATCHDOG_DISPATCH_COOLDOWN_SECONDS": "120"}, clear=False):
            self.assertTrue(self.policy.allowed("key", now=100.0))
            self.policy.record("key", now=100.0)
            self.assertFalse(self.policy.allowed("key", now=219.9))
            self.assertTrue(self.policy.allowed("key", now=220.0))

    def test_record_overwrites_existing_dispatch_timestamp(self) -> None:
        self.policy.record("key", now=100.0)
        self.policy.record("key", now=150.0)
        self.assertEqual(self.host.WATCHDOG_DISPATCH_TIMES["key"], 150.0)

    def test_zero_now_preserves_historical_truthiness_semantics(self) -> None:
        with patch("codex_web.services.watchdog_dispatch.time.time", return_value=250.0):
            self.policy.record("key", now=0.0)
            self.assertEqual(self.host.WATCHDOG_DISPATCH_TIMES["key"], 250.0)
            self.host.WATCHDOG_DISPATCH_TIMES["key"] = 100.0
            with patch.dict(os.environ, {"CODEX_WEB_WATCHDOG_DISPATCH_COOLDOWN_SECONDS": "120"}, clear=False):
                self.assertTrue(self.policy.allowed("key", now=0.0))

    def test_installer_rebinds_historical_watchdog_dispatch_names(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        host = SimpleNamespace(WATCHDOG_DISPATCH_TIMES={})

        policy = install_watchdog_dispatch_policy(app, host)

        self.assertIs(app.state.watchdog_dispatch_policy, policy)
        self.assertIs(host._watchdog_dispatch_allowed.__self__, policy)
        self.assertIs(host._record_watchdog_dispatch.__self__, policy)
        self.assertIs(host._watchdog_dispatch_cooldown_seconds, policy.cooldown_seconds)


if __name__ == "__main__":
    unittest.main()
