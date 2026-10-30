# Artifacts, evidence, verification, and deterministic gates

Codex-web records produced artifacts, observed evidence, and verification as separate canonical objects. An agent statement such as "tests passed" is not itself evidence.

## Artifact identity

An `Artifact` has a stable ID plus tenant, Project, Work Item, execution, execution-workspace, and canonical Resource provenance where available.

Initial artifact types include commits, branches, pull requests, patches, reports, screenshots, builds, packages, deployments, logs, and generated files.

Artifacts may carry:

- provider/source/external ID and deep link;
- revision;
- SHA-256 digest when applicable;
- producer identity;
- lifecycle and retention metadata;
- supersession links.

Supersession keeps both identities: the previous artifact becomes `superseded` and points to its replacement. Evidence dependent on the previous artifact is invalidated immediately.

Artifact and Evidence creation commit their metadata and mandatory governance
record/audit event in one shared canonical StateStore transaction. Cancellation,
failed writes and late composed guards roll back both. Concurrent idempotent
Evidence creation resolves the existing object before registering new metadata.
Internal composed writers include the prepared creation's complete transaction
domains and apply it under their existing guards; an incomplete legacy callback
fails before registration. Different governance/domain backends are refused.
Artifact creation without supersession reads and validates the complete current
catalog in that transaction, without a redundant full-catalog pre-read.
Supersession retains its producer/lifecycle prevalidation and atomic recheck.
No persisted contract version or external content action changes.

Existing governed deletion can reconcile an already absent local object with an
`already_absent` adapter receipt. It preserves the governance record and audit
history, does not invent missing Artifact fields, settle an unknown provider
intent, or claim deletion of external content. The exact current absence and
original protected custody must be checked before an operator uses this path.

## Evidence

Optional product acceptance observations use the distinct `user_outcome` Evidence
type. They retain human producer, scope and revision provenance but are excluded
from technical Evidence gates. Artifact/Evidence state `1.3` preserves `1.2`
records through deterministic migration and makes older readers refuse the new
state. [Outcome metrics](outcome-metrics.md) documents the qualification and
rollback boundary before enabling outcome writes.

Evidence is an observation or result about artifacts/work:

- test result;
- CI check;
- review;
- security scan;
- deployment verification;
- policy evaluation;
- approval record;
- artifact verification.

Evidence has its own producer, provider/source, result, timestamps, optional digest/deep link, and retention lifecycle. Evidence may reference one or more canonical Artifact IDs.

Evidence cannot be created against an inactive/superseded artifact, and an explicit Work Item attribution must agree with the referenced artifacts.

## Verification independence

A `Verification` is distinct from both Artifact and Evidence.

Independence is **computed**, never supplied by the caller. A verification is independent only when the verifier identity differs from every producer of the referenced artifacts/evidence.

A verification records method, result, findings, source/provider and deep link. If referenced proof is invalidated, expired, or superseded, the verification becomes `invalidated`.

This allows validation/release gates to require independent proof rather than trusting the producer's own assertion.

## Invalidation and retention

Artifact invalidation or supersession cascades to:

1. dependent valid Evidence;
2. Verifications referencing either that artifact or the invalidated Evidence.

Evidence invalidation/expiry similarly invalidates dependent Verifications.

Retention expiry runs at application startup and can also be invoked administratively. Expired proof cannot satisfy evidence gates.

Only the producer or a tenant administrator may invalidate or supersede a record, preventing same-tenant actors from rewriting provenance they do not own.

## Machine-readable evidence requirements

`EvidenceRequirement` declares:

- stable requirement ID;
- evidence type;
- optional required artifact type;
- minimum matching count;
- accepted results;
- optional provider;
- optional freshness limit;
- whether independent verification is required.

Canonical Work Item execution state owns `evidence_requirements`. The execution contract copies those requirements directly as `required_evidence`.

Execution contract schema **1.4** therefore carries proof requirements alongside expected outputs. Agents do not infer gate requirements from prose.

### Observed research eligibility

Artifact/Evidence state `1.2` adds canonical `knowledge_ids` and a derived
`research_origin` snapshot, using Memory's origin vocabulary. Existing state
`1.0`/`1.1` migrates through the existing registry to unknown origin and no
research links. Creation resolves exact current Knowledge records through
Memory authority and freshness checks, enforces the same Project or company
scope, and inherits their governance references. Callers cannot supply an
Artifact/Evidence origin label directly. The inspector displays origin and
source IDs as descriptive metadata, not verification.

Report metadata exports also retain bounded source-level Knowledge ID/version
and origin snapshots, including mixed derivation. Snapshot references name
canonical Memory records; they do not copy participant identifiers, transcript
contents, authorship or raw external source references. Source read constraints
remain enforceable on derived Memory get/list/retrieval and evidence eligibility.

Research Knowledge that cites canonical Evidence resolves that evidence's
governance record automatically during creation/revision. Callers cannot omit
source classification/retention/deletion inheritance by leaving the optional
governance-reference list empty. Foreign, missing or ungoverned evidence refuses
research provenance creation; ordinary unknown-origin Memory keeps its existing
compatibility behavior.

Source governance actions follow the existing governed source graph across
Memory, Artifact and Evidence payload handlers. Derived summaries and research
proof are redacted/deleted along with their source; no separate deletion system
is introduced. A derived legal hold blocks the source action before payload
mutation. Unavailable dependent domain adapters refuse the action. Canonical
governance remains responsible for the request, actor, source lifecycle and
action receipt; domain handlers retain their existing payload tombstones and
dependent-verification invalidation semantics.

An optional `EvidenceRequirement.observed_research` requires current observed
source proof in addition to its ordinary evidence constraints. It resolves
source-level distinctions from mixed Knowledge, rejects synthetic-only,
unknown, stale or unavailable sources, and requires valid source
Evidence with active digest-bound Artifacts. Source retention, content
integrity and configured freshness limits also apply. The source Evidence
must have a verified independent canonical Verification with method
`observed-user-research`. That method can be recorded only by a human actor;
the Verification retains the authenticated verifier principal kind, and legacy
records with unknown kind cannot qualify from a method label. Independence is
still computed against the proof producers; the research gate also excludes
the linked observation artifact's producers. A failed usability outcome remains
eligible source material when independently verified as an observed finding;
candidate evidence result requirements still apply separately. Ordinary review
methods and descriptive reviewed/origin labels cannot satisfy this gate.
Evaluation reports exact source verification IDs and a denial reason when
observed proof is absent. Mixed reports retain mixed origin even when eligible
observed-source proof exists for their observed material.

Only contracts with this opt-in requirement use execution schema `1.5`.
Ordinary engineering contracts remain `1.4` and omit the research flag when
false. Schema `1.4` refuses a true observed-research requirement. No model call
or research database participates in eligibility calculation.

## Gate evaluation

`ArtifactEvidenceService.evaluate()` evaluates requirements deterministically against:

- same-tenant records;
- the exact Work Item;
- valid/non-expired Evidence;
- active referenced Artifacts;
- accepted result/provider/type/freshness constraints;
- verified independent Verifications when required.

The result is `EvidenceGateEvaluation` with one outcome per requirement and exact matching evidence/verification IDs.

A success, validation, approval, or release flow can consume this structure instead of asking whether an agent claims a check passed.

## APIs

Artifact registry:

- `GET/POST /api/artifacts`
- `POST /api/artifacts/{artifact_id}/invalidate`

Evidence registry:

- `GET/POST /api/evidence`
- `POST /api/evidence/{evidence_id}/invalidate`

Verification registry:

- `GET/POST /api/verifications`

Work Item requirements and gate evaluation:

- `GET/PUT /api/evidence-requirements/work-items/{ref}`
- `POST /api/evidence-evaluations/work-items/{ref}`

Retention:

- `POST /api/artifact-evidence/expire-retention`

#141 should project these records in Operations UI. UI status chips such as "tests passed" or "verified" must derive from canonical evidence/gate evaluation rather than terminal text.

### Assignment-bound owner-proof verification

The worker broker exposes the existing Verification service for
`recovery-owner-disposition.v2` and the narrowly admitted
`recovery-storage-capacity.v1`, without creating another receipt or proof store.
This operation explicitly separates the current assignment requester A, whose
current Role must allow the exact `api.verifications.write` capability for the
Project/repository, from authenticated registered worker B, who records the
Verification. B must independently review A's original canonical Evidence and
all linked artifacts. The canonical service computes independence from actual
producer/verifier identities; no caller field overrides identity or independence.
A service-channel API policy check for B is additional to A's operational
authority, not a grant of A's identity or scopes to B.

The adapter requires a live assignment/fence, pinned current AgentProfile and
execution profile, writable workspace lease, current Work Item ownership by the
exact pinned Profile ID or its pinned role ID, tenant,
Project/resource visibility, current procedure/security admission and current
original firing/schedule/policy/state digests. Reference validation and Verification
append use the existing shared StateStore transaction with those canonical
admission records locked. Required mediated procedures refuse this direct adapter;
publication of this code does not bypass them or widen an execution profile.
Original records and Recovery state are never rewritten by verification.

Evidence/artifact metadata and managed content reads use existing record visibility
and governance. Content is limited to 256 KiB before opening and verified against
its canonical size/digest after reading. Artifact count is limited to eight,
including artifacts linked by the original Evidence. Only one original Evidence
is accepted. A missing original, changed snapshot, invalid record, expired lease,
revoked principal/authority or unavailable canonical admission refuses the append.
The adapter validates proof structure/current digests, not factual writer death:
B remains responsible for genuine independent review of the original inventory
and fence evidence. Missing files, age and an empty local queue are not proof.

A review is identified by its `sha256:<digest>` source, actual execution, Work Item
and method. Existing VerificationCreate has no retry-safe operation key. On response
loss, retain unknown outcome and use the bounded read-only exact lookup (32 records
maximum); one matching immutable record identifies the result, multiple or absent
records remain unknown. Never automatically replay POST or rotate the review digest.
This adapter neither creates Evidence nor activates RecoveryState 1.3, disposes an
original effect, changes Recovery health, or authorizes an operator migration.
Existing Verification inspection is the UI surface; it exposes actual producer,
verifier, independence, references, result and findings. Recovery operational
acceptance remains separate from source delivery.

The append read set includes `secret_metadata`, shared by the application's native
credential delegation and ActionExecutionService SecretBroker. When an assignment
has credential references, the adapter requires the trusted assignment-bound
broker callback to identify that same native credential-provider store. Opaque or
differently stored delegation composition refuses the mutation; it does not silently
substitute an assignment-only validator. The current validator still executes before
and after the proposed append. A separate PostgreSQL metadata revoke/rotation writer
must commit before admission (then refuse) or serialize after the append transaction.
Only metadata is inspected/locked; credential values never enter Verification or
broker output. The ScheduleRecord tenant field is `tenant_id`, paired with its
`workspace_id`; it must match the actual verifier's organization/workspace.


## Scoped Evidence point reads

A normal `get_evidence` point lookup can use the StateStore's optional document
array projection to transfer the canonical schema envelope and first matching
complete Evidence record for its exact ID, organization and workspace. The normal
Artifact/Evidence decoder still applies supported schema versions, legacy
migrations and full validation of that selected record. Lifecycle, retention,
metadata, digest and resource references are unchanged; each consumer still
performs its existing validity, freshness and authority checks. A missing or
foreign record does not become valid evidence.

This is a fresh canonical read, not a cache, index, new authority store or state
migration. It avoids materializing unrelated historical artifacts, Evidence and
verifications for one lookup; it is not a catalog-wide integrity scan. Explicit
full-state/transactional callers keep their supplied state. Older backends,
nonstandard legacy JSON and incompatible document-envelope shapes keep the full
canonical decoder path. PostgreSQL conversion fallback is isolated by a savepoint
so it cannot poison a surrounding writer transaction; unrelated storage failures
propagate. All normal writes, snapshots and restore bytes retain their existing
canonical paths.

PostgreSQL point-envelope selection shares one binary JSON parse for shape
guards and materializes original JSON members once for exact tenant/ID text
predicates. It selects the first scoped member by stored ordinality and returns
its original JSON value. This preserves duplicate order,
numeric types and legacy decoder/savepoint fallback while avoiding repeated
whole-catalog parsing. It changes no authority, schema, timeout or UI contract.

Known governed-input Verification IDs use the same fresh scoped envelope reader
through `get_verification`, selecting **all** matching ID/organization/workspace
members rather than a first match. The existing Work filter applies before the
exactly-one check, so missing or ambiguous scoped matches remain refused. Each
native input checkpoint still rechecks the original Verification result, source
Evidence, governance, current policy and authority; neither a prior successful
lookup nor an immutable input bypasses revocation. This changes no public DTO,
persisted format, provider I/O budget or inspection surface.

Managed content opening reuses the existing fresh scoped Artifact getter and
retains its first-scoped-member selection, content-state, digest and range guards.
Governed-input checks may share one selected Governance snapshot across the three
principals within that check; each principal retains its normal current filters
and authority checks. Custom/legacy owners retain their individual filter calls,
and no snapshot survives into a later checkpoint.

A governed-input callback reads its three known source Evidence IDs from one
fresh scoped envelope. Each ID retains the same first-scoped-member selection
before decoding, request order and repeated-ID behavior as individual reads;
missing members still refuse. Custom getters and unsupported backend projections
retain their existing owner/fallback validation. Every later callback reads again,
then performs all original provenance, freshness, governance and authority checks.

### Home preservation atomic catalog guard

Dormant Home preservation computes actor/tenant/source/key identities and checks
checkpoint ambiguity and exact prior preservation within ordinary governed
Artifact creation's existing cross-domain transaction. Full Artifact validation
runs first. The private guard checks the first stored actor-scoped checkpoint,
then dormant preservation, before Governance domain decoding, registration, model
export or upsert. A valid prior returns an empty delta without requiring the
ordinary creation payload-hash metadata. If no scoped prior exists, a foreign-only
collision on the computed preservation ID refuses before ordinary unscoped
idempotency can return that row. A valid scoped prior still wins over an earlier
foreign row. New creation retains complete typed
Artifact/Governance validation, current authorization and atomic rollback.

Snapshot observations and canonical readiness initialization remain outside these
locks and inside the original five-second budget. This intentionally permits a
metadata observation refusal before checkpoint ambiguity; ambiguity still forbids
any new Artifact/Governance write or native input. Composed writers and all
ordinary creations without the private guard retain their existing path.

Only the explicitly identified pre-write Artifact domain-decode refusal may use
the original scoped batch lookup to recover an already-valid prior or reject a
checkpoint. No prior means the original decode error is re-raised. Guard, updater,
Governance, database, export and write errors never enter this fallback. Raw JSON
parsing happens earlier in the shared backend: syntactically corrupt shared rows
can refuse even prior reuse, and are never written past. Malformed typed Governance
objects remain irrelevant to valid prior reuse because its domain decoder is not
called. No new store, schema, cache, timeout or UI object is introduced; existing
Artifact inspection and typed Home refusal surfaces remain sufficient.

### Assignment-bound MR observations

Artifact/Evidence state **1.4** adds the fixed MR observation semantics. The
existing registry migrates 1.3 to 1.4 by changing only the envelope version; all
prior objects and metadata remain unchanged, without legacy freshness relabels.
A 1.3 reader refuses the 1.4 envelope. Deployment is forward-only and requires
the existing canonical migration evidence, recovery qualification and approvals;
no operational rollback to a reader that ignores observation freshness is promised.

`verification.create` accepts the additional strict protocol
`code-host-mr-observation.v1`, using the existing capability and canonical
Artifact/Evidence store. Its request contains only `method`, the exact assigned
`repository_resource_id`, positive `pull_request_number`, `expected_head_sha`
and a stable 32-hex `request_id`. It accepts no caller result, producer,
independence, credential, URL, criteria override or arbitrary Evidence payload.

The owner reuses the existing credential-bound merge-requirement reader and
current requester/worker/Profile/Definition/assignment/fence/workspace checks.
The returned typed observation must match the same repository/MR/head and its
provider freshness window. Current custody, provider binding and authority are
rechecked inside the existing canonical transaction that appends the three
governed Evidence records and Verification. No external mutation is performed.
A failure rolls back both evidence metadata and governance registration.

The three results distinguish independent current-head review observations,
observed exact-head CI, and completeness of the merge-policy snapshot. Observed
successful checks do not establish unknown required-check or protection policy.
GitLab's lack of commit-associated approval proof remains unknown; it is never
converted into an approval. Known unmet review/check facts fail; unavailable or
incomplete facts warn. A complete policy snapshot does not imply that its rules
are satisfied. Neither observation success nor the worker's execution creates
reviewer consent, new QA approval or permission to merge.

The authenticated worker is the Evidence producer; the original requester and
current authority decision remain separately attributed. Verification
independence is calculated by the existing service. A worker verifying its own
observations is not an independent verifier. The original typed provider
snapshot, its digest/read ID and exact execution Definition are retained as
bounded governed provenance.

Provider freshness is **not data retention**. `observation_expires_at` bounds
current use of this fixed protocol; expired observations are rejected by current
procedure/Evidence gates. Original records and their provenance remain available
for historical reconciliation, under normal governance and retention policy.

Response loss uses only `verification.read` with `method` equal to this protocol
and the original `source`; no Work reference is substituted or request replayed.
The source is `sha256:` plus the SHA-256 of sorted compact JSON containing
`assignment_id`, `execution_id`, `fence`, `method`, and stable `request_id`.
The exact typed request has a separate retained digest. Reusing the same request
identity with different content is refused before another provider read; it
cannot manufacture a new observation by changing the requested head.
A unique original returns its Evidence and explicit `observation_fresh` value;
absence is UNKNOWN, not permission to submit another request. An expired original
is not refreshed or relabeled as a current passing assessment.

MR publication composes its existing Evidence, Verification and optional input
Artifact updaters against one complete validated Artifact/Governance model pair
inside the owning transaction. It exports each domain once, then rechecks current
principals, scope and the unchanged observation deadline. No model is cached
across transactions. Expiry during composition aborts the canonical append; expiry
during backend commit can occur after a durable append, so a post-commit check
returns the same typed reconciliation refusal instead of an already-expired 200.
That refusal does not claim rollback: exact-source lookup retains the original
with `observation_fresh=false`, and never repeats the provider read. Procedure
start still independently requires fresh, governed, exact-scope input.

Both this protocol and `code-host-mr-continuation-input.v1` return HTTP 409 for
the closed observation conflicts `mr_observation_head_changed_or_expired`,
`mr_observation_timestamp_in_future`, and `mr_observation_check_head_mismatch`.
The real broker response retains the code in `error.code` and exact assignment,
execution, fence, method, original source and request digest in
`error.mr_observation`. Existing audit columns retain assignment, execution and
fence; the bounded denial record retains the code, method, source, request digest
and reconciliation flags as compact JSON.
Reconcile that exact method/source through `verification.read` without a new
provider read. Absence remains UNKNOWN and does not authorize replay. These
refusals preserve the original freshness and transaction guards, including the
final check after content attachment. Unreferenced managed content may remain;
do not delete a shared digest or infer that no external read occurred. Unexpected
storage or programming failures remain HTTP 500 with an unknown outcome, rather
than being misclassified as one of these conflicts.

The read-only readiness ActionIntent uses the distinct fixed
`code-host-readiness-observation.v1` marker with the original observation expiry.
Its Evidence retains the existing `provider_receipt` and `artifact_verification`
kinds: it proves the original observation and verification, including pending
results. Current consumers reject it after expiry without deleting that history
or turning an observation receipt into review consent or merge permission.

Existing recovery Verification methods retain their original request, ownership,
independence and effect-proof guards. No role or capability grant is added. The
existing operation catalog and Evidence/Verification inspectors expose the new
method, typed request, source/producer/provenance and honest result; there is no
new UI object or evidence engine. This source capability is not live Executive
or operational-procedure migration qualification.

### Observation-policy reader boundary (ArtifactEvidence 1.5)

ArtifactEvidence `1.5` preserves every existing record and adds the reader boundary
for version-2 MR observation / continuation and read-only readiness Evidence.
Publication stores the immutable canonical Configuration policy alongside the
original observation start/expiry. Current-use evaluation checks that pin and any
subsequent reset, as well as the normal Evidence lifecycle, authority and resource
requirements. Unknown future MR observation methods fail closed. Data retention
never refreshes an observation.

The `1.4 -> 1.5` migration changes only the domain envelope version; there is no SQL
or destructive migration. All writers must drain and switch together. An old 1.4
reader rejects 1.5, rather than misinterpreting an unknown v2 method as generic
Evidence. Once a 1.5 envelope has been written, rollback to an old binary is not a
supported plan. This boundary needs a truthful forward state-migration Release;
it is not a code-only rollback-compatible deployment.
