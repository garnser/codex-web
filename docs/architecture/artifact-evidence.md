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
execution profile, writable workspace lease, current Work Item ownership, tenant,
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
