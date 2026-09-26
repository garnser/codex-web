# UX telemetry and friction metrics

codex-web has a separate, privacy-bounded product-experience telemetry layer for usability analysis. It is **not** canonical application state, audit evidence, runtime observability, prompt logging, or a source of authority.

The taxonomy version is `1.0`.

## Policy

Collection is disabled by default.

- `ux.telemetry.enabled` — boolean feature flag, default `false`, configurable at deployment, organization, or workspace scope. It is kill-switch capable.
- `ux.telemetry.retention_days` — integer retention window, default 30 days, allowed range 1–365 days.

The effective policy is resolved through the normal configuration hierarchy. A telemetry failure never blocks product work, changes canonical state, or grants authority.

## Closed event taxonomy

The API accepts only the fields defined by `UxTelemetryEventCreate`:

| Field | Meaning |
| --- | --- |
| `event_name` | Stable event enum |
| `workflow` | Stable coarse workflow enum |
| `step` | Optional workflow-specific allowlisted step |
| `route_group` | Optional coarse route enum |
| `duration_ms` | Bounded user-visible duration |
| `retry_count` | Bounded retry count |
| `journey_id` | Random session correlation token; not an identity |

Event names in taxonomy v1 are:

- `onboarding_started`, `onboarding_step_completed`, `onboarding_completed`, `onboarding_skipped`
- `workflow_started`, `workflow_completed`, `workflow_abandoned`
- `validation_error_shown`
- `action_started`, `action_succeeded`, `action_failed`
- `recovery_action_used`
- `route_transition`
- `contextual_help_used`, `empty_state_cta_used`, `feature_discovery_used`

Workflow names are `onboarding`, `project_setup`, `automation`, `agent_management`, `thread_execution`, and `navigation`. Steps are code-owned allowlists per workflow; arbitrary user-entered step names are rejected.

## Data that must never be emitted

The schema intentionally has no arbitrary metadata or payload field. It cannot represent:

- prompt or message bodies;
- repository names, paths, file contents, diffs, or generated content;
- raw URLs containing resource identifiers;
- form values, descriptions, instructions, change reasons, or user-entered names;
- credentials, tokens, cryptographic material, or `SecretReference` values;
- human/service identity IDs;
- Project, Thread, Work Item, Agent, Team, Automation, or resource IDs.

Tenant organization/workspace scope is attached server-side from the authenticated request. The random `journey_id` exists only to correlate coarse interaction sequences and is not an identity.

Pydantic uses `extra="forbid"` for ingestion. Sensitive or arbitrary fields therefore fail validation instead of being silently retained.

## Instrumented workflows

Taxonomy v1 qualifies the following end-to-end surfaces:

1. **First-value onboarding** — starts when canonical Project readiness is execution-ready, records first work appearing, completes when canonical recently-completed work appears, and records skip separately. Completion duration is time to first meaningful outcome.
2. **Project Setup** — records apply start, canonical execution-ready completion, failure/needs-attention, and explicit safe retry/recovery.
3. **Automation** — records manual run start and distinguishes admitted/canonical completion from denied or failed launch.
4. **Agent / Team management** — records contextual create/edit start, completion, client validation errors, and server-side action failure.
5. **Navigation** — records only coarse route groups so repeated A→B→A backtracking can be counted without storing raw URLs or resource IDs.

## Stable metric definitions

These definitions are versioned with taxonomy v1 so before/after UX changes remain comparable.

- **Activation completion rate** = `onboarding_completed / onboarding_started`.
- **Time to first meaningful outcome** = median `duration_ms` on `onboarding_completed`.
- **Workflow completion rate** = `workflow_completed / workflow_started` for each non-onboarding workflow.
- **Failure count** = `action_failed` events per workflow/step.
- **Validation friction** = `validation_error_shown` events per workflow/step.
- **Recovery use** = `recovery_action_used` events per workflow/step.
- **Abandonment** = explicit `workflow_abandoned` or `onboarding_skipped`; failure is not silently reclassified as abandonment.
- **Highest-friction step** = workflow/step with the largest combined count of abandonment, failure, validation-error, and recovery events.
- **Navigation backtrack** = an A→B→A sequence of coarse route groups within one journey where A and B differ.
- **Median completion duration** = median `duration_ms` of successful workflow completions.

## Inspection and export

Operators with Administration authority, or services with `ux-telemetry:read`, can use:

- `GET /api/ux-telemetry/summary` — derived activation, workflow, friction, duration, route distribution, and backtracking metrics.
- `GET /api/ux-telemetry/events` — bounded JSON export of the closed event records.

Both accept `start_at` and `end_at` on the server-side query path where applicable. Use two non-overlapping windows to compare a UX change before and after without changing metric definitions.

For example, query a baseline window and a post-release window and compare:

- `activation.completion_rate`;
- `activation.median_time_to_first_meaningful_outcome_ms`;
- `workflows.<workflow>.completion_rate`;
- `highest_friction_step`;
- `navigation.backtracks`.

## Retention

Events outside the effective retention window are excluded from reads and purged during ingestion for that tenant. The store also enforces a global 20,000-event safety bound. UX telemetry is intentionally lossy product analytics; it must never be used as durable audit evidence.

## Development rules

When adding telemetry:

1. reuse a stable event/workflow/step already in the taxonomy where semantics match;
2. add a new code-owned enum/step only when the distinction is required for a metric;
3. never pass errors, prompts, labels, IDs, form data, object names, raw paths, or arbitrary metadata;
4. do not derive authority or canonical completion from telemetry;
5. keep the browser collector fail-closed and non-blocking;
6. add a privacy regression test whenever the event contract changes.
