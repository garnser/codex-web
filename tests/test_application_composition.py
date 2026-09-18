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


def _path_tags(path: str) -> set[str]:
    operations = application.app.openapi().get("paths", {}).get(path, {})
    return {
        tag
        for operation in operations.values()
        if isinstance(operation, dict)
        for tag in operation.get("tags", [])
    }


class ApplicationCompositionTests(unittest.TestCase):
    def test_application_uses_single_core_fastapi_instance(self) -> None:
        self.assertIs(application.app, core.app)

    def test_composed_api_contains_liveness_executive_and_context_routes(self) -> None:
        paths = application.app.openapi().get("paths", {})
        self.assertIn("/api/livez", paths)
        self.assertIn("/api/executive/agents", paths)
        self.assertIn("/api/threads/{thread_id}/context", paths)
        self.assertIn("/api/threads/{thread_id}/compact", paths)

    def test_extracted_routes_are_owned_by_domain_routers(self) -> None:
        expected = {
            "/api/projects": "projects",
            "/api/resources": "resources",
            "/api/action-providers": "action-providers",
            "/api/execution-workspaces": "execution-workspaces",
            "/api/artifacts": "artifact-evidence",
            "/api/threads": "threads",
            "/api/threads/{thread_id}/compact": "context",
            "/api/status": "runtime",
            "/api/approvals": "approvals",
            "/api/bots/channels": "bots",
            "/bots/slack/events": "bots",
            "/api/work-items": "work-items",
            "/": "ui",
            "/api/auth-verifier": "system",
            "/api/integrations/gitlab": "integrations",
        }
        for path, tag in expected.items():
            with self.subTest(path=path):
                self.assertIn(tag, _path_tags(path))

    def test_legacy_runtime_uses_extracted_project_repository(self) -> None:
        self.assertIs(getattr(core._load_projects, "__self__", None), application.project_repository)
        self.assertIs(getattr(core._save_projects, "__self__", None), application.project_repository)
        self.assertEqual(core._atomic_write_text.__module__, "codex_web.storage.json_files")

    def test_thread_turn_compatibility_aliases_use_extracted_services(self) -> None:
        self.assertIs(getattr(core.read_thread, "__self__", None), application.thread_service)
        self.assertIs(getattr(core.resume_thread, "__self__", None), application.turn_service)
        self.assertIs(getattr(core.start_turn, "__self__", None), application.turn_service)

    def test_context_service_observes_shared_event_hub(self) -> None:
        self.assertIs(application.app.state.context_compaction_service, application.context_service)
        self.assertIn(application.context_service.observe, core.hub._listeners)

    def test_decomposition_replaced_legacy_routes(self) -> None:
        self.assertGreater(sum(application.EXTRACTED_ROUTE_COUNTS.values()), 0)
        self.assertGreater(application.EXTRACTED_ROUTE_COUNTS["projects"], 0)
        self.assertGreater(application.EXTRACTED_ROUTE_COUNTS["threads"], 0)
        self.assertGreater(application.EXTRACTED_ROUTE_COUNTS["runtime"], 0)
        self.assertGreater(application.EXTRACTED_ROUTE_COUNTS["bots"], 0)
        self.assertGreater(application.EXTRACTED_ROUTE_COUNTS["work-items"], 0)


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
