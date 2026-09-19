# Organizational Memory and Governed Retrieval

Milestone 10 introduces durable organizational memory as a retrieval layer over canonical company knowledge. Memory is not chat history and it is never allowed to silently replace current Goals, Decisions, policies, resources, or other canonical state.

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

Memory reuses M3 `DataGovernanceService`; it does not implement its own privacy/retention policy.

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

## Bounded hybrid retrieval

Retrieval is deterministic and model-free. The initial implementation combines:

- structured filters (type, project, logical key, tags, relationship target, author/role metadata)
- lexical overlap
- a small local deterministic concept/feature hash encoder for semantic similarity.

The local encoder is intentionally an implementation detail, not canonical truth. It can later be replaced by a pluggable embedding backend without changing the durable Knowledge contract or retrieval provenance.

Every query has explicit limits:

- Top-K result count
- candidate ceiling
- maximum packed context tokens
- optional progressive retrieval.

Candidates are authority- and governance-filtered before their content can enter a result. Freshness penalties are explicit. Context excerpts are truncated to the remaining token budget; retrieval never silently over-packs the requested budget.

## Retrieval provenance

Each search writes a metadata-only `KnowledgeRetrievalRun` containing:

- tenant/workspace and actor
- SHA-256 of the query/request rather than raw query text
- structured filters and retrieval limits
- selected Knowledge IDs
- selected scores and freshness states
- denied Knowledge IDs with machine-readable reasons
- candidate count and packed-token total.

Raw query text and returned memory content are intentionally not duplicated into retrieval audit state. Operators can therefore inspect what influenced reasoning without creating another sensitive-content store.

## Canonical-vs-memory rule

Memory may cite or summarize canonical state, but it never wins a conflict with the current canonical object. Callers must use the canonical Goal/Decision/Policy/Resource/etc. service when current operational truth is required.

Examples:

- an old architecture Decision retained for historical context is returned as superseded, never as the current Decision
- an expired runbook may be inspected only when stale history is explicitly requested
- a remembered policy cannot grant authority; Role/Authority definitions are resolved canonically at action time
- remembered credentials are forbidden by governance and may not enter model context.

## M10 follow-up

Issue #118 builds ingestion adapters, verified reusable procedures, the Memory workspace/retrieval inspector, and integration that retrieves bounded relevant memory before Executive reasoning or major technical changes. Those consumers must use this service and its retrieval-run provenance rather than performing full-history replay or adding a second memory store.
