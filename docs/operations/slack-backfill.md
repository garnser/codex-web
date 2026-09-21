# Slack missed-message backfill

Slack webhook ingestion and historical polling are separate paths. Webhooks remain available when polling backfill is disabled or paused by Project readiness/reconciliation gates.

## Emergency disable

Set:

```bash
CODEX_WEB_SLACK_BACKFILL_INTERVAL_SECONDS=0
```

Zero or a negative value disables periodic polling. This does **not** disable the Slack webhook endpoint.

The tradeoff is explicit: while polling is disabled, Slack messages missed by webhook delivery are not recovered until polling is re-enabled.

Interval behavior:

- unset: `15` seconds;
- invalid value: `15` seconds;
- zero/negative: disabled;
- positive values below five seconds: clamped to `5` seconds.

## Incremental and bounded reconciliation

Backfill no longer scans the complete bot event journal or complete reply/delivery-target registries on every cycle.

Recent webhook/delivery dedupe reads use the telemetry service's bounded reverse-tail reader. Production thread-target discovery starts from indexed Project Slack bindings and uses exact keyed reply/delivery target lookups for those bindings.

Each cycle is bounded by:

```text
CODEX_WEB_SLACK_BACKFILL_MAX_CALLS_PER_CYCLE=20
CODEX_WEB_SLACK_BACKFILL_MAX_CYCLE_SECONDS=5
CODEX_WEB_SLACK_BACKFILL_BATCH_LIMIT=50
```

Limits are clamped to safe ranges. Work targets are sorted and rotated by a durable cursor so a small per-cycle budget does not permanently starve later channels/threads.

Blocking journal lookup, binding/target discovery, checkpoint persistence, reconciliation-gate persistence, and backfill telemetry writes run off the main asyncio event loop.

## Durable checkpoints and pagination

State is stored in the canonical StateStore under the `slack_backfill` namespace.

Each channel/thread target records:

- completed timestamp watermark;
- Slack provider pagination cursor, when a result spans multiple pages;
- the pinned `oldest` boundary for an in-progress paginated scan;
- the pending high-watermark that becomes authoritative only when pagination completes;
- cumulative processed/skipped/error counts.

This distinction prevents a high-volume channel from advancing its timestamp watermark after only the newest API page and silently skipping older pages.

After restart, an in-progress provider cursor resumes with the same scan boundary. Completed targets resume from their durable timestamp watermark.

## Coalescing and cancellation

Only one backfill cycle runs in a process at a time. Duplicate triggers while a cycle is active are coalesced and counted rather than launching overlapping scans.

A cancelled cycle checkpoints only completed provider pages. It records the cycle completion attempt but does not advance `last_successful_completion_at`. Re-running resumes from the last committed page/watermark.

Project readiness/startup gating is still authoritative: an incomplete imported Project may keep historical polling paused while normal webhook ingestion remains active.

## Rate limits

Slack `429` / `ratelimited` responses set exponential/provider-aware cooldown state, honoring `Retry-After` when supplied.

Controls:

```text
CODEX_WEB_SLACK_BACKFILL_RATE_LIMIT_MIN_SECONDS=60
CODEX_WEB_SLACK_BACKFILL_RATE_LIMIT_MAX_SECONDS=900
```

Cooldown timestamp and failure count are persisted. Restarting the process therefore does not immediately discard a provider backoff and hammer Slack again.

## Diagnostics

Slack provider health exposes:

- configured interval and whether the polling loop is running;
- whether a reconciliation cycle is currently active;
- provider-call, time, and batch budgets;
- cooldown remaining/until and rate-limit failure count;
- readiness/reconciliation gate state;
- last cycle start/completion/successful completion;
- last duration and processed/skipped/error counts;
- current and last completed work cursor;
- target count/queue depth;
- per-target durable checkpoint/watermark/provider cursor state;
- number of duplicate/coalesced triggers.

These are operational diagnostics only; they do not contain Slack tokens or other Secret values.
