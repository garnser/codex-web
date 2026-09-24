# Operations

Operational state should be read from canonical APIs and the operator UI, not reconstructed from model conversation.

- Health and observability: [Observability architecture](../architecture/observability.md)
- Incidents: [Incident domain](../architecture/incidents.md)
- Backup/restore and RPO/RTO: [Recovery continuity](../architecture/recovery-continuity.md)
- Capacity/backpressure: [Capacity resilience](../architecture/capacity-resilience.md)
- CLI-backed AI runtimes: [Provider CLI execution](cli-backed-runtimes.md)
- Frontend responsiveness: [Frontend performance budgets](frontend-performance.md)
- Releases: [Release promotion](../architecture/releases.md)
- Upgrades and rollback compatibility: [Upgrade and rollback procedure](upgrade-and-rollback.md) and [Safe upgrades](../architecture/safe-upgrades.md)
- Multi-repository Project setup, execution, recovery and migration: [Multi-repository Projects](multi-repository-projects.md)
- Multi-repository security/E2E evidence: [Security qualification](multi-repository-security-qualification.md)
- Legacy multi-repository Project/thread conversion: [Legacy Project migration](legacy-project-migration.md)
- Controlled autonomy/audit: [Bounded autonomy](../architecture/bounded-autonomy.md) and [Autonomy audit](../architecture/autonomy-audit.md)
- First-class Automations: [Automation qualification](automation-qualification.md)

For local/container deployment details see [DOCKER.md](../../DOCKER.md).

- Removing a deployment: [Uninstall or remove a deployment](uninstall.md)

- [Operator runbooks](runbooks.md) — startup/shutdown, release, incidents, recovery, workers/extensions, keys, capacity and replicated failover.
- [Documentation and release readiness](release-readiness.md) — verified procedures, screenshot refresh, deprecation and tracked documentation gaps.
- [Company Operations diagnostics](company-operations.md) — canonical business state, source/extension health, fact/KPI conflicts, consequence explain chains and deterministic recovery guidance.

- [Canonical legacy-state materialization](canonical-materialization.md) — dry-run, apply/resume, tenant/resource/TaskSource/Secret recovery, and rollback boundaries for imported installations.

- [Project bootstrap manifest and CLI](project-bootstrap.md) — versioned desired-state input, safe scaffolding/dry-run, legacy materialization apply, exit codes, and secret/path validation.
- Project readiness diagnosis: [Project readiness troubleshooting](../troubleshooting/project-readiness.md) and [readiness/bootstrap reference](../reference/readiness-bootstrap.md).

- [Background reconciliation gates](reconciliation-gates.md) — readiness-aware service startup, durable approval/pause controls, maintenance exclusion, and Slack polling disable semantics.

- [Operational-state inspection and compaction](operational-state-compaction.md) — bounded pathology inspection, guarded delivery-target compaction, backup, verification, and rollback.

- [Slack missed-message backfill](slack-backfill.md) — polling disable control, bounded incremental reconciliation, durable pagination checkpoints, rate limits, and diagnostics.

- [Slack Socket Mode lifecycle](slack-socket-mode.md) — keepalive settings, transport failure classes, bounded reconnects, dedupe continuity, and operator diagnostics.

- [Stale active-turn recovery](stale-active-turn-recovery.md) — evidence-based active-marker reconciliation, crash-safe recovery, private legacy backups, and operator resolution.

- [Runtime event-journal lifecycle](event-journal-lifecycle.md) — active-file rotation, cross-segment tail reads, crash recovery, protected retention, archive/compression, and lifecycle diagnostics.

- [Recovery release v0.2.0](recovery-release-v0.2.0.md) — immutable candidate build, nginx qualification, real-provider promotion evidence, v0.1 emergency-mount upgrade, and rollback.

- [Failure taxonomy and retry contract](failure-taxonomy.md) — stable provider/runtime/policy/action reason codes, deterministic retryability, reconciliation safety, and metrics labels.
