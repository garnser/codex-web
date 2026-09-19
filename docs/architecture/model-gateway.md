# Model gateway, registry, and prompt governance

## Status

Canonical model-gateway and prompt-governance foundation.

The model gateway owns provider/model identity and deterministic routing. Agent/role identity does not select provider-specific model names directly.

## Stable model classes

Core orchestration should request a stable class/capability such as:

- `lightweight`
- `primary-coding`
- `high-reasoning`
- `strategic`

Concrete provider/model mappings live in the canonical registry and may change without rewriting orchestration logic.

Explicit provider-specific model strings remain a migration/compatibility surface until existing Codex project/thread settings are migrated.

## Provider and model registry

Provider records contain metadata only:

- provider ID and adapter type;
- base URL where applicable;
- secret-broker credential reference, never credential material;
- residency/compliance tags;
- availability state.

Model records contain:

- stable model ID and concrete provider model name/version;
- one or more stable model classes;
- capabilities/modalities/tool support;
- context and output limits;
- latency class;
- pricing metadata;
- residency/compliance tags;
- routing priority and lifecycle state.

## Tenant routing policy

Tenant/workspace policy may restrict:

- allowed providers;
- allowed models;
- required residency tags;
- required compliance tags;
- maximum invocation cost;
- maximum bounded attempts including fallback.

A policy can restrict routing but does not grant agent/action authority.

## Deterministic routing

Routing filters candidates before invocation in this order:

1. exact tenant/workspace scope;
2. stable model class;
3. active model/provider lifecycle;
4. tenant provider/model allowlists;
5. required capabilities;
6. residency and compliance constraints;
7. context-window capacity;
8. cost ceiling;
9. preferred provider, provider health, and route priority.

If no candidate survives, routing fails before provider invocation.

Fallback is bounded and only follows transient provider failures. Every fallback candidate is independently subjected to the same policy/residency/capability/budget constraints. The gateway conservatively charges the estimated upper-bound cost against the remaining fallback budget after an uncertain transient attempt so fallback cannot silently expand the configured budget.

## Prompt/version governance

Prompt templates are versioned canonical records with a SHA-256 checksum.

Invocation requests pin an explicit template version or resolve the current active version before routing. Invocation audit records contain the template ID/version/checksum plus a hash of the rendered prompt/messages, but never the prompt/message body.

The initial gateway treats template content as an administration asset. The Definition Registry may become the shared publication mechanism for prompt/template definitions later; this gateway remains the runtime resolver/enforcer and invocation-attribution owner.

## Credentials

Provider registry records may store a `credential_ref` only. When present, the model gateway resolves it through the canonical SecretBroker at the bounded provider-call boundary. Raw provider credentials are never returned through model-gateway APIs or persisted in model registry/invocation state.

Credential-less local providers such as a protected local Ollama endpoint may explicitly set `credential_required=false`.

## Invocation attribution

Every gateway invocation records metadata sufficient for audit/cost/replay attribution:

- tenant/workspace and acting identity;
- model class and purpose;
- exact prompt template ID/version/checksum;
- rendered prompt hash, message count and character count;
- required capabilities/residency/compliance constraints;
- effective cost ceiling;
- work/goal/decision/execution references;
- ordered provider/model attempts;
- exact selected provider, model, concrete model name and model version;
- provider request ID and provider stop reason when available;
- token usage and computed cost when available;
- success/failure timestamps.

Prompt text, user messages, model output and secret values are deliberately excluded from this ledger.

## Adapter boundary

`ModelProviderAdapter` is provider-neutral. The built-in adapters support:

- native OpenAI Responses API via `OpenAIModelProviderAdapter`;
- OpenAI-compatible chat-completions endpoints and Ollama through the same OpenAI adapter;
- native Anthropic Messages API via `AnthropicModelProviderAdapter`.

The Anthropic adapter maps canonical system prompts, user/assistant messages, output bounds, reasoning effort, provider request identity, stop reason and token usage without exposing Anthropic request objects to orchestration code. Provider/model names remain registry data rather than role logic. Unsupported canonical semantics fail explicitly instead of being silently dropped.

Provider errors are classified into transient vs terminal failures so fallback remains explicit and bounded.

## API

```text
GET /api/model-gateway/providers
PUT /api/model-gateway/providers/{provider_id}

GET /api/model-gateway/models
PUT /api/model-gateway/models/{model_id}

GET /api/model-gateway/prompts
PUT /api/model-gateway/prompts/{template_id}/{version}

GET|PUT /api/model-gateway/policy
POST /api/model-gateway/route
GET /api/model-gateway/invocations
```

There is intentionally no generic browser/API `invoke` endpoint in this foundation. Product/runtime services invoke models through the in-process gateway after their own authorization/context decisions; exposing a generic inference proxy would create a new unowned authority and abuse surface.

## Executive adoption

Open Executive advisory/board reasoning now consumes the gateway rather than choosing a provider directly in the production application composition.

- ordinary advice and board specialist/synthesis calls request the stable `strategic` model class;
- context compaction requests `lightweight`;
- the authenticated request actor supplies tenant/workspace scope to gateway routing;
- API responses retain the legacy `model` field for compatibility and additionally expose `model_class` plus exact `model_invocation_ids`;
- every referenced invocation resolves exact provider/model/template/policy attribution through the gateway ledger;
- successful provider token/cost usage is emitted into the canonical entitlement/usage ledger;
- prompt, conversation, response and credential bodies are not copied into model-invocation or metering records.

For existing self-hosted installations, Executive performs a compatibility bootstrap only when no strategic model mapping exists. It converts the current Executive provider/model environment defaults into ordinary tenant-scoped gateway registry records. Native OpenAI's standard `OPENAI_API_KEY` environment behavior remains a compatibility credential path; canonical hosted/provider administration should use SecretBroker `credential_ref` records.

This compatibility bootstrap is intentionally subordinate to canonical registry state: once an administrator supplies a strategic mapping, Executive no longer chooses a provider/model from its legacy environment settings.

## Adoption path

1. Compose the gateway and reference adapters at application startup.
2. Seed/migrate concrete providers/models/templates through canonical administration, never hard-coded runtime branches.
3. Migrate Executive reasoning to `strategic` / `high-reasoning` classes.
4. Migrate Codex turn defaults to `primary-coding` while preserving explicit user/project model overrides during transition.
5. Attach model invocation IDs to Work Items/Goals/Decisions/audit/evaluation.
6. Feed token/cost results into canonical entitlement and usage metering and future budget policy.

UI administration belongs to the platform administration and cross-cutting operator workspaces and must display provider/model capabilities, exact routing constraints and provenance without exposing credentials.
