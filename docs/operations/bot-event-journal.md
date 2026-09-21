# Bot/runtime event journal lifecycle

The local bot/runtime event journal is operational telemetry. Canonical domain events,
security state, durable Slack provider checkpoints, Work Item state and other authoritative
records live in their canonical StateStore repositories; rotating `bot_events.jsonl` does
not rotate those sources of truth.

Records in the bot journal are reconstructible operational telemetry by default. A journal
event is treated as protected when it explicitly carries one of these markers:

- `retention_class` is `audit`, `canonical`, `non_reconstructible` or `protected`;
- `audit_required=true`;
- `non_reconstructible=true`;
- its event type starts with `audit.`, `audit_`, `security.` or `security_`.

Protected closed segments are not automatically deleted unless an explicit protected
retention policy is configured.

## Rotation

The active file remains `bot_events.jsonl`. Closed segments are stored beneath
`bot_events.segments/` with a private manifest at
`bot_events.jsonl.manifest.json`.

Defaults:

```text
CODEX_WEB_BOT_EVENT_ROTATE_BYTES=67108864
CODEX_WEB_BOT_EVENT_ROTATE_SECONDS=86400
CODEX_WEB_BOT_EVENT_MAINTENANCE_SECONDS=30
```

Append is serialized with rotation inside the telemetry owner. Crossing a size or age
threshold only marks rotation as required; the supervisor-owned journal maintenance loop
performs rename/index/archive/cleanup through a worker thread, keeping compression and
large-file work off the asyncio event loop.

Rotation commits in this order:

1. fsync the active journal;
2. atomically rename it into the segment directory;
3. create and fsync a fresh active append target;
4. persist and fsync the segment manifest;
5. stream-index the closed segment for timestamps, record count, protection count and
   checksum.

A restart discovers orphan segment files and reconciles them with the manifest. This
covers interruption after rename, after manifest commit, and during archive completion.

## Recent-tail semantics

`BotRuntimeTelemetry.recent()` retains its bounded 1-300 event contract and walks the
active file followed by closed segments newest-to-oldest. The returned events remain in
chronological order.

Consequently a recent-tail read remains correct when the requested window spans a
rotation boundary.

When compression is enabled, a closed gzip segment also gets a private tail sidecar
containing its final 300 valid events. Normal recent reads use that sidecar rather than
inflating the complete archive.

Slack missed-message reconciliation continues to use its StateStore checkpoint
(`watermark`, provider pagination cursor, pinned oldest boundary and pending
watermark). Those values are independent of journal file offsets, so rotation does not
invalidate an in-progress Slack provider cursor. The bounded journal tail used for
message-id dedupe is segment-aware.

## Retention

Defaults:

```text
CODEX_WEB_BOT_EVENT_RETENTION_SEGMENTS=20
CODEX_WEB_BOT_EVENT_MIN_SEGMENTS=2
CODEX_WEB_BOT_EVENT_RETENTION_DAYS=14
CODEX_WEB_BOT_EVENT_RETENTION_BYTES=1073741824
CODEX_WEB_BOT_EVENT_MIN_RETENTION_SECONDS=3600
CODEX_WEB_BOT_EVENT_PROTECTED_RETENTION_DAYS=0
```

A closed operational segment becomes a cleanup candidate when any configured age,
segment-count or retained-byte ceiling is exceeded. It is removed only after the minimum
retention time and minimum segment count are satisfied.

`CODEX_WEB_BOT_EVENT_PROTECTED_RETENTION_DAYS=0` means protected segments are never
automatically removed. Setting a positive value enables protected cleanup, conservatively
clamped to at least 30 days. Use that only when the applicable audit/retention policy
authorizes removal.

Cleanup deletes segment/tail files before removing their manifest entry. An interruption
therefore leaves, at worst, a manifest record whose missing file is reconciled on restart;
it cannot make an already-deleted segment appear readable.

The manifest retains aggregate cleanup counters plus a bounded final 20 cleanup records,
including segment ID, removed bytes, protected-record count and cleanup reason.

## Optional compression

Compression is disabled by default:

```text
CODEX_WEB_BOT_EVENT_COMPRESS=0
CODEX_WEB_BOT_EVENT_HOT_SEGMENTS=1
CODEX_WEB_BOT_EVENT_ARCHIVE_MAX_SEGMENTS_PER_CYCLE=1
```

When enabled, maintenance keeps the configured newest closed segments uncompressed and
compresses older segments incrementally. Archive files and tail sidecars are fsynced before
the manifest switches to the compressed representation; the original source is deleted
only after that commit.

## Diagnostics

Cached runtime health and privileged diagnostics expose `botEventJournal`, including:

- active size;
- segment count;
- total retained bytes;
- oldest/newest retained timestamps;
- protected segment/record counts;
- rotation and archive counts/failures;
- cleanup totals and the bounded latest cleanup records;
- archive backlog;
- last maintenance/error state;
- configured rotation/retention/compression policy.

Operational-state inspection also reports active journal bytes plus manifest-derived
segment count and total retained bytes without scanning journal payloads.

## Failure handling

Journal append never performs compression or retention cleanup. Maintenance is
cancellable and runs via `asyncio.to_thread`.

If maintenance fails, the manifest records the error class/timestamp and the next cycle
reconciles filesystem state before proceeding. A failed archive keeps the original source
until compressed metadata has become authoritative. A failed rotate always restores an
active append target before returning.
