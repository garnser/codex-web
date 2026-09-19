# Bounded autonomy controller

## Status

**Milestone 7 control plane.** The autonomy controller consumes canonical event
facts from the M7 event boundary and separates deterministic observation,
reasoning, and external execution.

## Cycle contract

Every autonomous cycle follows this order:

```text
Canonical event
    ↓
Deterministic observation
    ├── resolved → record cycle, zero reasoning
    └── unresolved
          ↓
      mode / trigger / threshold / depth / cooldown gates
          ↓
      bounded reasoning retries
          ↓
      action-count budget
          ↓
      durable ActionIntent creation
          ↓
      existing authority + policy + security checks
          ↓
      worker execution / verification outside this controller
```

The controller never calls an ActionProvider directly. External effects are
represented as `ActionIntentCreate` records and cross the existing #137
ActionIntent boundary. A denied authority/security decision results in a
blocked autonomy cycle and no provider execution.

## Deterministic-first reasoning gate

A caller must provide an `AutonomyObservation`. If
`deterministic_resolved=true`, the cycle terminates without invoking a
reasoner regardless of its score.

For unresolved events, reasoning is permitted only when all configured gates
pass:

- autonomy mode is `active`;
- recursion depth is within the configured maximum;
- the event type is allowed by optional trigger rules;
- the deterministic reasoning score meets the threshold;
- the event-class/cycle cooldown is not active;
- neither dry-run nor simulation mode is suppressing execution.

No idle loop invokes the reasoner. Event-driven GitLab routing and legacy
watchdog fallbacks both pass through this boundary before an agent prompt is
sent.

## Loop and blast-radius safeguards

Controls are persisted in the shared SQLite state store and include:

- pause, resume, and global kill state;
- dry-run and simulation modes;
- reasoning score threshold;
- repeated-event cooldown;
- maximum reasoning attempts with exponential backoff;
- maximum recursion depth;
- maximum actions created by one cycle;
- optional allowed trigger event types.

Duplicate cycle keys for the same canonical event return the previously
recorded cycle. Canonical ingress also removes duplicate provider deliveries
before the autonomy controller sees them.

Reasoning failures exhaust a bounded retry count and enter a durable dead-letter
history. Recursion or action-budget violations fail closed and are also
dead-lettered.

## Observability and attribution

Each cycle records metadata only:

- canonical event ID/type/source;
- correlation and causation IDs;
- tenant/workspace scope;
- cycle key and recursion depth;
- reasoning score, whether reasoning ran, and attempt count;
- number of proposed actions and resulting ActionIntent IDs;
- outcome/reason/error and timestamps.

Recent cycle and dead-letter history is exposed through `GET /api/autonomy`.
Control mutations require autonomy-admin authorization (and MFA for human
administrators):

- `PATCH /api/autonomy/control`
- `POST /api/autonomy/pause`
- `POST /api/autonomy/resume`
- `POST /api/autonomy/kill`

## Legacy watchdog migration

Owner-work, release-gate, orchestrator, split-brain, and work-item-SLA fallback
reasoning now creates a canonical `work.transition` event and passes through
this controller. Purely deterministic watchdog repairs, such as expiring an
unacknowledged handoff and updating typed canonical state, remain model-free.

## UI follow-up

Issue #112 owns the orchestration inspector, event timeline, and autonomy-cycle
UI. It should consume the canonical cycle/event records rather than inventing a
separate browser state machine.
