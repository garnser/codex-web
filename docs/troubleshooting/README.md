# Troubleshooting

Start with observable state at the correct scope:

1. `/api/livez` — web-process liveness.
2. `/api/readyz` — application/runtime and canonical storage readiness.
3. `/api/projects/{project_id}/readiness` — semantic/execution readiness for one Project.
4. `/api/healthz` — deeper component/daemon diagnostics, not a Project execution gate.
5. Operations/observability UI and structured logs.
6. Canonical Attention/Incident state for human intervention.
7. ActionIntent history for external side effects with uncertain/failed outcomes.

Do not blindly retry an external action whose outcome is unknown. Reconcile provider state first, especially for non-idempotent actions.

Common starting points:

- Project exists but cannot execute -> [Project readiness troubleshooting](project-readiness.md);
- startup/container problems -> [Installation](../getting-started/installation.md) and [DOCKER.md](../../DOCKER.md);
- provider throttling -> [Provider capacity](../architecture/provider-capacity.md);
- saturated execution -> [Capacity resilience](../architecture/capacity-resilience.md);
- incident/recovery -> [Incident domain](../architecture/incidents.md);
- failed restore -> [Recovery continuity](../architecture/recovery-continuity.md);
- incompatible upgrade -> [Safe upgrades](../architecture/safe-upgrades.md).

- [Operator troubleshooting matrix](operator-matrix.md) — Definitions, providers, workers/extensions, secrets/keys, leases, schedules, approvals, capacity, releases, upgrades, restores, split brain and reconciliation.
