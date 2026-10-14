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

An optional `EvidenceRequirement.observed_research` requires current observed
source proof in addition to its ordinary evidence constraints. It resolves
source-level distinctions from mixed Knowledge, rejects synthetic-only,
unknown, stale or unavailable sources, and requires valid passing source
Evidence with active digest-bound Artifacts. Source retention, content
integrity and configured freshness limits also apply. The source Evidence
must have a verified independent canonical Verification with method
`observed-user-research`. That method can be recorded only by a human actor;
independence is still computed against the proof producers. Ordinary review
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
