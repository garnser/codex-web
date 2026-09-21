# Canonical Agent Teams and bounded delegation

## Status

**Architecture contract.** Agent Teams/Squads compose existing Agent Profiles into a bounded, auditable assignment target. Teams do not introduce a new model provider, worker identity, authority system, or ownership model.

The design follows the repository Token Efficiency Ruleset:

> deterministic routing first; event-driven leader reasoning only when selection is ambiguous; bounded participants/rounds/handoffs; no LLM polling while waiting.

## Canonical model

An Agent Team is a tenant/workspace-scoped, immutable revision containing:

- a stable Team ID, name, description, lifecycle, and owner;
- an exact leader Agent Profile ID and revision;
- agent or human members with capability tags and role descriptions;
- an exact published `agent.team-routing@1.0` Definition reference;
- the existing Agent Profile access-policy type for Team invocation access;
- explicit handoff, participant, coordinator-round, and parallelism budgets;
- Attention escalation recipients.

Agent members pin exact Agent Profile revisions. The logical Agent Profile's current lifecycle and invocation policy still govern whether new execution may begin. A Team revision therefore remains reproducible without allowing an archived/disabled or newly unauthorized profile to continue accepting new work.

Capability tags and role descriptions are routing metadata only. They never grant authority.

## Definitions are data

Team routing/delegation instructions are stored as versioned Definition Registry records with kind `agent.team-routing` and schema version `1.0`.

A Team revision pins the exact published Definition checksum/revision. Coordinator context is built only from that pinned Definition and the bounded relevant roster. Changing instructions creates a new Definition revision and a new Team revision.

Routing instructions are advisory reasoning input. They cannot alter:

- Agent Profile access policy;
- canonical authority roles/grants;
- execution profile/sandbox policy;
- worker capabilities or fencing;
- approval requirements;
- Team budgets.

The control plane validates all coordinator output after reasoning and before dispatch.

## Assignment and routing flow

A Work Item may be assigned to a Team without replacing its canonical concrete owner. `WorkItemState` records the Team ID, Team revision, and delegation ID as assignment/routing provenance.

For each Team assignment:

1. Validate tenant/workspace, Work Item/project identity, Team lifecycle, and Team invocation access.
2. Filter disabled/non-agent members and evaluate each candidate's current Agent Profile access/authority for the target Project.
3. Match required capabilities deterministically.
4. If exactly one eligible member matches, dispatch that member directly.
5. If multiple eligible members match, dispatch only the pinned Team leader with minimum sufficient context.
6. If no member is eligible, persist a blocked delegation and create canonical Attention.
7. A leader response must be submitted as a structured coordinator decision.
8. The control plane validates Team revision, leader identity, selected membership, current Agent Profile eligibility, self-loop rules, participant/parallel/handoff/reasoning budgets, and stale-event correlation.
9. Only validated members are dispatched through the normal Agent Profile thread/turn pipeline.

The Team layer never creates raw worker assignments. Production dispatch uses the existing thread/turn path, preserving Agent Profile resolution, model/runtime routing, execution preflight, sandbox and approval policy, execution workspace binding, worker trust/fencing, and execution provenance.

## Structured coordinator contract

Coordinator context is bounded to:

- current objective;
- Work Item and Project IDs;
- exact Team/delegation revision IDs;
- required capabilities;
- pinned routing instructions;
- a roster capped by the Team participant budget;
- member IDs, exact Agent Profile revisions, capability tags, and role descriptions;
- explicit Team budgets.

The leader emits a structured decision naming member IDs and a reason. Free-form instructions do not directly execute work.

The leader may not delegate to its own Agent Profile. A self-selection attempt is persisted as an escalated delegation and creates Attention rather than recursively invoking the leader.

## Loop, concurrency, and token controls

Default Team budgets follow the token-efficiency policy:

- maximum participants: 3;
- maximum coordinator reasoning rounds: 1;
- maximum handoffs: 4;
- maximum parallel member executions: 3.

All values are explicit bounded Team data.

Equivalent input triggers have a stable trigger hash and event ID. Duplicate triggers return the canonical existing delegation instead of starting new reasoning or work.

Coordinator decisions have a separate selection hash. Replaying the same active decision is idempotent. Repeating the same failed delegation selection is treated as a loop and escalates.

Member results carry event IDs and may carry execution IDs. Duplicate result events are ignored. A result for an older execution ID cannot mutate a newer active member execution.

When parallel members are still running, the Team service persists waiting state and performs no model call. Relevant completion/failure events may request re-coordination. Those in-process triggers use `KeyedTaskCoordinator` with a semantic delegation key, latest-event revision, Project scope, bounded concurrency, and timeout. The coordinator always re-reads canonical delegation state before acting.

If the coordinator-round budget is exhausted, the Team escalates through Attention instead of reasoning again.

## Work Item and audit visibility

The Work Item remains the canonical lifecycle/terminal-state owner. Team assignment adds provenance fields but does not create a shadow lifecycle.

Team delegation records expose:

- deterministic vs coordinator routing mode and reason codes;
- exact Team revision and routing Definition provenance;
- selected and active member IDs;
- member execution/thread links;
- coordinator round and handoff counts;
- completed/failed member state;
- blockers and escalation status;
- coordinator/member token and cost counters.

Work Item events record Team assignment, coordinator dispatch, member dispatch, and member results. The Team API provides bounded delegation history by Team or Work Item.

## Attention and escalation

Canonical `AttentionService` owns operator intervention. Team failures use dedupe keys scoped to delegation and failure class for cases such as:

- no eligible member;
- unavailable leader/member;
- self-delegation;
- repeated equivalent delegation after failure;
- coordinator budget exhaustion.

Teams do not implement a parallel notification or scheduler subsystem.

## Access and authority boundary

Team invocation access answers whether an actor may target the Team. It is distinct from member authority.

Before every member execution, the current Agent Profile invocation decision is evaluated for the actual Project. Agent Profile authority requirements remain fail-closed. A coordinator cannot bypass a denied profile by naming it, and capability/role labels never count as grants.

## Runtime and UI boundary

The backend API exposes Team CRUD/revisions, assignment, structured coordinator decisions, result correlation, usage attribution, and history under `/api/agent-teams`.

The shared Agent/Team/Skill product workspace is tracked separately by the existing UI follow-up. UI code must consume this canonical API/state and must not reconstruct Team routing, budgets, or delegation state client-side.

## Shutdown and recovery

Team revisions and delegation history are durable SQLite StateStore data. In-process re-coordination is intentionally ephemeral and event-triggered; canonical result state remains durable. On process shutdown the Team's keyed background coordinator is stopped/cancelled. A retried external/canonical event is safe because trigger/result dedupe is persisted.

## Non-goals

Agent Teams do not:

- replace Agent Profiles;
- grant authority;
- create a new worker pool or execution protocol;
- own Work Item terminal state;
- poll agents while waiting;
- run every member to ask who should work;
- permit unbounded agent-to-agent conversations.
