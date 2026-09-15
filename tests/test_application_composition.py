from __future__ import annotations

import hashlib
import hmac
import time
import unittest

from fastapi import HTTPException, Request

from codex_web import application
from codex_web.integrations.webhook_security import (
    verify_gitlab_token,
    verify_slack_signature,
    verify_telegram_secret,
)
from codex_web.runtime import core


def _request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw_headers})


class ApplicationCompositionTests(unittest.TestCase):
    def test_application_uses_single_core_fastapi_instance(self) -> None:
        self.assertIs(application.app, core.app)

    def test_composed_api_contains_liveness_and_executive_routes(self) -> None:
        paths = application.app.openapi().get("paths", {})
        self.assertIn("/api/livez", paths)
        self.assertIn("/api/executive/agents", paths)


class WebhookSecurityTests(unittest.TestCase):
    def test_unconfigured_webhooks_fail_closed(self) -> None:
        checks = (
            lambda: verify_slack_signature(_request(), b"{}", []),
            lambda: verify_telegram_secret(_request(), []),
            lambda: verify_gitlab_token(_request(), None),
        )
        for check in checks:
            with self.subTest(check=check), self.assertRaises(HTTPException) as raised:
                check()
            self.assertEqual(raised.exception.status_code, 503)

    def test_valid_slack_signature_is_accepted(self) -> None:
        secret = "test-secret"
        now = time.time()
        timestamp = str(int(now))
        body = b'{"type":"event_callback"}'
        base = f"v0:{timestamp}:{body.decode()}".encode()
        signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
        request = _request(
            {
                "x-slack-request-timestamp": timestamp,
                "x-slack-signature": signature,
            }
        )
        verify_slack_signature(request, body, [secret], now=now)

    def test_invalid_configured_secrets_return_unauthorized(self) -> None:
        checks = (
            lambda: verify_telegram_secret(
                _request({"x-telegram-bot-api-secret-token": "wrong"}),
                ["expected"],
            ),
            lambda: verify_gitlab_token(
                _request({"x-gitlab-token": "wrong"}),
                "expected",
            ),
        )
        for check in checks:
            with self.subTest(check=check), self.assertRaises(HTTPException) as raised:
                check()
            self.assertEqual(raised.exception.status_code, 401)

    def test_stale_slack_signature_is_rejected(self) -> None:
        now = time.time()
        timestamp = str(int(now - 301))
        request = _request(
            {
                "x-slack-request-timestamp": timestamp,
                "x-slack-signature": "v0=invalid",
            }
        )
        with self.assertRaises(HTTPException) as raised:
            verify_slack_signature(request, b"{}", ["secret"], now=now)
        self.assertEqual(raised.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
