# Human acceptance and outcome metrics

Outcome accounting composes canonical runtime usage, human review Evidence and
versioned Metric definitions. It does not extend the content-free UX collector,
infer acceptance from a model's completion claim, or alter routing or autonomy.
Inspection, joins, calculation and publication invoke zero model calls.

## Observation and authority

An optional human review observation is `EvidenceType.USER_OUTCOME` with source `human-outcome/1.0`
and informational result. Its producer is the authenticated human, not a caller
supplied identity. Service principals cannot report human acceptance. It records
an exact runtime usage ID, outcome, workload category, optional exact template
Artifact/revision, correction flag and bounded counts/time. There is no free-form
comment or prompt field. Review time is an explicit cumulative observation in
seconds; browser idleness and runtime duration are not substitutes.

The API requires Project/tenant matching and administrator authority. Human
mutations additionally require MFA. Joined service inspection/publication needs
both `metrics:admin` and `artifact-evidence:admin`. Published `outcomes.v1.*`
metrics preserve this read boundary in the generic Metric explorer, including
workspace-wide reads, history and snapshots. Ordinary Metric APIs are unchanged.

Human outcome feedback is excluded from technical Evidence gates even if a gate
accepts informational reviews. It does not create an ApprovalRequest, independent
Verification, provider receipt, or Work Item completion.

## Fixed calculation contract

The code-owned calculation contract is `accepted-outcome/1.0`. Changing a formula
requires a new engine version; editing a Metric name or description cannot change
the algorithm. Project/cohort Metric definitions are created in canonical Metrics
on explicit publication, using its existing revision, observation and snapshot
primitives. Names, ownership, freshness and thresholds remain canonical Metric
data. No separate analytics state or mutable operational catalog is added.

An eligible task has a canonical runtime usage record whose first recorded start
is in the half-open enrollment window `[start_at, end_at)`. When no start is
available, the recorded observation time is used. Attempts sharing the same
Work Item reference are one task; other records group by execution ID, falling
back to usage ID. Identical usage IDs are counted once. All recorded attempts up
to inspection time contribute cost, tokens and runtime duration, including retries
outside the enrollment window. Runs without a canonical grouping reference
cannot be proved to belong to the same task and remain separate.

The current attempt is the last start, with usage ID as a deterministic tie-break.
A review of an older attempt cannot accept a newer one. Known native termination
is required for accepted/rejected feedback. Unfinished execution stays pending;
unknown termination stays unknown. A successfully completed but unreviewed task
is unknown, never accepted.

| Measure | Numerator | Denominator / missing treatment |
| --- | --- | --- |
| Accepted outcome rate | Explicitly accepted current tasks | All eligible tasks, including rejected, pending, unknown and reopened |
| Human correction rate | Current measured correction flags that are true | Current human review observations with correction measured; missing flags make the value null |
| Review / intervention count | Sum of explicitly observed cumulative task counts | Null if any eligible task lacks that observation |
| Review seconds | Sum of explicitly observed cumulative task effort | Null if any eligible task lacks measured effort |
| Tokens / USD per accepted outcome | Usage across every eligible attempt, including rejected work and retries | Accepted task count; null with no acceptance or any missing usage |
| Runtime seconds per accepted outcome | Recorded runtime duration across eligible attempts | Accepted task count; never labelled human effort |

Cost uses the canonical runtime's recorded `cost_usd` field. Comparison-pricing
and quota resources are excluded; recorded usage cost may differ from billed
subscription charges. Missing cost remains null. The UI exposes eligible/accepted
counts, correction observation count and each unresolved outcome count, alongside
the formula version and source records. Global acceptance includes unclassified
tasks. Workload categories and selected template references are human observations;
model IDs come from runtime attribution. Unclassified/unobserved cohorts remain
visible. A matched category is not proof that tasks are comparable or that one
model caused an improvement.

## Revisions, duplicates and late observations

An idempotency key is scoped to tenant, human producer and source. The same
request returns its original Evidence; changed content conflicts. Evidence IDs
are deterministic and append deduplication is transactional. A late review is
applied to the original enrollment cohort at the next inspection.

Explicit supersession links replace old observations without modifying them.
Multiple un-superseded reviews are a conflict and yield unknown acceptance. A
resolution can supersede all displayed heads. Reopening removes acceptance until
a new current observation accepts the work. Invalidated/expired review Evidence,
missing ancestors, unavailable human provenance, or stale attempt attribution
cannot restore an older acceptance. Template Artifact supersession retains the
existing dependent-Evidence invalidation semantics.

Review counts/time are cumulative task observations, not sums of revision history;
supersession therefore cannot count the same effort twice.

## Publication, freshness and historical truth

Inspection returns a derived projection without writing Metric state. Explicit
publication recalculates on the server and records an immutable informational
projection Evidence with the exact window, cohort counts, source IDs, measurements
and formula version. It inherits source Evidence governance and denies model
context. Every Metric observation references this projection and its source
Evidence. Its content digest identifies the exact projection, and the observation
pins the current Metric revision. A duplicate identical projection reuses its
Evidence and Metric observations.

Runtime counters are mutable, so arbitrary historical `as_of` reconstruction is
refused. Historical truth comes from the saved projection Evidence and canonical
Metric snapshots. Old enrollment windows (over one day behind inspection), missing
input and partial runtime telemetry are explicitly labelled. Observation freshness
is anchored to enrollment-window end so republishing an old cohort does not make
it current. Unknown numeric measurements are never published as zero.

Evaluation refuses over 5,000 usage records or 10,000 relevant reviews rather than
silently truncating denominators. Evidence state advances to `1.3` for the distinct
non-gating outcome type. The `1.2` migration preserves existing records exactly;
older supported versions migrate through that step. Older readers refuse `1.3`.
Technical gates exclude the outcome type regardless of source or accepted result.
Metric state retains its existing contract.

Upgrade all Evidence readers and writers through the canonical qualified upgrade,
drain, backup and restore-verification flow before enabling this feature. The first
Evidence write under the new code persists `1.3`; source-only rollback to an older
reader is then unavailable. Recovery requires a supported new reader or an
explicitly qualified restoration of the pre-transition snapshot. Avoid mixed
writers and preserve all original evidence, queues, claims and uncertain outcomes.

## Surfaces

The existing Metrics dialog contains a Project-scoped outcome inspector, optional
feedback form and explicit publication action. Its form exposes the current
attempt and supersession targets, retains a denied draft, and clears prior results
and drafts on Project changes. Publication links to the canonical Metric drill-down.
Empty, unavailable, denied, missing, partial, stale and conflicted states are visible.

Use [the worked comparison](../examples/accepted-outcome-comparison.md) to reproduce
the calculation. It is a synthetic example, not evidence about real users.
