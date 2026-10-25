# Model gateway, registry, and prompt governance

## Status

Canonical model-gateway and prompt-governance foundation.

The model gateway owns provider/model identity and deterministic routing. Agent/role identity does not select provider-specific model names directly.

Workload routing uses stable semantic classes and versioned qualification state.
The dated concrete recommendation seed is a Definition Registry record; production
eligibility is recorded in Model Gateway evaluation profiles, qualification revisions,
and effective routing-definition revisions. Every mapped route and invocation records
the exact mapping and qualification revision. A discovered catalog model remains
ineligible until replay evidence satisfies the workload profile, and an independent
critic mappings record requested, minimum, and achieved independence. They may
degrade to a different model family, a different concrete model, or an explicitly
independent same-model run when policy permits.

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
- upstream provider family, execution runtime provider, access source, usage semantics, and health;
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
4. registered adapter, credential readiness, and provider health;
5. tenant provider/model and runtime-intersection allowlists;
6. required-catalog readiness, TTL, exact concrete-model membership, and runtime/access compatibility;
7. optional workload suitability;
8. required capabilities;
9. residency and compliance constraints;
10. context-window capacity;
11. cost ceiling;
12. provider-scoped workload qualification;
13. workload specificity, explicit preference, expected outcome efficiency, health, requested latency, usage semantics, and stable route priority.

If no candidate survives, routing fails before provider invocation.

Workload class, strict model pin, latency ordering, and lower-cost preference are
structured request metadata. A strict pin narrows the candidate set; it cannot
bypass tenant policy, lifecycle, capability, residency, compliance, context,
budget, provider availability, or credential-boundary checks. Workload-specific
models rank ahead of otherwise eligible generic models. Concrete workload
catalogs and mappings are mutable registry data rather than orchestration code.
By default, an active mapping includes newly qualified models for its workload
after all provider/runtime gates pass; explicit mapped IDs retain their ordering.
Operators may disable this expansion on a mapping revision when an exact closed
set is required.

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

Agent routing evaluates each eligible execution runtime against its declared
`model_provider_ids`. It selects a runtime/model pair only when Model Gateway finds
an eligible model inside that runtime's provider intersection. A globally known or
advertised model therefore cannot be paired with a runtime that cannot execute it.
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
- provider family, runtime provider, access source, and usage semantics;
- active candidate-set revision, provider eligibility, and rejected candidates;
- requested and achieved critic independence;
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
GET /api/model-gateway/provider-eligibility
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

Version `1.5` adds immutable monetary provenance to invocation attempts. New
records retain provider-reported currency and source, or the exact model version
and pricing fingerprint used for a codex-web calculation. Migration preserves a
legacy `actual_cost_usd` value but leaves its source and pricing revision unknown
because `1.4` did not retain enough evidence to reconstruct them safely.

Version `1.6` adds workload evaluation, provider-scoped qualification, and
versioned routing definitions. Version `1.7` adds provider family/runtime/access
semantics, health and required-catalog gates, explicit catalog execution
rejections, model-family identity, candidate-set and exclusion provenance, and
critic-independence policy. Migration gives legacy providers their existing ID
and adapter as family/runtime defaults and preserves prior invocation history
without inventing missing access or independence evidence.

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

## Restricted native entitlement reasoning

The opt-in `codex-native-reasoning` ModelProvider adapter uses the existing
ThreadService, TurnService, canonical worker/session manager, queued START
receipt and runtime usage owner. It is separate from ordinary operational Codex
sessions: a public invocation or an AgentProfile label cannot create its private
Executive reservation context. The Executive atomically reserves consultation
first; each role/synthesis has a stable logical invocation ID. That ID is bound
to the original queued attempt, while START, session and measured usage retain
the actual bootstrap assignment's physical execution ID. Unknown originals are
held by those existing owners and never authorize fallback or another input.

The deployment must explicitly compose `build_native_reasoning_components` and
`register_native_reasoning_components`, publish and pin a scratch/read-only
execution contract and exact AgentProfile, and supply current canonical Profile
and requester validators. These components do not activate a provider/Profile.
The configured native worker is the existing worker, not a second execution
plane. Its session has no operational broker and advertises no shell, filesystem,
git, MCP or approval capabilities. The actual isolated launch, lease, Profile,
worker, native version, restricted catalog/config and absence of an operational
broker are checked before inference. Public/native input still uses the existing
final worker/preflight guards; the internal context is not a source-prefix grant.

The pinned Codex 0.160.0 contract reduces one actual bundled model row: shell and
patch tools, experimental tools, search, apps/plugins/hooks and other operational
features are disabled. A fresh private Codex home mounts only the existing
sign-in owner's `auth.json` reference read-only and the reviewed restricted public
catalog. Credential bytes are never parsed/copied by the adapter, converted into
an API key or stored as another Secret. Ambient MCP configuration is not mounted;
an empty `mcp_servers={}` override alone is insufficient because native config
merges tables. Effective native config is read and validated before thread
startup. Server-origin tool/approval/auth-refresh requests are refused. An expired
sign-in requires the existing authentication owner; unknown inference is never
retried automatically. Real native tool-inventory/authentication qualification
is required before enabling this adapter; unit fixtures are not that proof.

Only this explicitly scoped native adapter can admit an unpriced
`CHATGPT_SUBSCRIPTION` / `ENTITLEMENT` route. API and unverified routes retain the
pricing-required budget guard. Entitlement USD cost remains null, not zero; token,
call, time and observed-byte budgets remain finite. Goal/Decision aggregate USD
is null when any included entitlement call has unattested USD, including mixed
priced usage. The individual priced attempts remain intact. The Decision UI
renders unknown entitlement USD, and ordinary priced deliberation refuses an
unknown aggregate rather than subtracting null as zero.

One inference is accepted only after an actual terminal turn and an attributed
canonical record with one measured call and input/output tokens. Native
`max_output_tokens` is not a qualified hard cap for this subscription protocol.
Observed token/output-byte limits interrupt and may overshoot before observation;
request timeout and the canonical process boundary remain finite. Cancellation
joins preparation and the existing exact process stop/reap owner, including
failures before thread-create returns. Missing/delayed/ambiguous usage, unexpected
tools, scope drift and unknown native outcomes do not become successful model
results. This contract does not claim a live consultation until the opt-in
composition, current authentication and actual bounded inference are qualified.

The invocation owns one monotonic deadline and cancellation witness shared by
its reservation guard, canonical queue preparation and the actual native stdin
writer. Waiting for a thread/turn response or writer lock does not preserve
permission after cancellation. The writer composes this check with the original
queued START guard, then writes only under the existing transport-generation
lock. Cancellation retains the original queue/preflight state and joins its
owned process; it does not classify a timed-out input as NOT-SENT. A configured
native adapter does not change projectless ordinary priced consultations: native
caller authorization is evaluated only if a scoped native route is selected.
Native completed commentary and final answers remain separate; only a bounded
completed final answer is returned as the model result.

Native reasoning startup uses the existing Configuration Registry, category
`Model & Provider`, with startup-only `model.native_reasoning.*` keys:
`enabled` (default false), `provider_id`, `model_id`, `concrete_model`,
`agent_profile_id`, `agent_profile_revision`, `execution_profile_id`, `worker_id`,
`catalog_path`, and `catalog_sha256`. Only explicit published deployment/global
values enable registration; an incomplete enabled set refuses startup. These
values select existing canonical objects and a reviewed, reduced public catalog;
they publish no provider, model, routing policy, Profile, permission or credential.
The actual existing worker ID and ambient credential-owner reference must match.
Changing/disabling any selected configuration record refuses further inference
until reviewed startup composition is repeated. Configuration inspection and
publication use the existing configuration UI/API, with ordinary canonical
provider/model/routing and AgentProfile/Definition administration alongside it.

The configured execution contract is an explicitly published scratch/none,
network-disabled, host-mutation-disabled, read-only contract, without broker,
Home or project coordination operations/targets. The Profile must be visible and
invokable by the actual consultation requester. A session uses only the existing
Codex sign-in authentication mode; API-key mode cannot silently replace it.
Before native thread bootstrap, application composition reuses the existing
coalesced Codex `account/read` metadata owner with `refreshToken: false` when
account evidence is absent or stale. This is a bounded request-time preparation,
not a model request, login, token refresh, idle poll or authentication substitute.
The original caller guard runs before metadata access and again afterward;
unchanged Thread/worker authentication preflight still refuses unavailable or
revoked sign-in evidence. Timeout and cancellation join the original metadata
owner before returning and cannot admit a late native thread.

Pre-start HTTP refusals retain their original public status, detail and cause.
The controller attaches only closed stage, exception class, HTTP status and
allowlisted refusal/blocker codes for native preparation diagnostics. Provider
errors after the start boundary cannot acquire this controller attribution.
`authentication_metadata` and `post_authentication_current_guard` distinguish
account preparation from a subsequent caller/context revalidation failure.
Existing operator diagnostic surfaces consume these events; no new UI control
or authority is introduced.

Current tenant, Profile revision, Definition, worker, empty physical workspace
and live resource lease are revalidated at preparation and final input. Existing
Skill discovery receives the exact `model.reasoning` operation and attested empty
tool capability sets: unrelated remote mutation procedures are unmatched, while
explicit missing/quarantined/version-skewed Skill pins and applicable procedures
still refuse. This is not an unconditional discovery bypass.

Native authorization rechecks the original canonical caller. Existing local
trusted identity, browser session expiry/revocation/assurance, service-token
metadata and membership remain owned by IdentityService. A credential-free broker
requester additionally requires its original canonical assignment-origin guard;
resolving membership never creates authentication or MFA. The existing Executive
consulting reservation and original broker custody remain current through the
same final native writer guard. Default-disabled composition is inert and makes
no model call. Actual subscription authentication, complete effective tool
restrictions, bounded usage and a genuine consultation still require qualified
runtime evidence before enabling this route.

The native bridge creates additional durable copies of the rendered request in
ordinary canonical ThreadHistory and the existing native worker rollout. It does
not have the HTTP adapter's hash-only prompt-metadata retention. The input is a
role-filtered subset of the retained Executive activation context, plus its
published role instructions; source IDs/citations remain in that rendering. The
logical Executive activation/role ID is retained by the original queued attempt,
and its START receipt binds the actual native session/turn and physical custody.
Existing Executive activation/revision reads are tenant-scoped; ThreadScope also
checks its canonical Project. An execution Profile is not an additional
transcript-read ACL. This component does not claim inherited source retention or
governed deletion of thread/native-rollout payloads: those domains have not yet
adopted the DataGovernance payload-handler interface. Existing governance actions
must continue to report an unavailable domain adapter as blocked, never deleted.
The planned lifecycle adoption is tracked separately in [#1747](https://github.com/garnser/codex-web/issues/1747).

Native inference does re-evaluate the existing DataGovernance context filter at
its current reservation and final writer edges. It uses exact governance IDs
retained in the original role-filtered business/memory context, including source
fact IDs' governance references; it does not reload a newer business value or
silently replace an original version. Missing legacy source provenance, a denied
source or classification drift refuses native input. Work-only context needs no
additional governed-source read. This current-input check is distinct from
inherited payload-retention/deletion enforcement.

### Queued START correlation compatibility

Ordinary `openai/codex` START/resume correlations retain exactly the existing
`1.0` representation. Restricted `openai/codex-reasoning` correlations use explicit
`1.1`; the version/runtime pairing is validated, with the same original queued
snapshot, worker/assignment/fence, guarded writer, reader evidence and receipt.
The capture owner additionally requires the private current invocation and its
exact original logical ID, requester and physical assignment owner. A runtime
label is insufficient. The result validates the exact runtime attribution too.
A positively completed and measured inference finishes/releases its ephemeral
scratch assignment through the existing canonical manager; unknown originals
remain held and are only stopped/joined.

Readers predating `1.1` reject these retained correlations visibly. No old
correlation is rewritten and no existing unknown is discarded. Deployment must
drain older writers before enabling the opt-in and declare the state migration
boundary. Once the first `1.1` correlation is durable, rollback to an older reader
is unavailable, even after the invocation completes, because canonical history
is retained. A release enabling this path must set `stateMigrationRequired=true`
and must not claim older-source rollback; a reviewed forward-compatible release
or explicit supported migration is required. Disabled registration before any
`1.1` write does not itself create such a correlation.

The restricted native transport allowlists protocol methods at both request and
actual writer boundaries. Unknown methods, steer, compact, goal, fork, command
execution and credential refresh cannot initiate unreserved inference. Input
writes additionally require the original private invocation, exact model,
workspace, native session and message digest, with the canonical START guard for
resume/turn input. Bootstrap is limited to one send for its owned process;
interrupt remains available for owned cleanup. These checks do not grant a
public thread operation permission merely because another consultation is live.
