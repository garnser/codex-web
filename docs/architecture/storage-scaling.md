# Storage scaling path

codex-web currently uses SQLite as the primary durable state store. That is intentional for the single-instance control-plane deployment: it keeps installation and rollback simple while service boundaries are still being extracted.

## Current guarantees

`SQLiteStateStore` provides:

- transactional document updates using `BEGIN IMMEDIATE`
- WAL mode with a five-second busy timeout
- private database/WAL/SHM permissions
- an explicit schema version
- `PRAGMA quick_check` health reporting
- WAL checkpoint support
- SQLite-native transactional backups
- compatibility JSON mirrors in the repositories that still require rollback support

A database created by a newer codex-web schema is rejected rather than silently opened by an older binary.

## Before a replicated deployment

Running multiple application instances requires more than replacing the database. Several runtime concerns are intentionally process-local today, including task ownership, queue-drain scheduling, provider sockets, active recovery work and some transient deduplication.

The migration should therefore be split into two concerns:

1. **PostgreSQL for durable shared state**
   - projects, thread settings and indexes
   - work-item state/events
   - Executive context, history and knowledge
   - bot connection/binding metadata
   - durable approval and routing records

2. **A shared coordinator (for example Redis) for ephemeral coordination**
   - leader election for watchdog/sweep jobs
   - distributed locks / leases
   - queue wake-ups and short-lived dedupe keys
   - provider ownership and pub/sub fan-out

Do not enable multi-instance execution merely because PostgreSQL is available. The runtime supervisor and provider services must first use shared leases so only one worker owns each singleton background responsibility.

## Migration sequence

1. Keep the current repository/service interfaces as the application boundary.
2. Introduce PostgreSQL implementations behind those interfaces and run dual-read/verification tests.
3. Add coordinator-backed leases to `RuntimeSupervisor` and provider workers.
4. Add a migration command that snapshots SQLite, copies documents transactionally, verifies counts/checksums, and can be re-run safely.
5. Exercise failover with two instances before declaring replicated mode supported.

The SQLite backup/status methods added here are prerequisites for that migration tooling; they are not a claim that codex-web is already safe for active/active deployment.
