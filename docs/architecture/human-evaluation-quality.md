# Manual semantic evaluation reports

Human semantic annotations supplement deterministic offline replay. A passing
replay can have a semantic failure, and a failed replay remains failed even
when a reviewer assigns a high quality score. Neither result rewrites the other.

The [rubric scoring engine](evaluation-rubrics.md) supplies criterion arithmetic.
The `EvaluationQualityService` connects it to authenticated human input and
canonical Artifact/Evidence records. It makes no model, replay or provider call.

## Exact attribution and source boundaries

A reviewer selects an immutable rubric Definition reference and an output
Artifact linked by valid Evidence in the recorded run's trace. The scenario must
have a canonical Project. The output must be active, in that Project, with an
exact revision and SHA256 digest. The submitted revision must match readback.
The digest is canonical artifact metadata; the service does not retrieve or
execute generated output, nor independently certify the bytes.

The report binds the complete canonical run fingerprint, run ID, output
Artifact/revision/digest, rubric record/revision/schema/checksum, scenario/version
and human identity. The run supplies the historical fixture, context, model,
prompt, runtime and Definition pins. No current-model or active-rubric substitution
occurs. Historical rubric revisions remain reproducible after later revisions.

Annotations are `user_outcome` Evidence with source `human-evaluation-quality/1.0`
and result `info`. They inherit governance from the output and its trace Evidence,
deny model context, and are excluded from technical gates by the existing
Evidence-type boundary. They do not assert independent verification, study
findings, release qualification or reviewed policy thresholds.

Reads recompute arithmetic from the exact pinned Definition and typed observation,
rather than trusting a stored score. Missing criteria yield an inconclusive
report. Invalidated/superseded/expired outputs, invalid source Evidence, changed
pins or unavailable/nonhuman producers yield an inconclusive annotation. Original
run assertions and safety Evidence remain intact. Individual annotations are
displayed separately; disagreement is not hidden by an automatic mean or vote.

## Identity, retries and API

Mutations require a canonical human administrator and MFA. Service identities
cannot submit human annotations regardless of their scopes. Reviewer identity
comes from authentication; the request cannot override it. Read access requires
an administrator, or both `evaluation:admin` and `artifact-evidence:admin` service
scopes, and canonical tenant/Project access.

- `GET /api/evaluations/runs/{run_id}/quality/options` lists scoped rubric revisions
  and active exact-revision trace-linked outputs.
- `POST /api/evaluations/runs/{run_id}/quality` records bounded criterion ratings
  and findings with a caller retry key. Identical retries return the same Evidence;
  different content at that key conflicts atomically.
- `GET /api/evaluations/runs/{run_id}/quality` returns deterministic status,
  valid and inconclusive annotations, pins and limitations.
- A named `baseline_evidence_id`, with an explicit `candidate_evidence_id` when
  needed, compares exact observations. Different rubric revisions or
  tenant/Project/scenario versions refuse comparison. Both Evidence IDs and run
  pins accompany the deltas. Incomplete deltas remain null.

Source scans allow at most 100 trace Evidence records and 100 output Artifacts;
larger inputs refuse inspection/scoring. The report view shows at most the newest
100 annotations with an explicit truncation flag. An explicit Evidence selection
allows comparison without guessing a candidate from a truncated/multiple-review
list. Rubric options are bounded to 100 scoped revisions. Criteria/ratings and
finding length follow rubric schema1.0 bounds. No new durable schema or migration
is introduced: annotations use existing Evidence metadata and links.

## Inspector and remaining acceptance

The orchestration inspector's **Evaluation & replay** records open human quality
review. It exposes pins, per-criterion findings, human identity, separate safety
and semantic states, named comparisons, and canonical output/annotation links.
Loading, denied, missing-output/rubric and superseded-source states are visible.
Denied writes retain drafts and unchanged retry keys. Late replies cannot replace
another selected run. Mounted deployment paths, mobile layout and keyboard
dialog dismissal are supported. Refresh and comparison remain read-only.

The browser/Python fixtures are synthetic protocol/arithmetic examples, not
genuine human calibration. [#1372](https://github.com/garnser/codex-web/issues/1372)
still requires real human-labelled examples for defect investigation, evidence
synthesis and UI/design critique, human review of workload thresholds and
repeatable calibration. A suggested rubric review starting point is below; these
values are proposals, not published definitions or measured targets.

| Criterion | What the human reviewer assesses | Proposed weight |
| --- | --- | --- |
| usefulness | Whether the output helps achieve the stated user goal | 2 |
| evidence-support | Whether claims follow the supplied evidence and uncertainty | 3 |
| user-constraints | Whether relevant requirements and constraints are retained | 3 |
| actionability | Whether recommendations give feasible next actions | 2 |
| appropriate-detail | Whether detail is sufficient and proportionate | 1 |

For each of the three workloads, a human should create a scoped rubric revision
with criterion descriptions specific to the task, a reviewed 0–4 scale and
explicit aggregate/criterion thresholds. Capture sanitized outputs and their
context as canonical Artifacts/Evidence before creating the immutable replay
fixture. Collect manual annotations through the authenticated form; retain
conflicting labels and compare exact annotations to a named baseline.

No model judge or automatic release/routing policy change is enabled. Fresh
candidate model capture remains a separately governed, explicitly requested and
budgeted qualification operation. This manual-report slice does not establish
calibration sample size, accuracy, false-positive/negative rates or study demand.
