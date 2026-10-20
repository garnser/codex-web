# Code-host provider boundary

GitLab artifact events keep their normal MR/pipeline projection. Separately,
verified publication lineage can route a terminal notification to its original
Work Item and current validation/action owner without applying a pipeline's
terminal stage to that original. The authenticated configured Project/repository
mapping, Resource provenance and unique enabled ActionProvider binding bridge a
proxy webhook endpoint to the public provider identity; hostname guessing and MR
body links are not authority. Missing/revoked configuration refuses a claimed
original. Unpublished/read-only branches retain existing artifact routing and do
not require a mutation-provider binding. A repair in a new execution can reuse
an existing MR only when the verified branch publication explicitly attests the
same MR number and original issue. That newer verified publication may supersede
an older TaskSource head snapshot without rewriting it; a newer conflicting
snapshot still refuses routing.

New canonical notifications preserve exact head and publication-binding identity
in bounded provider metadata. Adapter-only redelivery compatibility ignores
new optional metadata only when it was absent from an otherwise exactly matching
immutable historical event. Its original checksum remains unchanged and no
subscriber runs; changes to previously recorded core facts still conflict.
Historical events are never rewritten to add an inferred SHA or provider proof.

Pending/running CI remains deterministic with no native turn or implementation
handback. Terminal success reaches existing exact-head validation; failure does
not invent a code/runner diagnosis, and existing evidenced owner-disposition
policy remains required. Accepted historical handoffs are preserved and can carry
an independent ProviderNotificationPin; an active pending handoff retains its
separate ownership protocol. Pinned notifications enter the existing durable
queue even when idle, with ordinary profile/scope/serial-admission checks.
Global pause and every independent review, security, merge and release gate
remain effective. No polling or synthetic terminal event is added.

UI impact uses existing Work Item, queue/native, canonical event, ActionIntent
and Upgrade inspectors: route provenance names the original ref, MR/head and
actual publication intent IDs; refusal reasons and incomplete contract activation
remain visible. No new authoritative object or hidden operator action is added.

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

Pull-request discovery accepts the provider-neutral `state` filters `open`,
`closed`, and `all`, defaulting to `open`. CodeHost service and assignment broker
share normalization; native terms such as GitLab `opened` stay inside the adapter.
The broker catalog advertises the enum and default. Unsupported filters receive
an audited HTTP400 `invalid_pull_request_state` before credential/provider use;
genuine provider failures retain their separate error and redaction handling.

The internal `GitHubClient.read_json_bounded` transport supports fixed-provider
REST reads and engine-owned GraphQL queries with a maximum 512 KiB JSON body and
64 container levels. It requests identity encoding, rejects unrequested compressed
responses before decompression, and does not follow redirects. Missing or invalid
JSON, duplicate object keys, non-finite numeric constants, content-length mismatch,
and unexpected media types are incomplete reads, never empty fact collections.
HTTP denial and absence retain their original status codes. Endpoint schemas,
pagination completeness, scope and authority remain the adapter/service caller's
responsibility; this internal transport grants no provider capability by itself.

The `code-host-merge-requirements` projection contract is version `1.0` and
rejects unknown versions. It identifies the canonical repository/binding, provider
instance, exact head/base/target branch, observation time and finite freshness
window. Each source separately records complete, established absence, denied,
unsupported, truncated, unknown or changed availability. Only legacy protection
can have established absence; a completely read empty rules/check/review/thread
collection is a different observation. Unknown counts and applicability remain
null, rather than becoming zero or false. Boolean and count fields reject coercion.

A complete observation requires all six distinct sources (PR, legacy protection,
effective rules, checks, reviews and conversations), a requirements fingerprint,
and explicit counts/applicability. Changed, uninterpreted or incomplete sources
cannot claim completeness. Check requirements distinguish an explicit any-app
requirement from observations with unknown publisher attribution. This projection
is an expiring read fact, not a persisted requirements catalog or permission to
merge; mutation-time authority and provider enforcement still apply.

The GitHub normalization engine reads classic protection and all effective
repository/organization rules independently, combining approval minima with
`max`, required checks by context/app and restrictive flags with `or`. A generic
404 remains unknown. Established legacy absence requires the explicit bounded
`Branch not protected` protocol response and a matching null GraphQL protection
rule. It never establishes absence of effective rulesets. Classic REST and
GraphQL controls must agree, including policy flags unavailable in classic REST;
uninterpreted constraints remain explicit blockers. See GitHub's
[branch protection](https://docs.github.com/en/rest/branches/branch-protection),
[effective rules](https://docs.github.com/en/rest/repos/rules#get-rules-for-a-branch)
and [GraphQL branch](https://docs.github.com/en/graphql/reference/branches)
contracts.

Checks, independent reviews and conversations use bounded complete pagination.
Review identity uses stable provider Node IDs rather than mutable logins; comments
and pending reviews cannot erase an approval, while ambiguous latest opinions
remain unknown. A status context cannot borrow a same-named check's app identity.
Both policy sources and PR head/base/target are read again before returning;
any detected change invalidates the observation. Completeness describes source
coverage, and does not imply that the observed merge/review/check gates pass.

Each observation owns at most 40 requests, five pages per collection, a 30-second
whole-read deadline/freshness window and a 128 KiB projected body. Byte-budget
omission returns explicit truncation and null requirement counts, not an empty
complete policy. A reader instance owns one observation and cannot reuse previous
scope/facts. An engine-supplied canonical checkpoint runs before and after each
request, including provider errors, and before returning; its failures escape as
denials instead of being converted to partial provider data. Cancellation closes
the read without mutation or automatic replay. Public discovery still requires
the separate declared CodeHost/broker capability and current canonical authority;
these normalization primitives do not widen existing profiles or grants.

### Merge-requirement admission and operator inspection

The explicit CodeHost capability is `pull_request.merge_requirements.read`.
GitHub advertises it; GitLab explicitly excludes it and reports unsupported.
The assignment broker exposes `repository.read.merge-requirements` at
`GET /api/repository-facts/pull-requests/{external_id}/merge-requirements`, using
the existing canonical repository selector. The authority capability is
`repository.pull-request.merge-requirements.read` at read level. Discovery marks
the operation as requiring an explicit declaration; the assignment context reports
whether its pinned execution Definition is still current and declares it.
Discovery is metadata, not effective authorization. Existing execution/profile
seeds and authority grants are unchanged.

Every read requires a live assignment validator, exact current published execution
Definition and operation declaration, and independent current read authority for
the requester and inspecting principal. Selection uses the existing ActionProvider
registry and canonical Resource; zero or multiple eligible bindings deny the read.
The service rechecks current identity/membership, provider instance, Resource
provenance, binding and active SecretBroker reference before and after each HTTP
request and after scrubbing/auditing. The inspecting worker uses its own authenticated
SecretBroker rights; a privileged requester cannot lend credential authority.
Binding/policy changes, rotation, revocation, natural credential expiry, altered
assignment scope/fence/worker, expired lease/deadline and superseded declarations
prevent results from escaping. Reads accept no body, provider selector or raw URL.

Execution-profile records use the registry's uncached effective-time lookup.
A second registry/process
superseding or quarantining the declaration, or the declaration reaching its
effective-until time, invalidates an already primed reader and the assignment
inspector's declaration metadata. No invalidation notification is required for
this guard. This changes read freshness without a persisted contract migration.

The existing assignment inspector consumes the same typed projection through
`GET /api/control-plane-broker/merge-requirements`. Its authenticated operator must
have operator admission, their own current read authority and credential-use rights.
It loads the assignment and worker from canonical storage on every checkpoint,
validates current custody metadata, and preserves requester, operator, worker and
Definition attribution. It does not authenticate as the worker or synthesize worker
token scopes. UI controls provide explicit repository/PR selection, required versus
observed evidence, per-source coverage, exact revisions, provenance, expiry and
blockers. Expiry visibly marks retained evidence stale without polling the provider;
selection and Project changes fence late results. Provider text is escaped and PR
links require a matching configured HTTPS provider authority.
Each read carries a metadata-only correlation ID shared by its returned scope and
the existing SecretBroker audit, with assignment/execution/Project/fence, requester,
inspection kind and exact Definition record. No provider bodies or credential
material enter that audit context.

This is an additive runtime/read contract, with no persisted requirements store or
database migration. The standalone projection rejects versions other than `1.0`.
Existing bindings/profiles acquire no declarations or credential rights on upgrade;
enabling a read requires an explicit canonical publication by an authorized operator.
Older engines reject the unknown broker route on downgrade. They cannot consume
the new read projection or infer a grant from a published operation string. Custom
adapters must enumerate supported capabilities explicitly and implement the optional
read before advertising it; the service denies a missing implementation before
credential resolution. Mutation-time expected-head checks, authority, approvals and
provider enforcement retain their existing contracts.

## Read-only by construction

`CodeHostService` exposes read/discovery and webhook-normalization operations only.

It intentionally does **not** expose create-branch, open-PR/MR, merge, comment, approve, tag, release, or other mutation methods. Consequential external mutations must continue through canonical `ActionIntent` / `ActionProvider` handling so authority, approval, idempotency, reconciliation, provider receipts, and evidence remain enforceable.

A code-host extension may register both a `CodeHostProvider` and corresponding `ActionProvider` capabilities, but those are distinct boundaries. Installing/enabling a read adapter does not grant mutation authority.

The built-in GitHub and GitLab mutation adapters expose the same governed
`code-host.*` ActionProvider catalog. They do not add write methods to this
read/discovery service. Branch publication, issue mutation, and pull/merge
request upsert therefore retain ActionIntent authority, policy, idempotency,
receipt, verification, Evidence, and SecretBroker semantics regardless of host.

GitLab merge-request upserts map canonical draft state to the documented native
title markers (`Draft:`, `[Draft]`, or `(Draft)`), rather than an unsupported REST
`draft` parameter. Ready requests remove leading draft markers while preserving
title content. Receipts attest the resulting native title, and verification still
requires the exact title, draft state, ownership marker, body, branches, and IID.
The existing ActionIntent and provider-result views expose these outcomes; this
mapping adds no operator control or independent reconciliation path.

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

## Bounded CI artifacts

Artifact discovery and download share `ARTIFACTS_READ`, canonical repository
Resource scope, assignment broker authority, and the existing SecretBroker
credential reference. GitLab maps a run ID to a native pipeline ID and an artifact
ID to the job ID owning its archive. Discovery checks project/pipeline identities,
fetches at most ten pages of 100 jobs, and fails explicitly if that bound is
reached rather than silently returning a partial collection. Downloads verify the
job and canonical numeric project identity and reject missing/expired archives.
Metadata follows the [GitLab Jobs API](https://docs.gitlab.com/api/jobs/);
archive/member reads follow the
[Job Artifacts API](https://docs.gitlab.com/api/job_artifacts/).

The assignment endpoint
`GET /api/repository-facts/artifacts/{artifact_id}/download` accepts an optional
`member_path` query value for providers implementing native member reads. GitLab
streams that member directly; it does not fetch or unpack the whole ZIP locally.
Paths must be relative, at most 1024 characters, and contain no empty/dot/traversal
segments, backslashes, colons, percent escapes, or control characters. The same
320 KiB byte ceiling applies to both archive and member reads. Results preserve
binary base64, returned media type, byte count, and explicit truncation. Providers
without native member support reject that option before resolving credentials;
GitHub whole-archive behavior remains unchanged. GitLab download redirects never
forward the private token to a different origin and reject HTTPS downgrades.

UI impact: the existing artifact fact and download views remain the contract;
this adds no new first-class object or provider-specific settings surface.
