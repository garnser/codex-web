# Capability-driven agent routing

## Status

Architecture contract for issue #351. Runtime and model selection are deterministic application logic; model reasoning is never used to choose a provider.

## Boundaries

Codex-web keeps model inference routing and execution-agent routing separate:

- ModelGatewayService owns model-provider/model selection, model allowlists, prompt/model policy, residency/compliance constraints, and model token/cost budgets.
- AgentProviderService owns canonical provider identity, declared-versus-granted capabilities, lifecycle, compatibility, extension provenance, and provider health.
- AgentRuntimeRegistry owns code-backed runtime adapter registrations and their capability revision plus routing metadata.
- AgentRoutingService combines those existing boundaries into one deterministic execution plan. It does not create a second model registry, runtime lifecycle, permission system, or source of provider truth.

A request may therefore select an OpenAI model and a Claude execution runtime, or the reverse, when both choices independently satisfy effective policy.

## Runtime routing request

The runtime routing request may constrain required execution capabilities, provider/runtime allowlists, ordered provider/runtime preferences, provider/runtime residency and compliance tags, required sandbox and network profiles, persistent-session support, a bounded runtime-session cost ceiling when runtime pricing metadata is available, whether fallback is permitted, project and role attribution, and an optional independent ModelInvocationRequest.

agent_execution is always required. A persistent-session request also requires persistent_sessions.

## Eligibility and ordering

Candidates are resolved deterministically:

1. Discover the canonical AgentProvider and require all requested effective capabilities.
2. Require an explicitly registered AgentRuntime beneath that provider.
3. Apply provider/runtime allowlists.
4. Apply residency, compliance, sandbox, network and cost constraints.
5. Query runtime health and reject unavailable runtimes.
6. Rank surviving candidates by explicit provider preference, explicit runtime preference, health degradation, then stable provider/runtime identifiers.

A degraded provider/runtime can remain eligible but sorts behind an otherwise equivalent healthy candidate. An unavailable provider/runtime is ineligible.

When fallback is disabled and an explicit first provider/runtime preference is supplied, codex-web does not silently select a different provider/runtime.

## Fail-closed fallback

Every fallback candidate must satisfy the same required capabilities, tenant scope, provider/runtime allowlists, residency/compliance constraints, sandbox/network requirements and cost ceiling as the preferred candidate. Fallback never broadens authority, resource scope, data scope, network access, sandbox permissions or budget.

If a cost ceiling is supplied but a runtime has no cost metadata, that runtime is rejected rather than assuming it is within budget. Likewise, a requested sandbox or network profile is rejected when the runtime has not explicitly declared support.

## Provenance and replay

A selected runtime records or exposes the AgentProvider ID and provider revision, AgentRuntime ID/type, runtime capability revision, effective capabilities, provider/runtime health at selection time, applicable runtime routing metadata, and deterministic routing reason.

Independent model routing retains the existing model-provider/model IDs, model/prompt versions, model-policy fingerprint and routing reason from ModelGatewayService.

Execution/session persistence must carry the selected runtime binding so later capability/provider changes cannot silently reinterpret an in-flight assignment. Downstream replay/evaluation work should pin the same routing provenance.

## Configuration and role defaults

Project/workspace/role preferences are configuration or definition inputs to the routing request, not a separate routing truth. Configuration may narrow or order choices, but it cannot grant authority or bypass provider/runtime capability checks. Role definitions may express preferred capabilities/providers/runtimes, but hard security constraints remain code/policy owned.

## UI impact

Issue #354 owns the operator surface. It should display requested capabilities, candidate providers/runtimes, the selected model versus selected execution runtime, health/degradation, fallback reason, and exact provider/runtime capability revisions. Credentials remain references only.
