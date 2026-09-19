# Safe upgrades, migration orchestration, version skew and rollback

## Status

Canonical production-upgrade contract. Application upgrades are represented
as canonical Upgrade Plans with an explicit source/target compatibility profile,
ordered migration phases, durable step progress, preflight Evidence, and a
truthful rollback boundary.

Upgrades are deterministic orchestration. LLMs are not used to decide whether
versions, schemas, workers, definitions or extensions are compatible.

## Explicit compatibility profile

Each Upgrade Plan freezes the exact supported release pair and mixed-version
window:

- source and target application versions;
- deployment mode (local/shared/replicated);
- control-plane versions allowed during rollout;
- execution-worker versions allowed during rollout;
- execution-contract versions workers may consume;
- source and target StateStore schema versions;
- supported API and canonical-event contract versions;
- target Definition engine version;
- exact Definition kind/schema versions supported by that engine;
- target extension-host compatibility version;
- optional source version to which rollback remains supported.

No component guesses forward/backward compatibility from a higher/lower version
number.

## Target release binding

An Upgrade Plan is bound to one immutable Release from the release/supply-chain
domain. The Release version must equal the target application version and its
immutable artifact digest is reused when irreversible ApprovalRequests are
created.

Blocked, superseded or rolled-back Releases cannot be upgrade targets.

## Preflight

Preflight checks canonical runtime state before upgrade execution:

- current StateStore schema equals the declared source schema;
- target schema is not an unsupported downgrade;
- API/event contracts are inside the declared target window;
- observed control-plane versions fit the mixed-version rollout window;
- immutable target Release remains eligible;
- Recovery health is qualified when required;
- every effective published Definition is supported by the target engine and
  target Definition schema list;
- active/draining workers use declared rollout versions;
- nonterminal assignments use supported execution-contract versions;
- enabled/configured/upgrading extensions are compatible with the target
  extension-host version;
- active ActionIntent and worker execution is drained when the plan requires
  maintenance.

Preflight emits canonical POLICY_EVALUATION Evidence with source
upgrade-preflight. A failed preflight remains visible as explicit blockers rather
than silently being bypassed.

## Definition behavior preservation

Preflight freezes every effective published Definition by:

- record ID;
- stable Definition ID/kind;
- revision;
- definition schema version;
- checksum.

A target engine must explicitly advertise support for that kind/schema and must
also satisfy the Definition record's min/max engine version.

If a Definition must change, the upgrade records a DefinitionMigrationRecord
from the frozen source record/checksum to a published target record/checksum.
The migration must preserve the same canonical Definition slot and carries an
operator reason and actor provenance.

Post-upgrade verification rejects a changed/incompatible Definition unless that
migration was explicitly recorded. Stored payloads therefore cannot silently
acquire new meaning solely because a new engine was deployed.

## Worker mixed-version safety

ExecutionWorker now declares the exact execution-contract versions it supports.
Worker state contract 1.3 migrates existing workers to the historical 1.0
execution contract by default.

Assignment claim eligibility rejects
execution_contract_version_mismatch deterministically. A mixed-version worker
can only claim work whose execution contract it explicitly advertised.

This runtime boundary exists independently of upgrade preflight, so an
out-of-window worker cannot process incompatible canonical work even if an
operator bypasses an external rollout tool.

## Drain and maintenance

Upgrade maintenance is canonical plan state. While maintenance is active:

- ordinary ActionIntent claims are not admitted;
- new execution-worker assignments are rejected;
- execution-worker assignment claims are paused;
- incident/recovery/rollback/reconciliation ActionIntents remain eligible.

Already-running operations are not killed by entering maintenance. Preflight
reports their count and only passes the drain gate after claimed/running/
uncertain/reconciliation activity has reached a safe state.

This avoids a race where preflight succeeds and new ordinary work starts while a
migration begins.

## Migration phases

Steps are declared before execution and must be ordered:

1. **expand** — backward-compatible schema/data/Definition preparation;
2. **rollout** — control-plane/worker/extension deployment under the declared
   mixed-version window;
3. **verify** — health, compatibility and behavior checks;
4. **contract** — cleanup/removal after rollback compatibility is intentionally
   abandoned.

Each step declares kind, handler ID, idempotency, reversibility,
irreversibility, drain requirement and backup requirement.

A failed idempotent step retains attempts/error/Evidence and may be resumed.
A failed non-idempotent step is not automatically replayable.

Every attempt produces upgrade-step-verification Evidence.

## Irreversible boundaries

An irreversible step must be declared as such in the plan. Contract/cleanup
steps that are irreversible must require a pre-upgrade backup.

Before execution:

- a fresh encrypted backup is captured through RecoveryService when requested;
- a canonical ApprovalRequest is created against the exact Upgrade Plan/Step,
  target application version and immutable Release digest;
- the ApprovalRequest references the successful preflight Evidence;
- approval consumption is idempotent and bound to the step execution key.

Once an irreversible step succeeds, irreversible_boundary_crossed becomes true
and rollback_available becomes false permanently for that plan.

A process interruption after approval consumption can resume only by using the
same canonical ApprovalRequest consumption/idempotency identity.

## Rollback truth

Rollback is only advertised when the compatibility profile explicitly permits
return to the source application version and no irreversible boundary has been
crossed.

A rollback claim is rejected after an irreversible schema/Definition contract
boundary. A successful rollback record produces
upgrade-rollback-verification Evidence and exits maintenance mode.

Actual package/process rollback remains a deployment operation; the Upgrade
domain owns whether that rollback is compatible and truthful.

## Post-upgrade verification

Final verification requires:

- all declared steps completed/skipped;
- target StateStore schema active;
- frozen Definition checksums still valid or explicit Definition migrations;
- active workers inside the declared rollout window;
- enabled extensions compatible with the target host.

The result is canonical POLICY_EVALUATION Evidence with source
upgrade-post-verification. PASS completes the Upgrade and exits maintenance;
FAIL keeps the upgrade in a failed state for recovery/rollback decisions.

## API and UI

/api/upgrades exposes plan creation/list/read, preflight, drain, backup capture,
irreversible-step approval, step execution, Definition migration recording,
post-upgrade verification and rollback recording.

Human mutation requires administrator authority + MFA; service principals
require upgrade:admin.

The Autonomy Control Center and operator workspaces should display source/target versions, mixed-version warnings,
Definition incompatibilities, drain state, step progress/Evidence, irreversible
boundaries and rollback availability. The operator upgrade/runbook documentation
documentation.
