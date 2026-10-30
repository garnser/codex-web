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
snapshot still refuses routing. A verified typed existing-MR publication is also
sufficient when no successful MR-upsert receipt exists: the publisher already
attested the open MR and related owning issue before the exact-head CAS.
Cancelled or uncertain historical MR updates remain unchanged and confer no
proof; route provenance records a null request-intent ID rather than inventing
a creation/adoption receipt. If a successful upsert exists, its MR/branch and
execution or typed-repair relationship must still agree.

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

GitLab ref discovery reads at most the first 100 branches and 100 tags; it does
not claim complete repository enumeration. A valid default-marked branch on that
page supplies the default tip without additional reads. Otherwise the adapter
reads repository metadata and exactly that named branch, with the same binding,
Resource and credential. The exact response must identify the requested default
branch and a full commit SHA. Malformed, missing or changed default facts refuse
rather than guessing a tip. Workspace materialization retains its independent
current-tip, provider/resource/credential and post-transfer freshness checks.
Existing repository facts and refusal surfaces expose the result; no new UI
operation, authority or persisted contract is introduced.

Pull-request discovery accepts the provider-neutral `state` filters `open`,
`closed`, and `all`, defaulting to `open`. CodeHost service and assignment broker
share normalization; native terms such as GitLab `opened` stay inside the adapter.
The broker catalog advertises the enum and default. Unsupported filters receive
an audited HTTP400 `invalid_pull_request_state` before credential/provider use;
genuine provider failures retain their separate error and redaction handling.

Known code-host read failures retain safe adapter facts on the existing
`CodeHostError`: an explicit numeric HTTP status or a transport-failure marker.
GitLab and GitHub adapters preserve existing transient-error subclass behavior.
Exception-message text alone is not a typed HTTP witness. Unknown adapter or
implementation failures retain their original internal-error behavior.

After assignment scope, authority and procedure admission, ordinary repository
fact reads translate these classified failures to HTTP409 with
`repository_read_provider_error` (safe provider status) or
`repository_read_transport_error`. The existing broker audit records the same
safe reason and correlation. No raw provider error, path or credential is
returned. Action-write execution and uncertainty handling are unaffected.
A provider404 can conceal an inaccessible repository: it establishes neither
remote absence nor permission, and does not authorize retry, publication or
`expected_remote_absent`. Existing audit inspection renders this safe reason;
no new UI eligibility or domain object is introduced.

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
GitHub and GitLab advertise it; both still require an explicitly declared provider binding and current canonical authority.
The broker distinguishes an installed adapter's unsupported capability from
missing or ambiguous provider bindings. Its existing worker/operator denial and
discovery metadata expose that distinction; adding bindings or broadening grants
cannot implement a missing adapter capability. Other reads do not substitute for
unavailable merge-policy evidence.
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

### GitLab native merge-gate observations

GitLab uses the same version 1.0 projection and canonical credential/assignment
read checkpoints. Its bounded GET-only reader establishes the exact project/MR,
head and current target branch tip (not `diff_refs.base_sha`, which is a merge
base), native `detailed_merge_status`, the approval endpoint decision and an
exact-head pipeline observation. It re-reads the MR, target tip and available
approval observation, marking detected drift as changed. One observation has
at most eight requests, a 30-second deadline/freshness window, 512 KiB per response
and 128 KiB projected output; redirects/compression/invalid or duplicate-key JSON
fail closed. Every request, including errors, and the final result recheck the
canonical read guard. No provider mutation, polling or grant is introduced.

These are intentionally **partial native gate facts**, not a complete GitHub-like
branch/rules/review-thread inventory. `complete` remains false; unknown policy
flags, thread counts and commit-attributed approval counts remain null. GitLab's
`approved_by` has no approval commit SHA, so a nonempty approval list never
establishes current-head approvals. Community Edition's missing approval counts
and permission/tier HTTP404 never establish zero requirements. Native
`approved=false` alone is `NOT_APPROVED`, not a mandatory review failure; only
positive `approvals_left` or native `not_approved` establishes a required review
blocker. The provider-native merge gate remains visible alongside unavailable
policy sources so operators can assess actual known facts without fabricating
full coverage. Neither a positive native gate nor a partial projection grants
merge authority; current ActionIntent/provider enforcement remains unchanged.

The existing broker/operator projection exposes these source availabilities and
blockers; no separate UI object is added. Provider semantics follow GitLab's
[MR API](https://docs.gitlab.com/api/merge_requests/) and
[approval API](https://docs.gitlab.com/api/merge_request_approvals/).


### Premerge readiness observation action

The GitLab provider registers `code-host.pull-request.observe-readiness` as a
read-only, binding-credential-required, one-attempt action. Its only parameters are a
bounded positive MR IID and an exact expected head revision. Registration does
not grant a Profile or assignment any new operation or repository authority.
The existing merge-requirements reader owns fixed, bounded GETs against the
canonical repository locator; caller URLs, queries, executables, producer claims
and independent-review overrides are not accepted.

The result protocol `1.0` retains the complete typed merge-requirements fact and
uses the shared exact-head disposition projector. Its flat procedure outputs
include observation success, review/CI/policy dispositions, original blockers,
state/draft/merge gates and observation expiry. Complete policy or three passing
dispositions alone cannot establish readiness when actual blockers or merge
gates disagree. A successfully observed pending or blocked MR is not QA
acceptance, READY, merge permission or a merged-MR proof.

Existing ActionIntent/procedure admission rechecks the current requester,
assignment, canonical repository, provider binding, Definition and credential
around network awaits. The canonical async SecretBroker exposes only a copied,
request-local snapshot of the reference it selected for that borrow. This action
pins its lifecycle and complete metadata, including rotation/ACL changes; the
scope resets after success, exception or cancellation. This is metadata-version
attribution, not cryptographic attestation of backend material versions. No
credential values or reference metadata enter model context; the result retains
only a metadata fingerprint and rotation counter.

The registered code-host procedure adapter projects the original successful
receipt and its separate current verification through existing Evidence owners.
Verification performs a new bounded read and checks unchanged semantic facts,
exact head/scope and the original credential metadata version. Original receipt
and verification Evidence describe the observation, not passing review/CI/policy
Evidence. A one-step published diagnostic procedure can retain these proofs and
honest pending dispositions without satisfying a later merge procedure's three
passing input requirements. Existing action catalog, assignment checkpoint,
receipt/verification, output-Artifact and Evidence inspectors expose this path;
there is no new control, grant, store or execution engine.

These receipt and verification records retain their original Evidence kinds.
Their fixed `code-host-readiness-observation.v1` metadata carries the original
observation expiry; the ArtifactEvidence 1.4 current-use contract rejects an
expired observation while retaining its historical bytes. A subsequent
verification does not extend the original observation lifetime or turn the
receipt into review, CI or policy acceptance.

### MR observation validity versus transport timeouts

The v1 observation and continuation methods retain their fixed 30-second validity.
Explicit v2 methods resolve the canonical deployment Configuration
`code_host.mr_observation_window_seconds` once (default 30, finite 1..300) and pin
its exact provenance in the observation. Only registered GitLab/GitHub readers
receive this server-owned value; it is not a provider request parameter. Their
existing 30-second HTTP operation bounds, request/response limits and exact-head
checks remain unchanged. Longer validity does not waive fresh provider reads at
an action boundary or grant a merge, credential, lease or execution permission.
