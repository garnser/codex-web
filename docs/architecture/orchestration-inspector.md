# Orchestration inspector

## Purpose

The orchestration inspector is a read-only projection over canonical event, bounded-autonomy, scheduler, evaluation, approval, Attention, execution, runtime, model-routing, ActionIntent, and Evidence state. It lets an operator reconstruct why an event did or did not result in reasoning or an external action without creating a second UI state machine.

Refreshing the inspector is deterministic and side-effect free. It reads durable stores only. It cannot invoke a model, poll a provider, run an evaluator, advance a schedule, execute a worker, mutate an ActionIntent, or contact an external system.

## Data path

CanonicalEventStore, AutonomyStateStore, SchedulerStore, EvaluationStore, ApprovalRequestStore, AttentionStore, ActionIntentStore, AgentSessionStore, AgentRuntimeUsageStore, ModelGatewayStore, and ArtifactEvidenceStore feed OrchestrationInspectorService. The service exposes /api/orchestration/inspector, rendered by static/orchestration_ui.js.

The inspector API is tenant/workspace scoped from the authenticated actor. Service accounts require the existing orchestration/autonomy read scopes; human readers require canonical administrator authority.

## Event-to-result pipeline

Every displayed event projects the same canonical chain:

event → deterministic filter → reasoning gate → runtime/model routing when recorded → authority / ApprovalRequest → ActionIntent execution → provider receipt / verification / Evidence → Attention when human intervention is required.

The projection exposes:

- event ID, event type, source, timestamp, idempotency key, correlation and causation IDs;
- deterministic cycle/filter outcome and the exact recorded reason;
- whether reasoning was invoked or skipped, reasoning score, attempts, cooldown/backoff-sensitive outcome, recursion depth, and action count;
- selected AgentSession/provider/runtime/capability revision when canonical runtime telemetry can be correlated;
- ModelGateway invocation metadata including model class, route reason, selected provider/model/version, exact prompt-template version/checksum, and policy fingerprint;
- ActionIntent authority, policy and security decisions, provider binding, retry state, receipts and verification receipts;
- matching canonical ApprovalRequests with exact target/version, quorum, assurance and decisions;
- matching canonical Attention items rather than feature-local badges;
- related Evidence records;
- dead-lettered autonomy cycles and their bounded retry failures.

Correlation is deterministic and uses canonical IDs already present on events, cycles, ActionIntents, execution records, runtime telemetry, model invocations, approvals, Attention items, and Evidence. The inspector does not ask a model to infer relationships.

## Durable schedule inspector

The scheduler section reads SchedulerStore directly and displays schedule owner and tenant/workspace scope, recurrence/timezone, next and last firing, misfire/catch-up semantics, firing count, canonical event source, and recent emitted schedule events with scheduled-for/fired-at provenance.

Pause, resume and cancel buttons call the canonical /api/schedules transition endpoints. The UI does not maintain its own scheduler state.

## Evaluation and replay inspector

The evaluation section consumes the canonical #159 persisted records and shows scenario ID/version, replay fixture/checksum, replay backend/mode, exact Definition Registry revisions, exact AgentProvider/AgentRuntime capability revision, model/provider version, prompt-template version/checksum, policy fingerprint, deterministic assertion failures, usage, candidate-vs-baseline regressions, failure injection, suite results, and Evidence IDs.

Opening or refreshing this section never runs a replay. Evaluation execution remains an explicit operation on the evaluation API/CI path.

## Approval and human intervention

The inspector lists canonical ApprovalRequests and Attention items for the current tenant/workspace and deep-links them to their owning APIs. Approval truth remains in #338; the inspector exposes target/version, policy/authority source, assurance requirement, quorum and human decisions without calculating a separate UI quorum. Human-intervention truth remains in #160; the inspector shows owner, severity, source, due/escalation state and deep-link without creating a feature-local notification lifecycle.

## Operator controls

Autonomy controls use the same canonical endpoints enforced by runtime: pause, resume, global kill, dry-run, and simulation. Schedule pause/resume/cancel likewise uses canonical scheduler endpoints. High-impact operations therefore remain subject to the same identity, assurance, policy and authority checks as non-UI callers.

## UI behavior

The inspector remains inside the existing Developer workspace. Layouts stack at narrow viewports so the event pipeline, schedules, evaluations, approvals and Attention remain usable on phone-width screens.

The UI does not poll continuously. It loads when the Developer workspace opens and on explicit refresh/control actions. No refresh path can consume model tokens.
