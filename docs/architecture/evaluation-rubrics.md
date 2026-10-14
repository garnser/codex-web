# Deterministic offline rubric scoring

The `evaluation-rubric` Definition kind uses schema `1.0`. The application
registers its interpreter without seeding or publishing any rubric. Criteria,
scales, positive weights, workload name, aggregate threshold and optional
criterion floors are versioned Definition data. The existing Definition
inspector/API supplies publication, payload inspection and historical revisions.

`score_rubric(record, observation)` is an offline library function. The caller
loads the exact canonical Definition record and supplies its full reference,
tenant/workspace/project and task scope, artifact reference and SHA256 content
revision, annotation reference, rater reference and criterion findings/ratings.
The engine revalidates the record checksum and reference and rejects a scoped
Definition outside the observation scope. Historical superseded and unpublished
candidate records may be scored; scoring does not publish or authorize them.

Each rating is normalized as `(rating - minimum) / (maximum - minimum)`.
The aggregate is the weighted mean of all normalized ratings. A complete result
passes only when it meets the aggregate threshold and every configured criterion
floor. Missing ratings retain null score/pass and explicit missing findings;
partial means never qualify. Unknown or duplicate criterion IDs, invalid scales,
nonpositive weights, out-of-range or nonfinite numbers and unsupported schemas
are rejected. Rubrics and observations contain at most 32 criteria/ratings.

`compare_rubric_scores(record, baseline, candidate)` recomputes both results
from observations, requires the identical rubric pin and tenant/project/task
scope, and returns aggregate and per-criterion deltas. Different output artifact
references/content revisions are preserved. Incomplete deltas remain null.

## Boundaries and remaining integration

There is no model invocation, provider action, live replay, new store, schema
migration or automatic change to `EvaluationTrace` or its
deterministic assertions. Engine results are calculations, not qualification
Evidence or authority grants. Existing policy/no-LLM assertions remain separate.

Observation scope, artifact content digest, annotation reference and rater
reference are caller-supplied attribution. The library cannot establish that a
rater is an authenticated human, verify artifact bytes or certify calibration.
An integration must resolve canonical records and enforce authorization and
data governance before using them. Findings must be sanitized; this API is not
a secret or raw prompt transport. Synthetic arithmetic tests are explicitly
fixtures, not human-labelled research or calibrated quality assessments.

The [manual semantic evaluation integration](human-evaluation-quality.md) adds
authenticated annotation intake, canonical Evidence and the result inspector
without changing deterministic evaluation state or automatic policy.

The independently deliverable scoring prerequisite is tracked in
[#1410](https://github.com/garnser/codex-web/issues/1410). Genuinely human-labelled
defect-investigation, evidence-synthesis and UI-critique fixtures, calibration
and reviewed workload thresholds remain tracked by
[#1372](https://github.com/garnser/codex-web/issues/1372). No model judge or
automatic routing/autonomy change is enabled by this prerequisite.
