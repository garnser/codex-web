# Code-host provider boundary

## Status

Milestone 3 foundation for issue #373.

`CodeHostProvider` is the provider-neutral read/discovery boundary for source-control hosts such as GitLab and GitHub. It is deliberately separate from `TaskSource`: a repository host can provide code facts without being the authoritative task system for a project.

## Canonical identity

Repositories remain canonical `Resource` objects of type `repository`.

Provider identities are references and provenance:

- `Resource.id` is the stable codex-web identity used by policy, authority, work, evidence, and project bindings.
- `Resource.provenance.external_id` records the provider's stable repository/project identity when available.
- provider aliases record current provider locators such as `owner/repository` or `group/project`.
- provider URL/name changes do not replace the canonical Resource ID.
- code-host facts never grant authority merely because a provider reports ownership, membership, approval, or repository permissions.

This separation is important for providers such as GitHub, where the stable numeric repository ID is not the REST locator used for repository reads.

## Provider binding

A `CodeHostProviderBinding` identifies tenant/workspace, provider type and instance, API base URL, declared capabilities, and an optional canonical SecretBroker `credential_ref`.

Raw credentials are not configuration and are never stored in bindings. `CodeHostService` resolves secret material only around the bounded adapter call and passes it directly to the provider implementation.

Bindings are runtime/provider configuration rather than a second canonical repository catalog. Extension lifecycle/configuration from #169 may register bindings and adapters; canonical repository identity remains in the Resource catalog from #133.

## Capability contract

Capabilities are explicit and fail closed: repository metadata, refs, commits, pull/merge requests, reviews/approvals, checks/statuses/pipelines, releases, compare/diff facts, and webhook normalization.

A binding cannot declare a capability the adapter does not implement. Calling an unavailable capability raises `CodeHostUnsupportedCapabilityError`; providers must not silently approximate unsupported behavior.

Provider-specific SDK objects and terminology remain inside adapters. Callers receive canonical fact models such as `CodeHostRepositoryFact`, `CodeHostPullRequestFact`, and `CodeHostCheckFact`.

## Read-only by construction

`CodeHostService` exposes read/discovery and webhook-normalization operations only.

It intentionally does **not** expose create-branch, open-PR/MR, merge, comment, approve, tag, release, or other mutation methods. Consequential external mutations must continue through canonical `ActionIntent` / `ActionProvider` handling so authority, approval, idempotency, reconciliation, provider receipts, and evidence remain enforceable.

A code-host extension may register both a `CodeHostProvider` and corresponding `ActionProvider` capabilities, but those are distinct boundaries. Installing/enabling a read adapter does not grant mutation authority.

## Canonical events

Provider webhooks are normalized into `CodeHostWebhookFact` before orchestration. The fact identifies provider instance, durable delivery/event identity, canonical event type, repository/subject external identities, action/state, and bounded provider metadata.

`CodeHostService.ingest_webhook()` sends normalized facts through `CanonicalEventIngestionService` with a deterministic provider-instance/event-id idempotency key. Replayed deliveries therefore resolve to the same canonical event instead of creating duplicate work.

The existing GitLab webhook path continues to use `TaskSource` normalization for authoritative issue/task events. Non-task source-control events use `GitLabCodeHostProvider` for shared pull-request, pipeline/check, deployment, incident, and failure normalization.

## Reference adapters

The initial built-in adapters are `GitLabCodeHostProvider`, backed by the existing GitLab HTTP client, and `GitHubCodeHostProvider`, backed by a small GitHub REST read transport.

Both normalize overlapping repository, ref, commit, pull/merge request, review/approval, check/status, release, compare, and webhook concepts into the same canonical contract. Provider-specific differences remain explicit; absence of a claimed capability is an error rather than an implicit fallback.

Future Bitbucket and Azure DevOps adapters should implement the same protocol without changing Goal, Decision, Work, Resource, or Evidence schemas.

## Tenant and secret isolation

`CodeHostRegistry.resolve()` enforces binding tenant/workspace scope. `CodeHostService` separately resolves the requested canonical Resource through the caller's tenant-scoped Resource catalog and checks provider/provider-instance provenance when present.

Knowing an external repository ID, provider URL, object key, or provider-side permission cannot bypass tenant isolation or codex-web authority.

## UI impact

No provider-specific settings island should be created. The shared Integrations/Providers and Resource workspaces tracked by #125/#127 should expose provider instance/health, declared/effective capabilities, canonical Resource binding/provenance, secret reference metadata (never values), webhook/event provenance, reconciliation state, and explicit unsupported/denied/degraded states.

Source-control mutations shown in the UI must still create canonical ActionIntents and display their authority/approval/result evidence rather than invoking a code-host read adapter directly.
