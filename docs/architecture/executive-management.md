# Canonical Executive Management

Milestone 9 turns Executive roles from free-form personas into bounded advisory roles over canonical company state. Executive reasoning can recommend or propose changes, but it is never itself authority to mutate Goals, Decisions, Work, provider state, or external systems.

## Role policy

The Executive role catalog is a versioned Definition Registry object (`executive-role-catalog`).

Each role defines:

- stable ID, title, lifecycle, and responsibilities
- canonical object types it may observe
- event subscriptions that can justify activation
- deterministic routing keywords
- bounded consultation peers
- allowed proposal kinds
- whether a proposal is eligible for canonical materialization
- the exact authority capability required for each materialization kind
- role-specific instructions and context/consultation limits

The bootstrap catalog includes Chief of Staff, CTO, CPO, COO, CFO, VP Engineering, Security, CRO, CMO/Growth, and Customer Success. The Definition Registry remains the source of truth once published; later catalog revisions do not rewrite prior activation history.

## Activation and routing

An `ExecutiveActivation` is a durable tenant/workspace-scoped record. It stores:

- trigger kind and trigger reference
- explicit event type when event-driven
- relevant Goal/Decision/Work/Evidence references
- exact Executive role Definition revision
- deterministic role-selection scores and reasons
- a bounded reasoning budget
- exact canonical context snapshot
- specialist consultations and model invocation IDs
- synthesis, disagreement, and escalation state
- advisory proposals
- revision history

Role selection is deterministic before any model call. Explicitly requested roles are honored only within catalog limits. Otherwise subscriptions and keyword matches determine relevant roles. If nothing matches, the catalog's fallback role is selected. Routine work therefore does not invoke every Executive role.

Event subscriptions are routing policy, not polling loops. Codex-web does not run free-running Executive model checks. A request, canonical event, or scheduled review must explicitly create an activation.

## Canonical context

Executive reasoning receives only explicitly referenced canonical state that the selected role policy permits:

- Goal snapshots
- Decisions
- Work Items
- WorkGraph snapshots
- Evidence

References are resolved inside the authenticated tenant/workspace before model invocation. Missing or cross-tenant references fail closed. Context is wrapped as untrusted data before it is passed to the Model Gateway; task text, evidence text, provider output, and model output cannot grant authority.

This M9 layer intentionally does not make the legacy Executive chat transcript a source of company state.

Project WorkGraph context is an Executive-specific bounded projection of the
complete canonical snapshot, rather than every project node in the model prompt.
It retains exact whole-project progress, tenant/project scope and a SHA-256 digest
of the complete source snapshot. Selected nodes retain their complete canonical
readiness and findings; edge, runnable, failure-impact and critical-path lists
explicitly report omitted counts. Critical-path aggregate counts describe the
whole graph even when its displayed references are partial. `complete=false`
must never be interpreted as an empty or healthy remainder.

The existing role context-item limit and ordinary canonical retrieval capacity
bound the projection. A numeric per-call input budget further caps graph data
at half that budget; disabled consultation enforcement never makes retrieval
unlimited. Explicit Work references and their incoming
parent/dependency closure are mandatory: unavailable or oversized required
context refuses preparation before activation persistence or model invocation.
Remaining capacity prioritizes critical-path, failed-dependency, blocked,
runnable and active work deterministically. Selected canonical rows are not
silently text-truncated. Ordinary operator graph APIs remain complete; this adds
no graph store, cache, model call, authority or larger token budget. Memory and
business context retain their existing retrieval and governance owners.

## Bounded multi-role consultation

A consultation performs:

1. one strategic Model Gateway call for each selected role, in parallel
2. at most one synthesis call when more than one role participates
3. exact JSON/schema validation of every specialist output
4. durable recording of role outputs and model invocation IDs
5. explicit preservation of material disagreement and escalation

The activation budget caps model calls, input tokens, output tokens, and cost. Creation fails before any model invocation when the budget cannot cover the selected roles plus required synthesis.

A role output may contain advisory proposals only. It cannot claim that a Goal, Decision, Work Item, approval, or external action has already happened.

## Materialization and authority

Executive proposals remain `proposed` until a separate canonical materialization request succeeds.

Supported materialization paths are deliberately narrow:

- Goal proposal -> canonical `GoalService.create`
- Decision proposal -> canonical `DecisionService.create`
- Work proposal -> canonical `DecisionWorkService.commit`

Before materialization, codex-web evaluates the authenticated actor through the canonical operational Role/authority service using the capability required by the Executive role catalog. An authority denial leaves the proposal advisory and performs no company-state mutation.

Authority grants that require qualifying approvals remain denied until those approvals are satisfied by canonical policy. Executive Management does not implement a second quorum system.

Work proposals additionally require a Decision ID and pass through the M8 Decision-to-Work bridge. That bridge requires the Decision to be canonically approved and creates external task-source work only through durable ActionIntent/ActionProvider execution. Executive code never calls an external provider directly.

Goal and Decision records created from Executive proposals store the trusted activation/proposal origin on the canonical record. Before creating either object, the materialization bridge searches for that exact origin and reuses it. This makes retries safe even if canonical creation succeeded but the Executive proposal bookkeeping update was interrupted.

Escalation proposals are never automatically materialized into company state. They remain human/action-routing signals for the canonical Attention/Approval/Decision workflows.

## Compatibility

The older Executive chat/delegation API remains available during migration, but its `/api/executive/agents` metadata is sourced from the canonical Executive role catalog when the catalog service is installed. Existing execution-role delegation remains governed by the separate canonical execution-role contract and Work Item ownership state.

The new canonical management API lives under `/api/executive/roles` and `/api/executive/activations...`. Milestone 9 UI work in issue #116 should visualize these records rather than creating a dashboard-owned Executive lifecycle.


## Management workspace and explainability

The canonical Executive/Organization workspace is a read-through and command surface over the M9 APIs. It does not own an Executive-specific task or approval lifecycle.

The workspace exposes:

- the current Executive role Definition revision, responsibilities, observable object types, event subscriptions, consultation relationships, proposal permissions, and required authority capabilities
- recent durable Executive activations
- the exact trigger kind, event type/reference, initiator, deterministic role-selection score, and selection reasons answering “why was this Executive activated?”
- captured canonical Goal, Decision, Work, WorkGraph, and Evidence context
- specialist consultation outputs and Model Gateway invocation IDs
- bounded activation budgets
- synthesis, disagreement, and explicit escalation reason
- advisory proposal state, authority capability/reasons, materializing actor, and resulting canonical reference
- activation revision history

Creating an activation and running consultation are separate UI actions. Creation records canonical routing/context state but makes no model call. Consultation performs the bounded model work defined by the backend. Proposal materialization is a third, separately authorized action.

Navigation from Executive activity reuses existing canonical views:

- Goal references open the Goal workspace
- Decision references open the Decision workspace
- Work and ActionIntent references open the canonical ActionIntent timeline/search
- Evidence references open the canonical Artifact/Evidence explorer/search

The workspace can dispatch navigation to these views, but it does not copy their records into browser-owned state as a new source of truth. External consequences shown from a materialized Work proposal remain visibly chained through approved Decision → ActionIntent → ActionProvider rather than being presented as an “Executive action.”

The legacy Executive advisor drawer remains available during migration for conversational compatibility. The Organization workspace is the canonical management/explainability surface; a chat transcript remains non-authoritative.

### Context preparation timing and timeout outcomes

Activation preparation retains its existing bounded request window. Existing
correlated internal telemetry records executor queue wait and closed preparation
stages (catalog, business scope, canonical context, WorkGraph, and Memory), plus
persistence duration. It contains no request/context content, exception text or
stack. Late preparation may finish and log its duration after the request expires;
that completion does not invoke activation persistence.

`executive_context_timeout` identifies expiry of the preparation wait.
`executive_context_dependency_timeout` identifies a timeout raised by preparation
itself. `executive_activation_persistence_timeout` reports an unknown persistence
outcome and requires canonical inventory reconciliation rather than resending the
request. Neither the dependency nor persistence response recommends retrying.
These error distinctions use the existing API error surface; no new operator
control or independent lifecycle state is introduced.


### Explicit per-activation ceiling opt-out

An authorized operator can set `budget.max_input_tokens`, `max_output_tokens`,
and `max_cost_usd` to JSON `null` when temporarily qualifying a new consultation.
The existing UI exposes this choice on creation and displays disabled ceilings
on the persisted activation. Omission retains the numeric defaults. Historical
activations and revision budgets are never rewritten. Call count remains finite;
for a single-call qualification select one role and `max_model_calls: 1`.

Null removes only the application ceiling. ModelGateway still enforces model
context capacity, declared output capacity, policy/mapping constraints, authority,
provider admission, and usage attribution. Output null resolves to the smaller
of declared model output capacity and remaining context capacity. Native
entitlement requires exact null/finite caller-marker agreement before routing;
the native adapter then binds separate finite declared model capacities for the
writer. Null application markers remain null: an output reservation estimate is
not an observed-input ceiling. Final and streaming native usage checks retain
finite caller limits, declared output capacity and the joint input-plus-output
context capacity. These declarations require their own configuration provenance;
they are not automatically attested physical provider limits.
No fallback, tool loop, or additional call is authorized by this option. Retrieval
limits remain finite independently of invocation ceilings.

Executive activation state envelope 1.1 adds nullable ceilings. Its 1.0 migration
changes only the envelope version and preserves every activation/revision. Old
readers refuse 1.1; rollback requires compatible readers, not dropping nulls or
inventing numeric limits. Input pipeline projections preserve the same explicit
null output marker until routing. This operator exception to default bounded
application budgets does not waive provider limits or canonical policy. See #1784.

Deployment must declare this domain-envelope reader change in the existing
Release/Upgrade compatibility evidence: all readers of `executive_activations`
must support 1.1 before new writes. The physical StateStore schema number is
unchanged; it is not a substitute for this domain compatibility check. The
existing migration registry owns decoding, with no new deployment controller.

Native reasoning Profile defaults are independent configuration. A null activation
cannot waive an existing Profile or policy constraint. The canonical Profile
schema already permits null input/output/cost defaults; removing such defaults
requires an authorized new Profile revision and matching exact native binding
configuration. Runtime-only native selection has no ModelInvocationRequest and
therefore does not consume the Profile's model-request budget defaults; normal
AgentRouting model requests still apply them. A null request under a finite
Profile ceiling adopts that ceiling, rather than raising a type error. See #1790.
### Native preparation diagnostics

The existing runtime event journal records `native_reasoning_preparation_failed`
with the original Executive execution, activation, role and tenant scope, a fixed
preparation stage, and a closed exception category. It excludes exception text,
tracebacks, prompts and credentials. A validated controller-owned ThreadService
prestart refusal may also carry its closed bootstrap stage and exception category.
Only that refusal proves provider thread startup was not called. Other failures
before native input admission do not establish that no provider thread exists.

Diagnostic failure cannot replace the original refusal or its cleanup. Failures
after native input admission retain their existing unknown-outcome semantics.
These events confer no retry, replay, START, completion or accounting authority.

Cancellation and finite dependency timeouts also record
`native_reasoning_lifecycle_interrupted` in that same event journal. It captures
the interrupted closed phase, total and phase monotonic elapsed seconds (capped
at one day), and already-bound execution/native identifiers. It contains no
exception text, prompt, output, reasoning, credentials or process diagnostics.
`native_input_requested` means the original TurnService call was entered;
`native_queue_submitted` means it returned its queue ID. Neither proves native
START. Canonical START and measured terminal usage remain separate evidence.

Ordinary admitted failures also use this event with `interruption=failed` and
`native_exception_type` from the existing closed exception-category mapper
(unknown classes become `Exception`). The snapshot precedes UNKNOWN wrapping
and cleanup, so successful native usage followed by a result-attribution or
assignment-completion failure remains distinguishable. Initial guard and cleanup
failures retain their original exceptions. No exception message or arbitrary
class name is recorded, and successful inference alone grants no result authority.

The cancellation snapshot precedes the original owned cleanup join, so cleanup
cannot overwrite the phase that was waiting at the deadline. Cancellation during
completion or cleanup is reported as that phase. A broken diagnostic sink cannot
replace the primary exception or skip joining the original cleanup. No new store,
timer, model call, retry, deadline extension or successful outcome is introduced.
Operators inspect these events through existing diagnostics; no new UI object or
success indicator is added. This evidence does not identify or fix the underlying
cause of an inference timeout.

### Overlapping independent context retrieval

When both WorkGraph and Organizational Memory are visible, context preparation may overlap their existing governed operations. The graph snapshot and its bounded Executive projection run in one additional worker; Memory retrieval and its canonical retrieval audit remain on the calling thread. Both complete before preparation returns, retaining graph-first error precedence. A completed authorized Memory retrieval can therefore retain its truthful retrieval-attempt audit even when the sibling graph operation fails; no activation or model invocation is created by that failure.

Each Executive service admits at most two additional context workers, without a waiting queue. Saturated requests use the original sequential path. The worker uses a copied context, including tenant tracing and the same cooperative storage deadline, and its private executor is joined and shut down on every exit. It does not depend on the default executor already occupied by the parent. Unsupported/custom storage compositions and canonical PostgreSQL owning callbacks retain sequential execution; an owning transaction's connection is never transferred across threads. The original five-second API window, visibility, full current validation, retrieval limits, audit preservation, and persist-only-after-success rule remain unchanged. Overlap does not establish a production deadline pass; that still requires genuine post-deployment qualification.


Managed native completion first advances the canonical ActionIntent readiness-origin
index through its existing preparation API. This preparation has a 30-second
cooperative storage budget (bounded by the caller's remaining budget) and at most
eight advancing cursor observations, with current worker lease/fence authority
validated before every step. Every cooperative store guard also observes parent
budget cancellation. Cancellation joins the original preparation owner.
Only preparation progress is continued: the worker terminal transition is called
once and retains its final current-origin check under the canonical writer lock.
Unresolved origins, invalid or stalled indexes and changed custody still refuse
completion. This does not retry a model call or infer that an unknown origin is empty.

## Executive invocation duration

`executive.invocation_timeout_seconds` is a canonical deployment Configuration
integer from 1 through 600 seconds, defaulting to 120. Existing Configuration
administration exposes its bounds, value and publication provenance. Publication
applies to the next consultation without a restart. One fresh resolution is
captured before that consultation is reserved; every role and any synthesis use
the same value even if an administrator publishes another value while it runs.
The next consultation resolves again.

The duration covers the complete Model Gateway attempt, including native
preparation, inference, attribution and completion. The same value bounds the
native invocation context and its owned process. Correlated metadata records the
activation, effective seconds and Configuration record/revision; no activation or
invocation history schema is rewritten. A shared, resource-constrained host may
need a longer finite allowance; elapsed time alone is not a failure diagnosis.
Call counts, token/cost budgets, authority, leases, fences, resources and positive
cleanup joins retain their existing controls. Expiry retains UNKNOWN outcomes
without retry permission. An operator's outer HTTP wait must accommodate the
selected duration and cleanup; extending that wait cannot revive an old attempt.
