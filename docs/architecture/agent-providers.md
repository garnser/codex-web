# Canonical Agent Providers

AgentProvider is the provider-neutral identity and capability layer above model inference and agent execution. A provider may supply model inference, execution-agent behavior, or both. Domain/orchestration code must depend on canonical capabilities rather than Codex- or Claude-specific protocol details.

## Capability authority boundary

Provider capabilities are descriptive inputs to routing, not permissions.

The registry tracks two sets:

- **declared capabilities** — what the provider says it can technically perform
- **granted capabilities** — the subset tenant/platform policy currently permits the provider to expose

Effective capabilities are the intersection of declared and granted capabilities after lifecycle, health, compatibility, extension and linked-model checks. A provider declaration can never grant Role authority, resource access, secret access, ApprovalRequest bypass, ActionIntent permission, worker trust, sandbox escape or data-governance exceptions.

## Capability taxonomy

The v1 code-owned taxonomy includes:

- model inference
- agent execution
- persistent/resumable sessions
- native execution objectives/goals
- streaming
- interrupt/cancel
- filesystem editing
- shell/tool execution
- git/repository operations
- interactive approval requests
- native context compaction
- subagents
- MCP/tool-server integration
- exact usage reporting
- partial usage reporting

Model inference and agent execution are independent capabilities.

## Provider record

A canonical provider record contains stable provider ID and tenant/workspace scope plus:

- display identity and lifecycle
- declared/granted capabilities
- linked ModelGateway provider IDs
- optional extension installation and immutable extension provenance
- configuration references
- credential references only (never secret values)
- residency/compliance metadata
- health and compatibility state
- actor/timestamp/revision provenance

## ModelGateway compatibility

Existing ModelGateway providers remain authoritative for model routing and invocation. AgentProvider discovery deterministically synthesizes a model-only provider view for ModelGateway providers that do not yet have an explicit AgentProvider record.

This preserves existing ModelGateway APIs while making model providers visible through the same provider identity/capability surface later used by execution runtimes. An explicit AgentProvider record can link one or more canonical ModelGateway provider IDs and add independently granted execution capabilities.

Disabled ModelGateway providers cannot become eligible through AgentProvider metadata.

## Extension lifecycle

Provider records may reference a canonical extension installation. The immutable extension identity/version/digest is copied into provider provenance for inspection.

An extension-backed provider is effective only while the referenced installation is present in the same tenant/workspace, enabled, and not unhealthy. Model-inference extensions must declare the extension `model_provider` type; execution-agent extensions must declare `worker`.

Extension installation/enabling does not automatically grant AgentProvider capabilities; declared and granted capability sets remain separate.

## Deterministic discovery

`/api/agent-providers/discover` evaluates requested capabilities against the canonical provider view without model calls. Results expose:

- provider record/revision
- effective capabilities
- eligibility
- deterministic reasons for denial or mismatch

Unavailable/incompatible/disabled providers fail closed. Missing requested capabilities make a provider ineligible for that discovery request without changing the provider's canonical declaration.

## Downstream contract

AgentRuntimeAdapter and AgentSession own execution-session behavior, while the routing policy owns model/runtime selection. Those layers must consume effective AgentProvider capabilities and must not route from raw declarations.

The provider layer intentionally does not execute actions, start sessions, reveal credentials, or authorize resource access. Those remain behind canonical runtime/worker, identity, policy, ApprovalRequest, ActionIntent, resource, secret and data-governance boundaries.


## Native execution objectives

`NATIVE_EXECUTION_OBJECTIVES` is capability-gated runtime functionality, not
canonical Goal authority. When effective, the AgentSession boundary may create,
inspect, update, or clear the provider-native objective through the supported
runtime protocol. codex-web never reads provider-private persistence such as local
SQLite files to infer this state.

Native objective discovery is read-only until an authorized operator explicitly
attaches the session to a canonical Goal or promotes its objective text into a draft
Goal. A runtime that lacks the capability returns explicit unsupported behavior and
continues to participate through canonical GoalExecutionBinding checkpoints and
persistent AgentSession continuation.

Provider-native completion/failure events are runtime observations. They may update
the execution projection and trigger bounded continuation/recovery, but they cannot
mutate canonical Goal completion, budgets, approvals, authority, Work Graph state,
Evidence, ActionIntent, sandbox, or resource policy.
