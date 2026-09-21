# Slack Socket Mode lifecycle

Slack Socket Mode is a long-lived transport. A dropped WebSocket is treated as a transport lifecycle event, not as proof that otherwise valid Slack credentials must be rotated.

## Keepalive configuration

Defaults:

```text
CODEX_WEB_SLACK_SOCKET_PING_INTERVAL_SECONDS=20
CODEX_WEB_SLACK_SOCKET_PING_TIMEOUT_SECONDS=10
CODEX_WEB_SLACK_SOCKET_OPEN_TIMEOUT_SECONDS=10
```

The ping interval is clamped to 5-120 seconds. Ping timeout and opening-handshake timeout are finite and clamped to 2-60 seconds.

This replaces the previous 60-second ping interval with no ping timeout, which could leave an unhealthy transport undetected for too long.

## Failure classes

Socket lifecycle diagnostics distinguish:

- `handshake_timeout`: opening WebSocket handshake timed out;
- `keepalive_timeout`: an established connection failed ping/pong liveness;
- `clean_close`: Slack closed the established WebSocket normally;
- `transport_reset`: reset, EOF, or missing close frame;
- `transport_error`: other WebSocket/transport failures;
- `authentication_configuration`: Slack authentication/configuration failure.

Transient transport failures do not set a token-rotation requirement.

## Reconnect behavior

Each reconnect performs the supported Slack flow again, including a fresh `apps.connections.open` request before opening a new WebSocket.

Reconnect delay uses bounded exponential backoff with jitter:

```text
CODEX_WEB_SLACK_RECONNECT_MIN_SECONDS=2
CODEX_WEB_SLACK_RECONNECT_MAX_SECONDS=60
CODEX_WEB_SLACK_RECONNECT_JITTER_RATIO=0.2
CODEX_WEB_SLACK_RECONNECT_STABLE_RESET_SECONDS=60
```

A connection that remained healthy for the configured stable-reset duration resets the consecutive-failure backoff sequence.

Only one receive loop exists per configured connection. The runtime owns one connection task per connection and reconnects sequentially inside that task.

## Delivery and dedupe across reconnects

Slack payload worker queues and the bounded replay-dedupe set are kept alive across transient Socket Mode reconnects. They are only torn down when the configured runtime connection itself is stopped.

This means replayed Slack envelopes/events after reconnect remain idempotent while bounded queue/backpressure behavior remains unchanged.

## Diagnostics

Runtime status exposes transport information including:

- `connectedSince`;
- `lastFrameAt` / `lastEnvelopeAt`;
- `lastEventAt`;
- `lastPingAt` and `lastPongAt` when ping instrumentation is supported by the WebSocket implementation;
- ping interval, ping timeout, and opening timeout;
- reconnect count and consecutive failure count;
- last disconnect/error class;
- reconnect delay and next retry timestamp;
- `tokenRotationRequired=false` for transport reconnects.

Reconnect telemetry records error classes rather than depending on raw exception text for operator decisions.

## Shutdown

Task cancellation stops the receive loop immediately and does not enter the reconnect path. Payload workers are drained/cancelled according to the existing bounded shutdown policy.
