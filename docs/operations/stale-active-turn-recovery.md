# Stale active-turn recovery

Active-turn records are runtime recovery markers, not disposable locks. Codex Web therefore uses a short evidence-reconciliation window and a longer hard-expiry window instead of clearing them immediately based on age alone.

The stale-turn reconciler combines age with canonical queue, execution-assignment, worker, lease/fence and prior-resume evidence before choosing an outcome.

## Startup and runtime behavior

Startup schedules one bounded reconciliation through the keyed background-task coordinator. Queue recovery uses the same coalesced reconciler rather than independently clearing active markers.

The default stale/liveness window and scan budget are:

```text
CODEX_WEB_ACTIVE_TURN_RECONCILE_AFTER_SECONDS=120
CODEX_WEB_ACTIVE_TURN_ORPHAN_RELEASE_AFTER_SECONDS=900
CODEX_WEB_ACTIVE_TURN_RECONCILE_MAX_RECORDS=100
```

The age threshold only determines when an ownerless marker needs stale analysis. Strong canonical evidence takes precedence:

- a valid unexpired assignment lease owned by an active/draining worker remains live even when the marker is old;
- a canonical pending assignment remains live/pending worker execution;
- terminal assignment state makes a leftover active marker terminal/recoverable;
- expired leases or untrusted/lost workers classify the interrupted execution explicitly;
- a stale marker with queued work but no live owner releases the marker to the existing queue without duplicating the queued turn;
- an old `queued:*` marker with no queue, assignment or owner is classified interrupted rather than left active indefinitely;
- an ownerless marker with no queue, assignment, lease/fence, or prior resume attempt is classified interrupted after the hard-expiry window, allowing unattended work recovery after a restart or abruptly terminated provider turn;
- multiple possible assignments, prior unresolved resume attempts, fence mismatches, and otherwise insufficient evidence are blocked for operator review.

Between the short reconciliation threshold and the hard-expiry threshold, an ownerless marker remains blocked for evidence or operator review. The hard expiry never overrides a live/pending assignment, queued work, a fence conflict, or prior unresolved resume evidence.

Fresh ownerless turns discovered at process startup are the only automatic legacy-resume candidates. The existing restart-resume implementation is called with exact thread IDs and no longer scans/saves the complete active-turn registry for that bounded path.

## Crash-safe reconciliation

Destructive recovery has a durable boundary:

1. classify the canonical evidence;
2. create a private backup of the legacy `active_turns.json` compatibility file, if it exists;
3. persist a recovery audit record in `planned` state, including the original active-turn snapshot and backup reference;
4. remove the canonical active marker;
5. mark the recovery record `applied`;
6. refresh the compatibility mirror.

Backup directories are mode `0700`; backup files are mode `0600` and fsynced before mutation.

If the process stops after step 3 or 4, the next reconciliation finishes the same stable recovery ID instead of creating a duplicate execution. If the active record changed after the plan was written, the old plan is converted to an operator-blocked conflict instead of deleting the newer marker.

## Operator inspection

Admin-authorized endpoints:

```text
GET  /api/active-turn-recovery/status
GET  /api/active-turn-recovery/inspect
GET  /api/active-turn-recovery/records
POST /api/active-turn-recovery/reconcile
POST /api/active-turn-recovery/{thread_id}/resolve
```

`inspect` is a bounded dry-run and does not mutate active turns. It reports the proposed outcome and supporting evidence.

`records` pages durable audit records including original active-turn state, classification evidence, recovery ID, outcome, reason, operator identity, timestamps and backup reference.

Blocked records can be resolved with one of:

```json
{"action":"retain","reason":"provider confirms the execution is still live"}
```

```json
{"action":"interrupt","reason":"operator verified that no execution can resume"}
```

```json
{"action":"release_to_queue","reason":"queued work is canonical and should continue"}
```

`release_to_queue` is rejected unless the thread currently has queued work.

## Diagnostics

The status endpoint exposes:

- current blocked count;
- last reconciliation trigger/start/completion/duration;
- scanned/stale/fresh/live/requeued/terminal/interrupted/blocked counts;
- current bounded scan cursor;
- pending crash-recovery plans;
- total applied recoveries;
- audit-record count;
- configured age and scan limits;
- configured ownerless hard-expiry limit;
- keyed coordinator running/coalesced/timeout metrics.

No unbounded recovery history is loaded to generate status.

## Recovery semantics

A terminal/interrupted result removes the active marker but retains its original state and provenance in the recovery audit. It is not silently deleted.

A requeued result removes only the stale active marker; the existing queued turn remains canonical and the normal queue drain is scheduled.

A blocked result leaves the active marker intact until evidence changes or an authorized operator resolves it.
