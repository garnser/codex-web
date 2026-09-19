# Provider Capacity, Quota Failover, and Resume

## Purpose

Provider capacity is canonical operational state used by deterministic routing. It represents temporary provider/model/runtime availability caused by rate limits, token or usage allocation exhaustion, spend controls, and similar capacity constraints. It is distinct from commercial Entitlements: entitlements answer whether codex-web is permitted to use a capability; provider capacity answers whether an otherwise permitted provider can accept work now.

No model call is used to detect, rank, wait for, or resume provider capacity.

## Canonical state

Capacity records are tenant/workspace scoped and keyed by provider plus an optional runtime:

- provider-only records govern Model Gateway inference, for example `openai/*`;
- runtime-specific records govern execution agents, for example `openai/codex`;
- statuses are `available`, `throttled`, `depleted`, or `unavailable`;
- `retry_at` records a provider-supplied or bounded fallback reset/cooldown time;
- source and bounded metadata record how the observation was obtained without storing credentials or prompt content.

A runtime-specific capacity record does not implicitly disable every model API for that provider, and a provider-only model API record does not implicitly disable a separately authenticated execution runtime. This keeps API quotas, subscription-backed agent runtimes, and future provider channels independent unless they explicitly share a capacity source.

Expired throttled/depleted records become eligible again deterministically. A registered provider probe may re-check capacity before routing resumes.

## Failure normalization

Provider adapters normalize explicit capacity failures separately from generic transient failures.

- HTTP 429 or equivalent rate-limit signals become `throttled` unless the provider evidence indicates quota/usage/credit exhaustion.
- Explicit quota, usage-limit, credit, or token-allocation exhaustion becomes `depleted`.
- Provider reset metadata such as `Retry-After` and supported rate-limit reset headers is retained when available.
- If a provider supplies no reset time, codex-web applies a bounded cooldown rather than busy-looping.
- Network errors, timeouts, and ordinary 5xx failures remain generic transient failures unless provider evidence says they are capacity failures.

For Codex app-server, `account/rateLimits/read` is a deterministic zero-model-call capacity probe. `ordinaryUsageAllowed`, `rateLimitReachedType`, usage-window percentages, reset timestamps, and spend-control state are normalized into the canonical runtime capacity record.

## Model fallback

The Model Gateway keeps its existing deterministic route order and tenant policy bounds.

1. Apply model class, lifecycle, allowlist, capability, residency, compliance, context, and cost constraints.
2. Exclude statically eligible candidates whose provider capacity is currently blocking.
3. Invoke candidates in deterministic order, bounded by tenant `max_attempts` and request `allow_fallback`.
4. A capacity failure updates canonical capacity and permits the next eligible candidate.
5. A successful invocation marks that provider channel available.
6. If every attempted candidate is capacity constrained, return a capacity-specific unavailable error with the earliest known retry time.

Prompt or message content is not persisted for retries. Higher-level canonical work owns re-execution.

## Agent runtime fallback

Agent runtime routing applies the same principle after capability, policy, residency/compliance, sandbox/network, and cost checks.

A depleted preferred runtime is excluded and the next eligible runtime may be selected when fallback is allowed. New assignments can therefore route from Codex to Claude or another registered execution runtime when capability and policy constraints permit it.

Existing persistent sessions are not silently migrated between providers. Their provider-native session identity, isolated execution workspace, worker lease/fence, and approval context are canonical provenance. If the owning runtime becomes capacity constrained, the turn is parked and later resumed on that runtime rather than pretending another provider owns the same session.

## Durable waiting and resume

When no eligible capacity is available, canonical work may create a `ProviderCapacityWait` containing only identifiers, provider keys, reason, and retry time. It never stores prompt text or credentials.

The wait creates a one-shot durable scheduler record using trigger `provider-capacity.resume`. On firing:

- a queued thread is drained again;
- a Work Item is re-enqueued to its current canonical owner when a binding exists;
- otherwise the normal autonomous recovery/watchdog cycle is nudged.

Queued turns do not consume retry attempts while waiting for capacity. This prevents depleted providers from being hammered and preserves the original execution/work-item attribution.

## Operator surface

`GET /api/provider-capacity` exposes scoped capacity records and waits. The Developer Model Gateway and Agent Provider surfaces show capacity status, reset time, waiting work count, runtime capacity, routing rejection reasons, and fallback choice. Secret values are never returned.

## Invariants

1. Capacity routing is deterministic and uses zero LLM tokens.
2. Capacity cannot expand an allowlist, capability grant, residency/compliance boundary, sandbox/network profile, cost budget, or authority decision.
3. Fallback is bounded and can be disabled by canonical configuration/policy.
4. Provider/native session provenance is preserved; persistent sessions are not silently reassigned across runtimes.
5. Retry/reset state is durable; idle waits do not poll in a tight loop.
6. Provider success or an elapsed/probed reset can make a channel eligible again.
7. Prompt/message content and credentials are not persisted in the capacity ledger.
8. Capacity state remains separate from Entitlements/quota policy and from general provider health.