# Canonical Metrics and KPI Observations

Codex-web treats metrics as deterministic, provenance-aware canonical state used by Goals, Decisions, Executive reasoning, evidence, and later business operating views. A metric is not reconstructed from chat, dashboards, or model output.

## Boundary

The canonical layer owns:

- `MetricDefinition`: stable tenant/workspace identity, unit/type, deterministic aggregation/window semantics, freshness policy, owner, scope, permitted sources, and optional thresholds.
- `MetricObservation`: immutable measured input with exact metric revision, observed/window timestamps, source/provider, external record reference, evidence references, and an idempotency key.
- `MetricSnapshot`: immutable evaluation record containing the exact metric revision and observation IDs used at a point in time.

The derived **current value** is calculated deterministically from canonical observations. It is convenient query state, not a second source of truth. A later observation may change the current value but cannot rewrite an already captured snapshot.

## Deterministic evaluation

Known metric state never invokes an LLM. The service validates the declared value type and unit, rejects disallowed sources, bounds history reads, and supports deterministic last value, sum, average, minimum, maximum, and count aggregation.

Freshness is explicit:

- `fresh`: observations satisfy the definition's freshness policy.
- `stale`: observations exist but the newest contributing observation is older than policy allows.
- `missing`: no observation contributes to the requested window.
- `partial`: at least one contributing observation explicitly reports incomplete input.

Callers must not treat stale, missing, or partial data as current fact merely because an aggregate value is present.

## Idempotency and provenance

Observation ingestion requires a caller-supplied idempotency key. Repeating the same key and content returns the existing canonical observation; reusing a key with different content fails closed.

An observation can retain provider/source identity, an external record reference, and canonical Evidence IDs. Raw provider payloads are not copied into metric state.

## Goal and Decision use

Goal success criteria remain backward compatible with the legacy `metric_key`, but new criteria may bind `metric_id`, an optional exact `metric_snapshot_id`, and an optional metric window.

Decision work should capture a `MetricSnapshot` when a measured value materially influences deliberation or approval. This preserves historical reasoning context even after newer observations arrive.

Metric snapshots are evidence references; they do not grant authority. Decision approval and external effects continue through canonical ApprovalRequest, Work, ActionIntent/ActionProvider, and Evidence paths.

## Security and tenancy

Every definition, observation, and snapshot is tenant/workspace scoped. API mutations require an MFA-backed administrator or a service identity with `metrics:admin`. Read access uses the authenticated tenant boundary.

Future ingestion adapters must apply data-governance/classification requirements before exporting or persisting sensitive measurements. Domain-specific business KPI catalogs belong to M13; this M8 layer stays generic.

## UI

The Metric/KPI explorer reads the same canonical API as other consumers. It distinguishes derived current state from immutable snapshots and displays freshness, source/provider, external references, Evidence IDs, definition revision, and scope. It does not maintain client-side metric truth or perform metric calculations in the browser.
