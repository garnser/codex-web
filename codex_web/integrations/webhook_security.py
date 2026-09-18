from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Iterable
from typing import Any

from codex_web.services.secrets import SecretBroker

from fastapi import HTTPException, Request


def _configured_secrets(values: Iterable[str | None]) -> list[str]:
    return [value for value in values if value]


def verify_slack_signature(
    request: Request,
    body: bytes,
    signing_secrets: Iterable[str | None],
    *,
    now: float | None = None,
) -> None:
    secrets = _configured_secrets(signing_secrets)
    if not secrets:
        raise HTTPException(status_code=503, detail="Slack webhook verification is not configured")

    timestamp = request.headers.get("x-slack-request-timestamp")
    signature = request.headers.get("x-slack-signature")
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Missing Slack signature")
    try:
        request_time = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid Slack timestamp") from exc
    if abs((time.time() if now is None else now) - request_time) > 300:
        raise HTTPException(status_code=401, detail="Stale Slack signature")
    try:
        decoded_body = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=401, detail="Invalid Slack request body") from exc

    base = f"v0:{timestamp}:{decoded_body}".encode()
    for signing_secret in secrets:
        expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, signature):
            return
    raise HTTPException(status_code=401, detail="Invalid Slack signature")


def verify_telegram_secret(request: Request, expected_secrets: Iterable[str | None]) -> None:
    secrets = _configured_secrets(expected_secrets)
    if not secrets:
        raise HTTPException(status_code=503, detail="Telegram webhook verification is not configured")
    received = request.headers.get("x-telegram-bot-api-secret-token")
    if not received or not any(hmac.compare_digest(expected, received) for expected in secrets):
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")


def verify_gitlab_token(request: Request, expected_secret: str | None) -> None:
    if not expected_secret:
        raise HTTPException(status_code=503, detail="GitLab webhook verification is not configured")
    received = request.headers.get("x-gitlab-token") or ""
    if not hmac.compare_digest(received, expected_secret):
        raise HTTPException(status_code=401, detail="Invalid GitLab webhook token")


def install_webhook_security(
    host: Any,
    secret_broker: SecretBroker | None = None,
) -> None:
    """Install fail-closed verification adapters.

    Secret-reference credentials are consumed inside broker callbacks. Legacy
    connection fields and deployment environment secrets remain supported only
    as compatibility inputs during migration.
    """

    def slack(request: Request, body: bytes) -> None:
        deployment_secret = os.environ.get("SLACK_SIGNING_SECRET")
        legacy: list[str | None] = [deployment_secret]
        for connection in host._load_bot_connections():
            if connection.provider != "slack":
                continue
            secret_id = getattr(connection, "signing_secret_secret_id", None)
            if secret_id and secret_broker is not None:
                actor = host._bot_runtime_actor(connection.project_id)

                def verify(value: str) -> bool:
                    verify_slack_signature(request, body, [value])
                    return True

                try:
                    secret_broker.use(
                        secret_id,
                        actor=actor,
                        operation="slack.webhook.verify",
                        consumer=verify,
                        context={"connection_id": connection.id},
                    )
                    return
                except HTTPException as exc:
                    if exc.status_code != 401:
                        raise
                continue
            legacy.append(connection.signing_secret)
        verify_slack_signature(request, body, legacy)

    def telegram(request: Request) -> None:
        legacy: list[str | None] = [os.environ.get("TELEGRAM_WEBHOOK_SECRET")]
        for connection in host._load_bot_connections():
            if connection.provider != "telegram":
                continue
            secret_id = getattr(connection, "webhook_secret_secret_id", None)
            if secret_id and secret_broker is not None:
                actor = host._bot_runtime_actor(connection.project_id)

                def verify(value: str) -> bool:
                    verify_telegram_secret(request, [value])
                    return True

                try:
                    secret_broker.use(
                        secret_id,
                        actor=actor,
                        operation="telegram.webhook.verify",
                        consumer=verify,
                        context={"connection_id": connection.id},
                    )
                    return
                except HTTPException as exc:
                    if exc.status_code != 401:
                        raise
                continue
            legacy.append(connection.webhook_secret)
        verify_telegram_secret(request, legacy)

    def gitlab(request: Request) -> None:
        verify_gitlab_token(
            request,
            os.environ.get("CODEX_WEB_GITLAB_WEBHOOK_SECRET")
            or os.environ.get("GITLAB_WEBHOOK_SECRET"),
        )

    host._verify_slack_signature = slack
    host._verify_telegram_secret = telegram
    host._verify_gitlab_webhook = gitlab
