# Orchestration inspector

## Purpose

The orchestration inspector is a read-only projection over canonical event and
bounded-autonomy state. It lets an operator reconstruct why an event did or did
not result in reasoning or an external action without creating a second UI
state machine.

## Data path

```text
CanonicalEventStore ─┐
                     ├─> OrchestrationInspectorService ─> /api/orchestration/inspector
AutonomyStateStore ──┘                                      ↓
                                                static/orchestration_ui.js
```

Refreshing the inspector performs only SQLite reads and deterministic
projection/filtering. It cannot invoke a reasoner, model gateway, ActionIntent
mutation, provider action, or watchdog cycle.

The timeline exposes:

- event ID/type/source/timestamp;
- correlation and causation IDs;
- deterministic filtering/cycle outcome;
- whether reasoning was invoked or skipped and the recorded reason;
- reasoning attempts, recursion depth, and action count;
- ActionIntent IDs, deep-linked to the canonical ActionIntent API;
- dead-lettered cycles and their bounded retry failures;
- active autonomy mode and configured limits.

Autonomy controls use the same canonical endpoints enforced by runtime:
pause/resume/global kill and dry-run/simulation configuration.

## Deferred dependency-backed sections

The issue also requires schedule, evaluation/replay, and human-attention
inspectors. Those are deliberately not represented by browser-local mock state.
The current inspector reports their foundation dependencies explicitly:

- scheduler: #158;
- evaluation/replay: #159;
- attention queue/inbox: #160.

Once those foundations exist, their canonical services can be projected into
this inspector without changing the no-polling/no-reasoning refresh contract.
