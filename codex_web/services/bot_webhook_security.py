from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import HTTPException, Request

from codex_web.services.bot_connections import BotConnectionService
from codex_web.services.secrets import SecretBroker


class BotWebhookSecurityService:
    """Verify provider webhooks without exposing stored credential material."""

    def __init__(
        self,
        connections: BotConnectionService,
        *,
        secret_broker: SecretBroker | None = None,
    ) -> None:
        self.connections = connections
        self.secret_broker = secret_broker

    async def _secret_matches(
        self,
        connection,
        field: str,
        operation: str,
        matcher,
    ) -> bool:
        secret_id = getattr(connection, f"{field}_secret_id", None)
        if secret_id and self.secret_broker is not None:
            async def consume(secret: str) -> bool:
                return bool(matcher(secret))

            return bool(
                await self.secret_broker.use_async(
                    secret_id,
                    actor=self.connections.runtime_actor(
                        connection.project_id
                    ),
                    operation=operation,
                    consumer=consume,
                    context={
                        "connection_id": connection.id,
                        "provider": connection.provider,
                    },
                )
            )
        raw = getattr(connection, field, None)
        return bool(raw and matcher(raw))

    async def verify_slack(
        self,
        request: Request,
        body: bytes,
    ) -> None:
        timestamp = request.headers.get("x-slack-request-timestamp")
        signature = request.headers.get("x-slack-signature")
        environment_secret = os.environ.get("SLACK_SIGNING_SECRET")
        candidates = [
            connection
            for connection in self.connections.load_connections()
            if connection.provider == "slack"
            and (
                connection.signing_secret
                or connection.signing_secret_secret_id
            )
        ]
        if not environment_secret and not candidates:
            raise HTTPException(
                status_code=503,
                detail="Slack webhook verification is not configured",
            )
        if not timestamp or not signature:
            raise HTTPException(
                status_code=401,
                detail="Missing Slack signature",
            )
        try:
            request_time = int(timestamp)
        except ValueError as exc:
            raise HTTPException(
                status_code=401,
                detail="Invalid Slack timestamp",
            ) from exc
        if abs(time.time() - request_time) > 300:
            raise HTTPException(
                status_code=401,
                detail="Stale Slack signature",
            )
        basestring = f"v0:{timestamp}:{body.decode()}".encode()

        def matches(secret: str) -> bool:
            expected = "v0=" + hmac.new(
                secret.encode(),
                basestring,
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(expected, signature)

        if environment_secret and matches(environment_secret):
            return
        for connection in candidates:
            if await self._secret_matches(
                connection,
                "signing_secret",
                "slack.verify_webhook",
                matches,
            ):
                return
        raise HTTPException(
            status_code=401,
            detail="Invalid Slack signature",
        )

    async def verify_telegram(self, request: Request) -> None:
        environment_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
        candidates = [
            connection
            for connection in self.connections.load_connections()
            if connection.provider == "telegram"
            and (
                connection.webhook_secret
                or connection.webhook_secret_secret_id
            )
        ]
        if not environment_secret and not candidates:
            raise HTTPException(
                status_code=503,
                detail="Telegram webhook verification is not configured",
            )
        received = request.headers.get(
            "x-telegram-bot-api-secret-token"
        )
        if not received:
            raise HTTPException(
                status_code=401,
                detail="Invalid Telegram webhook secret",
            )

        def matches(secret: str) -> bool:
            return hmac.compare_digest(secret, received)

        if environment_secret and matches(environment_secret):
            return
        for connection in candidates:
            if await self._secret_matches(
                connection,
                "webhook_secret",
                "telegram.verify_webhook",
                matches,
            ):
                return
        raise HTTPException(
            status_code=401,
            detail="Invalid Telegram webhook secret",
        )


def install_bot_webhook_security_service(
    app,
    host,
    *,
    connections=None,
    secret_broker=None,
) -> BotWebhookSecurityService:
    service = BotWebhookSecurityService(
        connections or app.state.bot_connection_service,
        secret_broker=(
            secret_broker
            or getattr(app.state, "secret_broker", None)
        ),
    )
    app.state.bot_webhook_security_service = service
    # These compatibility functions are async; legacy endpoints are replaced
    # by extracted routers in application composition.
    host._verify_slack_signature_async = service.verify_slack
    host._verify_telegram_secret_async = service.verify_telegram
    return service
