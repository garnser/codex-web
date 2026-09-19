# Upgrade and rollback

> Applies to: current main  
> Audience: administrators and operators  
> Risk: control-plane change; production upgrades can affect canonical state and external execution

## Goal

Upgrade codex-web without silently crossing an incompatible schema, Definition, worker, extension or rollback boundary.

## Prerequisites

Before a production upgrade:

- the current deployment is healthy;
- Recovery is configured and has a fresh successful restore verification;
- the target immutable Release exists;
- the target compatibility profile is known;
- required worker, extension, event/API, StateStore and Definition versions are supported;
- operators understand whether any migration step is irreversible;
- active operations can be drained when the Upgrade Plan requires it.

Read [Safe upgrades](../architecture/safe-upgrades.md) and [Recovery continuity](../architecture/recovery-continuity.md) before operating a production upgrade.

## Production upgrade

Use the canonical Upgrade lifecycle rather than applying database changes manually.

1. Create an Upgrade Plan for the current and target versions.
2. Run preflight.
3. Resolve every blocking compatibility result.
4. Enter drain/maintenance mode if the plan requires it.
5. Capture the pre-upgrade backup.
6. Obtain the canonical ApprovalRequest for any declared irreversible step.
7. Execute ordered expand-compatible, rollout, verification and contract/cleanup steps.
8. Run post-upgrade verification.
9. Exit maintenance only after verification succeeds.

The operator UI and `/api/upgrades` expose the same canonical Upgrade state.

## Verify

A completed upgrade should have:

- satisfied preflight Evidence;
- successful migration-step Evidence;
- compatible active Definition revisions;
- compatible workers/extensions;
- successful post-upgrade Evidence;
- no duplicate or unknown external side effects;
- an explicit rollback-availability state.

## Rollback

Rollback is allowed only while the Upgrade Plan still reports it as compatible.

Do **not** assume an older application binary can read state after an irreversible schema or Definition migration. Once the plan crosses an irreversible boundary, codex-web intentionally stops advertising rollback.

When rollback is still supported:

1. stop/drain new ordinary execution;
2. use the canonical rollback operation for the Upgrade Plan;
3. restore the known-compatible application/state combination as required by the plan;
4. reconcile pending/unknown ActionIntents;
5. verify health, Definitions, workers, schedules and provider state before resuming autonomy.

## Simple local development checkout

For a disposable/local development environment, normal source-control/container update procedures may be sufficient, but back up any state you care about first. Do not use a development shortcut as a production migration procedure.

## Failure modes

| Symptom | Meaning | Response |
| --- | --- | --- |
| preflight blocked | target is not safely compatible | resolve the reported blocker; do not force the step |
| migration interrupted | step did not complete | resume only if the step is declared idempotent/resumable |
| rollback unavailable | irreversible boundary crossed | follow recovery/forward-fix procedure rather than pretending downgrade is safe |
| post-upgrade verification fails | target is not qualified | keep maintenance/drain posture and investigate Evidence |
