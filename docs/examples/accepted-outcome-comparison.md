# Worked comparison: acceptance, review effort and cost

This example uses synthetic canonical runtime records and human-observation
fixtures. It is an arithmetic demonstration, not participant research or a
codex-web productivity benchmark. The regression
`test_baseline_candidate_worked_comparison_reports_sample_and_missing_data` in
`tests/test_outcome_metrics.py` reproduces it without external effects or model calls.

## Comparable task setup

Use the same bounded documentation objective and two matched test tasks per model.
Both cohorts use the explicit `documentation` workload and the same enrollment
window. The fixture provides exact native termination, tokens, recorded cost and
runtime duration. Its human actor reports current acceptance, correction and
cumulative review effort. Optional template attribution is absent in both cohorts
and remains visibly unrecorded; no template-effect conclusion can be drawn.

| Input per task | Baseline | Candidate |
| --- | --- | --- |
| Tasks | 2 | 2 |
| Native result | Both succeeded | Both succeeded |
| Human outcome | 1 accepted, 1 rejected | 2 accepted |
| Correction needed | Both | Neither |
| Review count | 1 | 1 |
| Interventions | 0 | 0 |
| Measured review effort | 60 seconds | 30 seconds |
| Tokens | 100 | 150 |
| Recorded cost | USD 0.01 | USD 0.02 |
| Runtime duration | 5 seconds | 5 seconds |

## Result

| Derived measure | Baseline | Candidate |
| --- | --- | --- |
| Accepted / eligible | 1 / 2 | 2 / 2 |
| Accepted outcome rate | 0.5 | 1.0 |
| Human correction rate | 1.0 | 0.0 |
| Review count total | 2 | 2 |
| Measured human review time total | 120 seconds | 60 seconds |
| Tokens per accepted outcome | 200 | 150 |
| Recorded USD per accepted outcome | 0.02 | 0.02 |
| Runtime seconds per accepted outcome | 10 | 5 |

The rejected baseline task contributes its usage to cost per accepted outcome.
The candidate consumes more tokens per attempted task while consuming fewer
tokens per accepted outcome in this fixture. These figures do not establish a
causal benefit, model preference, statistical significance or a routing decision.
Two synthetic tasks per cohort cannot represent product workloads or real users.

## Inspect and reproduce

Run the focused suite from a test checkout with runner-local data:

```bash
python -m unittest tests.test_outcome_metrics
```

In an isolated application with legitimate canonical runtime records, open
**Metrics → Human acceptance and review effort**, set the enrollment window and
choose **Inspect outcomes**. Review sample sizes, unresolved task counts, source
Evidence, model attribution and measurement omissions. Authorized human
administrators can record optional feedback with MFA, or explicitly supersede a
prior observation after reviewing its displayed target. Do not use production
records to manufacture the synthetic fixture or overwrite real user observations.

Choose **Record canonical Metrics** to preserve the server-calculated projection
and versioned observations. The returned Metric drill-down exposes the exact
definition revision, window, projection Evidence and source Evidence.

The suite separately covers unreviewed native success, pending/unknown outcomes,
reopened work, retries, duplicate requests, conflict resolution, tenant/Project
denial, stale windows and missing effort/cost. With missing cost or effort, the
corresponding value is null and the cohort is partial; the fixture does not
replace unavailable measurements with zero or infer effort from latency.

See [the calculation contract](../architecture/outcome-metrics.md) for denominator,
eligibility, governance and supersession semantics. Usability study findings and
human-calibrated semantic rubrics remain separate evidence requirements.
