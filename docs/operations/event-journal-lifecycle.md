# Runtime event-journal lifecycle

The bot/runtime JSONL journal is operational telemetry. Canonical Project, Work Item, execution, recovery, authorization, and audit state belongs in the canonical StateStore/domain stores; the journal is not used as the authoritative database.

Some journal events can still be marked retention-sensitive. Records are treated as protected when they declare `retention_class: audit|protected|non_reconstructible` or use protected security/audit/approval/action-intent/execution-authority event families. Any segment with protected records is excluded from automatic deletion.

## Active-file bounds

The active `bot_events.jsonl` file rotates by size or age:

```text
CODEX_WEB_EVENT_JOURNAL_MAX_BYTES=67108864
CODEX_WEB_EVENT_JOURNAL_MAX_AGE_SECONDS=86400
CODEX_WEB_EVENT_JOURNAL_MAINTENANCE_SECONDS=30
```

Defaults are 64 MiB, 24 hours, and a 30-second maintenance cadence.

Rotation itself is deliberately small and synchronous under the append mutex:

1. fsync the active file;
2. atomically rename it to a monotonically numbered closed segment;
3. create a new private active file;
4. fsync the directory;
5. atomically persist segment metadata.

Long indexing, compression, and retention work never holds the append mutex.

Each newly appended event receives an internal stable `journal_cursor` of `<segment-sequence>:<byte-offset>`. Sequence numbers remain monotonic across rotation.

## Crash recovery

The manifest is advisory/recoverable rather than a single point of failure.

At startup Codex Web discovers segment files in the hot and archive directories and reconciles them with the manifest. This recovers a crash after rename but before manifest persistence.

A stale `.gz.tmp` is discarded while the uncompressed source remains authoritative. If both an uncompressed segment and a completed gzip archive exist, the gzip is validated; a valid archive wins and the duplicate source is removed, while an invalid archive is discarded.

Missing segment files are removed from reconstructed manifest state.

## Recent-tail reads

`BotRuntimeTelemetry.recent()` keeps its bounded reverse-tail contract from #482.

It reads:

1. the active segment;
2. newest closed uncompressed segments as needed at the rotation boundary.

It does not scan the complete journal. Existing `recent_metrics()` fields remain compatible.

The newest closed segment is always kept hot/uncompressed, so normal recent-tail and Slack dedupe reads remain valid across a rotation boundary.

Slack historical reconciliation introduced in #478 uses independent durable provider cursors in StateStore; journal rotation therefore does not invalidate provider pagination/watermarks.

## Indexing and archive

Closed segments are indexed in the maintenance worker to derive:

- exact event count;
- oldest/newest event timestamp;
- protected-record count;
- segment byte size.

Indexing can scan a large legacy segment, but runs in a worker thread without holding the append mutex.

Older non-hot segments are optionally gzip-compressed and moved to the private archive directory:

```text
CODEX_WEB_EVENT_JOURNAL_HOT_SEGMENTS=1
CODEX_WEB_EVENT_JOURNAL_COMPRESS=true
```

Archive directories use mode `0700`; active, closed, manifest, and compressed files are restricted to `0600` where Codex Web creates/manages them.

Compression uses a private temporary file, fsyncs it, atomically publishes the gzip, fsyncs the archive directory, then removes the source.

## Retention

Automatic cleanup is bounded by age, segment count, and retained bytes:

```text
CODEX_WEB_EVENT_JOURNAL_RETENTION_SECONDS=2592000
CODEX_WEB_EVENT_JOURNAL_MAX_SEGMENTS=48
CODEX_WEB_EVENT_JOURNAL_RETENTION_MAX_BYTES=4294967296
```

Defaults are 30 days, 48 closed segments, and 4 GiB retained bytes.

Safety rules:

- newest hot segments are never deleted;
- unindexed/unknown-protection segments are never deleted;
- segments containing protected records are never deleted automatically;
- cleanup operates oldest-first only on indexed, non-protected segments;
- no event-level filtering/rewrite occurs during cleanup.

Protected/audit retention therefore requires an explicit future policy/action rather than accidental expiry through the operational journal policy.

## Diagnostics

`GET /api/operational-state/inspection` includes bounded journal lifecycle metadata without scanning event contents:

- active bytes;
- closed segment count;
- archived segment count;
- archive backlog;
- total retained bytes;
- oldest/newest indexed retained timestamps;
- protected/unknown-protection segment counts;
- rotation/archive/cleanup failure counts.

The journal status also exposes maintenance-run counters, removal counts/bytes, limits, and last rotation/maintenance timestamps.

## Runtime isolation

The runtime supervisor owns one event-journal maintenance loop. Heavy indexing/compression runs through `asyncio.to_thread`.

Append and rotation have a separate short critical section; slow archive storage does not block concurrent appends or lightweight HTTP/event-loop work.
