# Background reconciliation gates

Codex Web separates process liveness, live event ingestion, and provider-expensive
historical reconciliation. Background services declare their startup behavior
instead of relying on incidental startup order.

## Gate states

Project-scoped reconcilers expose one of:

- `waiting` — prerequisites are satisfied but explicit approval is required.
- `eligible` — the reconciler may run on its next bounded cycle.
- `running` — an activity lease is active.
- `paused` — operator pause or maintenance exclusion is active.
- `blocked` — Project readiness or another required invariant is not satisfied.

Use:

```text
GET /api/projects/{project_id}/reconciliation-gates
```

The response includes reason codes, the relevant readiness check, approval/pause
state, timestamps, checkpoint/cursor metadata when supplied by the reconciler,
active maintenance leases, and recent operator audit events.

## Slack backfill

Slack webhook ingestion and historical polling are intentionally independent.
Webhook handling remains active even when historical polling is blocked, paused,
awaiting approval, or disabled.

Historical Slack polling:

1. requires Project execution readiness;
2. requires explicit operator approval for the initial/recovery backfill;
3. is paused while an incompatible maintenance lease is active;
4. re-evaluates readiness on every cycle;
5. performs local journal/target scans off the main asyncio event loop.

Approve a ready Project:

```text
POST /api/projects/{project_id}/reconciliation-gates/slack-backfill/approve
{"correlation_id":"change-or-incident-id"}
```

Pause/resume:

```text
POST /api/projects/{project_id}/reconciliation-gates/slack-backfill/pause
{"reason":"operator maintenance"}

POST /api/projects/{project_id}/reconciliation-gates/slack-backfill/resume
```

Approval and pause state are durable across restart.

### Emergency polling disable

Set:

```text
CODEX_WEB_SLACK_BACKFILL_INTERVAL_SECONDS=0
```

Zero or any negative value disables Slack historical polling. This does **not**
disable Slack webhook ingestion. Positive values below five seconds are clamped
to five seconds; invalid/unset values use the 15-second default.

When polling is disabled, missed webhooks are not recovered by periodic Slack
history reconciliation until polling is re-enabled and the Project gate permits
backfill.

## Maintenance exclusion

Maintenance/compaction workflows can acquire a Project maintenance lease through
the reconciliation gate service. A maintenance-incompatible reconciler cannot
start while that lease is active, and maintenance acquisition fails while a
bounded reconciliation activity lease is active. This is the race-prevention
boundary used by destructive legacy-state maintenance.
