# Worked example: Goal decomposition, Executive analysis and Decision

## Scenario

Northstar Labs creates the Goal:

> Reduce checkout API p95 latency below 300 ms without increasing error rate.

## Decompose the Goal

Deterministic/product structure should separate the outcome from candidate work:

```text
Goal
 ├─ measure current latency/error KPI
 ├─ investigate database/cache bottleneck
 └─ evaluate candidate change
```

The Goal does not become “done” because an agent completed a task. The measured outcome is separate.

## Executive analysis

An Executive/Product/Engineering role may recommend options such as cache tuning or query changes. That output is advisory until it is turned into canonical work or a Decision.

Useful Executive output includes:

- assumptions;
- options/trade-offs;
- evidence needed;
- proposed next action.

It must not grant itself deployment authority.

## Create a Decision

Suppose evidence supports adding a bounded cache.

A Decision records the chosen option, rationale, provenance and related Goal. It may create Work Items, but the Work Items still follow their own lifecycle and authority rules.

## Intentionally rejected Decision

Security review finds the proposed cache would store restricted data without an approved encryption boundary.

Healthy behavior:

- Decision remains rejected/superseded or blocked as appropriate;
- the finding is preserved;
- implementation is not created merely because an Executive model preferred the option.

## Verify outcome

After implementation/deployment, compare KPI observations against the Goal. LLM arithmetic or prose does not replace canonical Metric observations.
