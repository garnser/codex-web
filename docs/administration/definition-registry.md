# Definition Registry administration

Mutable operational definitions are database-backed so operators can inspect,
validate, publish, diff, supersede and roll them back without editing Python or
raw database rows. Schemas, interpreters, migrations, cryptographic checks and
hard security invariants remain code-owned.

This boundary is intentional: **definitions are data; engines and structural
security are code**.

## Supported lifecycle

A normal definition change follows:

```text
draft
  -> validate
     -> approval when required
        -> publish
           -> effective revision
              -> supersede or rollback
```

Quarantined/invalid/incompatible definitions must fail visibly and must not
silently fall back to an arbitrary older or hard-coded interpretation.

## Inspect before editing

Record:

- definition ID/kind;
- scope;
- lifecycle;
- schema version;
- revision/checksum;
- created/published identity and timestamps;
- current references/usage;
- exact effective revision by relevant project/role/runtime.

Historical executions, decisions, audits and evaluations should retain the exact
definition record/revision that influenced them.

## Draft and validate

Create a new revision instead of editing a published revision in place.
Validation checks the registered schema/interpreter contract for that kind.

A failed validation is healthy blocking state. Fix the draft or migrate it; do
not bypass validation by writing directly to the backing StateStore/database.

## Impact and diff

Before publication:

1. inspect the structural diff from the currently effective revision;
2. review impacted scopes/references;
3. preview effective policy/authority where applicable;
4. verify target runtime/worker/extension compatibility;
5. obtain canonical approval when the definition kind/policy requires it.

Changing a role or execution contract may affect authority, worker eligibility,
approval requirements or replay meaning even when the JSON diff is small.

## Publish, supersede and rollback

Publication makes a specific validated revision eligible to become effective.
Superseding creates a new effective revision; it does not rewrite history.

Rollback means selecting a previously compatible revision through the canonical
Definition lifecycle. A rollback is unsafe when the runtime/state has crossed an
incompatible migration boundary; use the Upgrade compatibility contract rather
than guessing.

## Scope and effective revision

Definitions may be global or scoped by supported domain semantics. The runtime
must resolve the effective revision deterministically and expose that result to
the admin/effective-policy UI.

Never implement a parallel JavaScript override that disagrees with canonical
resolution.

## Bootstrap and seeding

Bootstrap/seeding creates initial canonical records for definitions that were
formerly code-owned. Seed operations must be idempotent and preserve stable IDs
where other state references them.

After migration, code may keep schemas/interpreters/default safety invariants,
but it must not continue to act as a second mutable source of truth.

## Worked migration: execution contracts

`execution_contracts.py` is the representative migration pattern:

1. identify mutable role/execution contract data previously embedded in code;
2. define/retain a code-owned schema and interpreter;
3. seed versioned Definition records;
4. update runtime/API/UI to resolve exact Definition revisions;
5. retain revision attribution on assignments/executions/audit;
6. remove mutable duplicated code tables after compatibility migration;
7. verify old persisted references still replay deterministically.

The migration is complete only when an authorized operator can inspect,
validate, publish, supersede/rollback and determine impact without a source edit.

## Cache/reload behavior

Definition caches are derived state. Canonical database state remains
authoritative.

If a node detects a stale/incompatible cache:

- stop using the stale revision;
- reload/reconcile from canonical storage;
- expose degraded/stale status;
- fail closed where interpretation could change authority or execution.

Restarting the whole deployment should not be the only supported way to apply a
published compatible definition.

## Safe failure modes

| Condition | Expected behavior |
| --- | --- |
| Definition missing | Block the dependent operation and name the missing ID |
| Schema invalid | Keep revision non-effective; surface validation errors |
| Schema version unsupported | Block with compatibility reason |
| Effective revision stale | Reload/reconcile; do not guess |
| Publication approval missing | Remain unpublished |
| Referenced revision quarantined | Block dependent operation |
| Rollback incompatible | Refuse rollback and use upgrade/recovery runbook |
| Historical revision superseded | Preserve historical attribution/replay |

See [Definition Registry architecture](../architecture/definition-registry.md),
[Compatibility/versioning](../architecture/compatibility-versioning.md) and
[Safe upgrades](../architecture/safe-upgrades.md).
