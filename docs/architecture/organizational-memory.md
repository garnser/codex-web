# Organizational Memory and Governed Retrieval

Organizational Memory provides a durable retrieval layer over canonical company knowledge. Memory is not chat history and it is never allowed to silently replace current Goals, Decisions, policies, resources, or other canonical state.

## Canonical knowledge records

A `KnowledgeRecord` is tenant/workspace scoped and versioned by a stable logical key. Supported object types cover architecture decisions, policies, products, repositories, services, incidents/postmortems, authorized customer/account context, projects, Goals, Decisions, reusable procedures, and other governed knowledge.

Every version records:

- title, summary, bounded durable content, tags, and optional project scope
- exact provenance including source kind/ref/URL/revision, authorship/observation timestamps, Evidence IDs, and source governance records
- references to canonical objects rather than copied ownership/authority state
- typed relationships to other knowledge objects
- effective data classification and its canonical Data Governance record
- retention action/deadline, model-context deny flag, and optional operational-role requirement
- review/validity dates used to calculate explicit freshness
- lifecycle (`current`, `superseded`, `invalid`, `redacted`, `deleted`)
- content digest, previous/superseding version references, actor and timestamps.

Revising a logical key creates a new immutable version and marks the former current version `superseded`. Retrieval excludes superseded, invalid, stale, expired, redacted, and deleted material by default. Historical inclusion must be explicitly requested, and returned items retain their non-current freshness label.

## Governance and privacy

Memory reuses the canonical `DataGovernanceService`; it does not implement its own privacy/retention policy.

When a knowledge version is created or revised, a `DataCategory.MEMORY` governance record is registered. Classification, retention, residency and `deny_model_context` constraints can only become more restrictive through source-governance inheritance.

Before content is returned for reasoning context, retrieval calls the canonical governance context filter. It deterministically denies:

- non-active governance records
- credentials
- Secret-classified records even when the caller requests a Secret maximum
- records that explicitly deny model context
- classifications above the caller/query context ceiling.

Governance redaction/deletion invokes the memory domain handler. The handler removes durable content, semantic-index material, tags/references and relationships for the target and for memory versions derived through that governance provenance. This prevents a derived summary from silently surviving source deletion.

Legal holds and retention scheduling remain owned by Data Governance. Memory only supplies the domain payload-action adapter.

## Authority and tenant boundaries

Every read candidate is tenant/workspace scoped before ranking. Cross-tenant IDs behave as unavailable.

Memory reads require the canonical `memory.read` authority capability at READ level for the relevant project scope. Writes/revisions/invalidation require `memory.write` at EXECUTE level. A record may additionally require one or more live operational Role IDs; retrieval fails closed if the actor does not hold a qualifying role.

The HTTP surface adds service-scope checks (`memory:read`, `memory:write`, `memory:admin`) and step-up assurance for human mutations, but those checks supplement rather than replace canonical Role authority.

## Pluggable derived retrieval

Retrieval is deterministic and does not use an LLM to choose search results. Canonical `KnowledgeRecord` objects remain authoritative; search indexes, vectors, and embeddings are derived/rebuildable infrastructure.

The code-owned `RetrievalBackend` contract supports:

- lexical and/or vector search
- structured tenant/workspace/project/type/lifecycle filters
- an explicit canonical-ID allowlist supplied by the memory service after authority/governance filtering
- bounded result counts
- canonical source ID/version/content digest on every hit
- backend health, capabilities, and index revision
- upsert, delete-by-canonical-ID, and complete rebuild.

The reference implementations are:

- `LocalLexicalRetrievalBackend` — local deterministic lexical indexing with no embedding dependency
- `LocalVectorRetrievalBackend` — local hybrid lexical/vector retrieval through the provider-neutral `EmbeddingProvider` contract.

A backend never receives authority to decide which memory the caller may access. Tenant, Role/authority, lifecycle, classification, retention, and model-context checks remain in `OrganizationalMemoryService`; the resulting allowed canonical IDs are passed to the backend. This means replacing a backend cannot widen the caller's memory scope.

Every query has explicit limits:

- Top-K result count
- candidate ceiling
- maximum packed context tokens
- optional progressive retrieval.

Candidates are authority- and governance-filtered before their content can enter a result. Freshness penalties are explicit. Context excerpts are truncated to the remaining token budget; retrieval never silently over-packs the requested budget.

## Embedding identity, rebuilds, and data egress

Embedding providers carry versioned provider/model identity, model revision, dimensions, capability revision, locality, and residency tags. This uses the same provider/model identity vocabulary as the canonical model layer without creating a second durable provider registry.

Non-local identities are validated against the active canonical ModelGateway provider/model registry before indexing: the provider and model must exist and be active, the model must declare the `embedding` capability and matching dimensions/revision, and the declared embedding residency must be represented by canonical provider/model configuration.

The default provider is offline/local and deterministic. Changing its model revision makes the vector index unhealthy until it is rebuilt, preventing incompatible vectors from being silently mixed.

For an external embedding provider, indexing is fail-closed:

- startup does not automatically export memory to the provider
- an authenticated administrator must explicitly rebuild the index
- every record passes canonical model-context filtering, so Secret data and `deny_model_context` records are excluded
- every record also passes canonical export authorization
- governed residency tags must be a subset of the embedding provider's declared residency tags
- denied records are removed from the derived index rather than retained as stale vectors.

The Memory API exposes `GET /api/memory/index` for backend/index/embedding health and revision, and `POST /api/memory/index/rebuild` for an explicitly authorized rebuild. Loss or corruption of derived index state is therefore repairable from canonical memory without changing Knowledge IDs or provenance.

## Retrieval provenance

### Research origin

Memory state schema `1.1` adds origin metadata to `KnowledgeProvenance`:
`human_observed`, `secondary_research`, `ai_inference`, `mixed`, and `unknown`.
Schema `1.0` records decode through the canonical migration registry with unknown
origin and unreviewed status; authorship, source kind, or an observation timestamp
cannot establish participant research. Older engines refuse the new state version.

Origin and review status are descriptive metadata, never independent verification
or authority to satisfy an evidence gate. Reviewing an AI hypothesis preserves
its AI origin. Derivation through canonical `derived_from` relationships records
exact source Knowledge IDs, versions, source references/revisions, and origins.
Different source origins produce `mixed`; summaries retain source-level
distinctions. Revision retains prior origin attribution rather than erasing it
through a new provenance label. Caller-supplied origin snapshots must match the
canonical sources. Sources pass existing tenant and memory-read checks.

Derived records inherit source governance references, including classification,
retention and deletion constraints, through the existing governance boundary.
Governed redaction/deletion removes derived origin snapshots as well as summary
content. Retrieval includes origin and source citations within its existing token
budget and returns the structured provenance alongside excerpts. The Memory
inspector shows origin, review status, and source-version attribution. No extra
model calls, participant telemetry, or separate research store are introduced.

Derived research freshness also checks the lifecycle and deadlines of exact cited
source versions. An invalid, redacted, deleted, missing or foreign source makes
the derived finding invalid for normal retrieval; superseded sources make it
stale, and source validity/retention expiry makes it expired. Explicit historical
queries retain those labels. A newer source version is never silently substituted.
When a record explicitly cites a newer version with historical attribution, the
newest cited version of each source logical key determines source freshness.
Historical attribution within the finding's own revision lineage does not make
its current revision stale. Canonical source lifecycle and historical origins
remain unchanged; the derived freshness is calculated using one retrieval-state
snapshot before ranking or context packing.

Each search writes a metadata-only `KnowledgeRetrievalRun` containing:

- tenant/workspace and actor
- SHA-256 of the query/request rather than raw query text
- structured filters and retrieval limits
- selected Knowledge IDs
- selected scores and freshness states
- denied Knowledge IDs with machine-readable reasons
- candidate count and packed-token total
- retrieval backend ID/index revision
- embedding provider/model/revision where vector retrieval participated.

Raw query text and returned memory content are intentionally not duplicated into retrieval audit state. Operators can therefore inspect what influenced reasoning without creating another sensitive-content store.

## Canonical-vs-memory rule

Memory may cite or summarize canonical state, but it never wins a conflict with the current canonical object. Callers must use the canonical Goal/Decision/Policy/Resource/etc. service when current operational truth is required.

Examples:

- an old architecture Decision retained for historical context is returned as superseded, never as the current Decision
- an expired runbook may be inspected only when stale history is explicitly requested
- a remembered policy cannot grant authority; Role/Authority definitions are resolved canonically at action time
- remembered credentials are forbidden by governance and may not enter model context.

## Ingestion, reusable procedures, and reasoning integration

Authorized source adapters normalize repository, pull-request, issue, architecture-decision, and other provider snapshots into `KnowledgeCreate` records and submit them through the batch ingestion boundary. The memory service owns deterministic idempotency: a retry with the same logical key, content, governance metadata, and source revision is unchanged; a changed source snapshot becomes a new canonical version and explicitly supersedes the prior version.

Verified recurring solutions can be promoted into `PROCEDURE` memory only with explicit Evidence IDs and source Knowledge IDs. The promoted procedure records typed `derived_from` relationships plus source governance provenance. It inherits the strongest source classification, earliest source retention expiry, delete semantics, model-context denial, and operational-role restrictions. Promotion creates reusable governed knowledge; it does not grant execution authority or bypass ActionIntent/Approval policy.

Executive reasoning retrieves memory before model invocation. Both the compatibility Executive chat path and canonical Executive Management use the same `OrganizationalMemoryService.search` boundary with bounded Top-K/candidate/context budgets. Canonical Executive activations retain the exact retrieval-run ID, while prompt context carries exact `[memory:<id>@v<version>]` citations. Retrieval does not add a model call.

The Memory workspace is an inspectable operator surface over canonical knowledge and retrieval provenance. It exposes scope, type, lifecycle/freshness, classification/retention, provenance, versions, relationships, derived backend/index/embedding revision, selected/denied candidates, scores, reasons, and packed token budgets. The UI explicitly labels the index as derived/rebuildable state rather than canonical truth and remains usable at phone viewport widths.

The legacy `/api/executive/knowledge` compatibility surface routes to Organizational Memory when the composed application is running. It no longer creates a second Executive reasoning source; standalone compatibility tests may still instantiate the legacy store when Organizational Memory is intentionally absent.

## Typed UX context briefs

Memory state schema `1.2` introduces the `ux_context_brief` Knowledge type.
Migration from `1.1` preserves existing records, content digests, source attribution,
relationships and lifecycle. Older engines refuse the new state version; rollout
requires the canonical migration and backup/restore qualification, and an older
reader is not a supported rollback target.

Brief content uses the bounded `ux-context-brief/1.0` format: target users,
observed needs, journeys, design constraints, approved copy examples,
uncertainties and evaluation criteria. Each section carries optional exact
Knowledge/Artifact/Evidence source pins. Knowledge pins use immutable record ID,
version and content digest. Artifact pins also retain the declared external
revision; Artifact and Evidence pins fingerprint the exact canonical record,
so a revision label alone cannot conceal changed content or lifecycle. Negative
Evidence outcomes remain inspectable source context rather than being treated
as invalid source records.

All Memory create, revise and ingest entry points validate brief content and
resolve source pins through canonical services. They inherit source governance
and derive Knowledge provenance through existing relationships. Source Project,
tenancy and read authority are checked before curation. Ordinary reads, lists
and retrieval also enforce source access; source changes, invalidation, deletion,
supersession and expiry propagate to brief freshness and dependent memory.
Context filtering rechecks current source-governance ancestry, including
constraints tightened after curation. Recursive source checks are bounded and
fail closed for cycles or unavailable sources.

Project-bound brief views live within `/api/memory/projects/{project_id}/ux-briefs`
and reuse Memory reader/writer scopes, authority, tenant checks and human mutation
step-up. The Memory dialog includes a guided authoring/revision editor. Source
updates require explicit inspection and repinning; a save does not silently
substitute new source revisions. Drafts remain in memory for their original
Project, and denied or failed saves retain their fields.

A core-owned context projection can request exact Knowledge IDs and selected
sections through the existing retrieval pipeline. The engine checks exact ID,
version and digest after authority/governance filtering and before projection.
It records bounded section/source pins in the existing metadata-only retrieval
run. Source review dates, origin/review labels and outcome metadata accompany
selected references. Complete sections and their attribution either fit the
context budget or are omitted with an explicit reason; projection output is not
silently cut through a source citation. Selection avoids injecting unrelated
source lineage or other brief sections. Selection and freshness consume no model
calls. A preview/retrieval audit is not proof of provider delivery or a trusted
execution continuation checkpoint.

The optional chat composer picker sends `TurnCreate.memory_context_selection`.
The server binds exact section/source pins, the Project/tenant and original
principal to a context digest. Queued and active turns persist this bounded
binding, without source prose or credentials. Legacy turns omit the absent
field, preserving their queue/preflight snapshot digests. Delayed admission and
recovery resolve current canonical identity membership and, for a service, its
current unrevoked/unexpired token grant. Native admission previews the exact
selection again after worker/bootstrap awaits. Missing permissions, changed
sources, omissions, changed context or scope refuse delivery with
`memory_context_denied`; the engine never substitutes a newer brief.

Selected context is task data in the existing user message. It does not grant
execution authority or alter developer instructions. Only a runtime start
response containing a native turn ID records `filters.native_delivery` on the
existing Memory retrieval audit. That receipt contains exact selected pins via
`context_selection`, context/message digests, Thread/execution and native
provider/runtime attribution, without copied source text. An ambiguous start
has no proven receipt. An audit write failure after provider acceptance emits
metadata-only telemetry and never retries the accepted native start. The Memory
retrieval inspector distinguishes accepted delivery from preview/retrieval.

The guided editor and picker link exact canonical Knowledge/Artifact/Evidence
records, preserve scoped in-memory drafts on failure and use no model calls or
browser persistent storage. Product/user facts stay in Memory; reusable task
instructions stay in versioned Skills. Ordinary free-text requests and CARE
composition can use the same optional selection.
