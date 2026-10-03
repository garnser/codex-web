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
- whether read-only catalog discovery is enabled and its bounded cache TTL.

Model records contain:

- stable model ID and concrete provider model name/version;
- one or more stable model classes;
- optional workload suitability classes used for deterministic task-aware routing;
- capabilities/modalities/tool support;
- context and output limits;
- latency class;
- pricing metadata;
- residency/compliance tags;
- routing priority and lifecycle state.
- explicit availability source (`static` or provider-discovered) and optional
  upstream provider/model identity for aggregator catalogs.

Provider catalog discovery is provider-scoped canonical state. A refresh uses the
registered adapter and any credential reference through the SecretBroker; raw
credentials and arbitrary provider response fields are never persisted or returned.
Snapshots expose a content revision, discovered/expiry timestamps, normalized model
identities and explicit ready/stale/error state. A discovered model definition is
eligible only while its provider snapshot is ready, unexpired and still contains the
exact concrete model. Failed refreshes retain the prior entries for inspection but
mark the snapshot error, so routing fails closed. Static definitions remain an
explicit fallback mode and are never presented as discovered availability.

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
2. an optional strict model pin and the stable model class;
3. active model/provider lifecycle;
4. tenant provider/model allowlists;
5. discovered-catalog readiness, TTL and exact concrete-model membership where required;
6. optional workload suitability;
7. required capabilities;
8. residency and compliance constraints;
9. context-window capacity;
10. cost ceiling;
11. workload specificity, preferred provider, provider health, requested latency order, requested lower estimated cost, and route priority.

If no candidate survives, routing fails before provider invocation.

Workload class, strict model pin, latency ordering, and lower-cost preference are
structured request metadata. A strict pin narrows the candidate set; it cannot
bypass tenant policy, lifecycle, capability, residency, compliance, context,
budget, provider availability, or credential-boundary checks. Workload-specific
models rank ahead of otherwise eligible generic models. Concrete workload
catalogs and mappings are mutable registry data rather than orchestration code.

## Effective request precedence

The shared Agent routing path builds one effective `ModelInvocationRequest` in
this order:

1. explicit workflow or turn preferences;
2. the exact Agent Profile revision selected for the execution;
3. typed Configuration resolved at project, workspace, organization, then global scope;
4. request and registry defaults.

Singular preferences such as workload class and a strict model pin use the first
non-empty value. Ordered provider and latency preferences retain higher-precedence
entries first and remove duplicates. Cost ceilings use the lowest supplied value,
and fallback is allowed only when every applicable layer allows it. Tenant model
policy, model lifecycle, availability, capability, residency and compliance remain
hard filters after preferences are resolved; no preference layer grants authority
or broadens a constraint.

Scoped model defaults use the existing typed Configuration Registry keys under
`model.routing.*`. Agent Profile defaults remain part of the versioned profile
revision. Workflow and turn callers pass structured request fields rather than
creating another settings store. The route result returns exact Configuration
record scope/revision provenance and the Agent Profile execution binding so an
operator can explain the effective choice.

Agent Profile state version `1.1` adds workload, strict pin, latency, cost and
fallback model defaults. Migration from `1.0` supplies automatic-selection
defaults, preserving the effective behavior of every existing profile revision.

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
- workload class, any strict model pin, and latency/cost preferences;
- exact prompt template ID/version/checksum;
- rendered prompt hash, message count and character count;
- required capabilities/residency/compliance constraints;
- effective cost ceiling;
- work/goal/decision/execution references;
- ordered provider/model attempts;
- exact selected provider, model, concrete model name and model version;
- selected upstream provider/model and catalog revision/discovery time where applicable;
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

GET /api/model-gateway/catalogs
POST /api/model-gateway/providers/{provider_id}/catalog/refresh

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

The operator route preview exposes workload, pin, latency, and cost preferences
through the same canonical route API used by runtime callers. Preview remains
deterministic and performs no provider invocation.

Refreshing the preview catalog retains an explicit model pin. If the model has
disappeared, the picker marks it unavailable and keeps the pin so the route API
fails closed; only an explicit operator choice restores automatic selection.

Gateway state version `1.3` added workload suitability and request preference
provenance. Migration from `1.2` supplies empty workload classes and unpinned,
no-preference defaults, retaining legacy routing order. Version `1.4` adds
provider-scoped discovered catalog snapshots and migrates older providers to
explicit static availability. Older readers must not interpret either state as
an older schema. Workload values remain canonical model registry data (not a new
hard-coded workload catalog); request preferences do not grant authority or relax
tenant policy.

## Provider binding administration

Operations' Provider Binding Management edits existing ModelGateway binding
metadata through the existing provider PUT endpoint, independently from any
AgentProvider record or synthesized view. Active, degraded and disabled statuses
are local routing state; they do not change remote accounts, plans or credentials.
Adapter registration is deployment-owned. Registry edits make no provider call and
add no LLM usage. Credential inputs remain SecretReference IDs only.

`GET /api/model-gateway/provider-administration` exposes the typed schema and
administrator/MFA or service-scope permission. `GET /api/model-gateway/providers/{id}/impact`
returns tenant-filtered model, policy, AgentProvider, profile and retained invocation
references. Each inventory has a 5,000-record scan bound and 100-row display bound;
unavailable or invalid dependencies fail visibly. Invocation projection includes
only ID/status, not prompt data or private invocation payloads. Arbitrary future
request preferences are not enumerable and the preview is not a consumer lock.

The optional `expected_revision` on provider PUT is a SHA-256 fingerprint of the
current canonical binding, checked inside the same mutation transaction. `none`
means create-if-absent. It is a concurrency token, not a model version or immutable
revision-history claim. Existing API callers remain compatible when omitting it;
UI writes always supply the reviewed fingerprint. Stale writes return 409 without
replacing current configuration. No stored contract migration is needed.

Both provider administration APIs validate optional Project view context through
the canonical Project boundary. Bindings remain workspace-scoped and inherited,
not Project-specific overrides. Disabling prevents subsequent selection, including
linked AgentProvider eligibility, but does not cancel already-accepted invocations
or delete model definitions. The remote provider's own management surface remains
the authority for account lifecycle.
