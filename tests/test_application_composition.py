from __future__ import annotations

import hashlib
import hmac
import time
import unittest
from typing import Any

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


def _route_modules(routes: list[Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for route in routes:
        path = getattr(route, "path", None)
        endpoint = getattr(route, "endpoint", None)
        if isinstance(path, str) and endpoint is not None:
            result.setdefault(path, set()).add(getattr(endpoint, "__module__", ""))
        nested = getattr(route, "routes", None)
        if nested:
            nested_result = _route_modules(list(nested))
            for nested_path, modules in nested_result.items():
                result.setdefault(nested_path, set()).update(modules)
    return result


class ApplicationCompositionTests(unittest.TestCase):
    def test_application_uses_single_core_fastapi_instance(self) -> None:
        self.assertIs(application.app, core.app)

    def test_composed_api_contains_liveness_and_executive_routes(self) -> None:
        paths = application.app.openapi().get("paths", {})
        self.assertIn("/api/livez", paths)
        self.assertIn("/api/executive/agents", paths)

    def test_extracted_routes_are_owned_by_domain_modules(self) -> None:
        modules = _route_modules(list(application.app.routes))
        expected = {
            "/api/projects": "codex_web.api.projects",
            "/api/threads": "codex_web.api.threads",
            "/api/status": "codex_web.api.runtime",
            "/api/approvals": "codex_web.api.approvals",
            "/": "codex_web.api.ui",
            "/api/auth-verifier": "codex_web.api.system",
            "/api/integrations/gitlab": "codex_web.api.integrations",
        }
        for path, module in expected.items():
            with self.subTest(path=path):
                self.assertIn(module, modules.get(path, set()))
                self.assertNotIn("codex_web.runtime.core", modules.get(path, set()))

    def test_legacy_runtime_uses_extracted_project_repository(self) -> None:
        self.assertIs(getattr(core._load_projects, "__self__", None), application.project_repository)
        self.assertIs(getattr(core._save_projects, "__self__", None), application.project_repository)
        self.assertEqual(core._atomic_write_text.__module__, "codex_web.storage.json_files")

    def test_decomposition_replaced_legacy_routes(self) -> None:
        self.assertGreater(sum(application.EXTRACTED_ROUTE_COUNTS.values()), 0)


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
