# Operations

Operational state should be read from canonical APIs and the operator UI, not reconstructed from model conversation.

- Health and observability: [Observability architecture](../architecture/observability.md)
- Incidents: [Incident domain](../architecture/incidents.md)
- Backup/restore and RPO/RTO: [Recovery continuity](../architecture/recovery-continuity.md)
- Capacity/backpressure: [Capacity resilience](../architecture/capacity-resilience.md)
- Frontend responsiveness: [Frontend performance budgets](frontend-performance.md)
- Releases: [Release promotion](../architecture/releases.md)
- Upgrades and rollback compatibility: [Upgrade and rollback procedure](upgrade-and-rollback.md) and [Safe upgrades](../architecture/safe-upgrades.md)
- Multi-repository Project setup, execution, recovery and migration: [Multi-repository Projects](multi-repository-projects.md)
- Multi-repository security/E2E evidence: [Security qualification](multi-repository-security-qualification.md)
- Legacy multi-repository Project/thread conversion: [Legacy Project migration](legacy-project-migration.md)
- Controlled autonomy/audit: [Bounded autonomy](../architecture/bounded-autonomy.md) and [Autonomy audit](../architecture/autonomy-audit.md)

For local/container deployment details see [DOCKER.md](../../DOCKER.md).

- Removing a deployment: [Uninstall or remove a deployment](uninstall.md)

- [Operator runbooks](runbooks.md) — startup/shutdown, release, incidents, recovery, workers/extensions, keys, capacity and replicated failover.
- [Documentation and release readiness](release-readiness.md) — verified procedures, screenshot refresh, deprecation and tracked documentation gaps.
- [Company Operations diagnostics](company-operations.md) — canonical business state, source/extension health, fact/KPI conflicts, consequence explain chains and deterministic recovery guidance.

- [Canonical legacy-state materialization](canonical-materialization.md) — dry-run, apply/resume, tenant/resource/TaskSource/Secret recovery, and rollback boundaries for imported installations.

- [Project bootstrap manifest and CLI](project-bootstrap.md) — versioned desired-state input, safe scaffolding/dry-run, legacy materialization apply, exit codes, and secret/path validation.

- [Background reconciliation gates](reconciliation-gates.md) — readiness-aware service startup, durable approval/pause controls, maintenance exclusion, and Slack polling disable semantics.
