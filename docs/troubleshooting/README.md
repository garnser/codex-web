# Troubleshooting

Start with observable state:

1. `/api/livez` — web-process liveness.
2. `/api/healthz` — deeper Codex daemon readiness.
3. Operations/observability UI and structured logs.
4. Canonical Attention/Incident state for human intervention.
5. ActionIntent history for external side effects with uncertain/failed outcomes.

Do not blindly retry an external action whose outcome is unknown. Reconcile provider state first, especially for non-idempotent actions.

Common starting points:

- startup/container problems → [Installation](../getting-started/installation.md) and [DOCKER.md](../../DOCKER.md);
- provider throttling → [Provider capacity](../architecture/provider-capacity.md);
- saturated execution → [Capacity resilience](../architecture/capacity-resilience.md);
- incident/recovery → [Incident domain](../architecture/incidents.md);
- failed restore → [Recovery continuity](../architecture/recovery-continuity.md);
- incompatible upgrade → [Safe upgrades](../architecture/safe-upgrades.md).

- [Operator troubleshooting matrix](operator-matrix.md) — Definitions, providers, workers/extensions, secrets/keys, leases, schedules, approvals, capacity, releases, upgrades, restores, split brain and reconciliation.
