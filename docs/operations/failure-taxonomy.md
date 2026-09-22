# Failure taxonomy and retry contract

Codex Web uses one versioned, machine-readable failure contract across model providers, agent runtimes, execution workers, Work Items, and external ActionIntents.

Contract: `failure-taxonomy/1.0`.

A failure record contains stable low-cardinality classification plus provenance. It must not be treated as a place to copy arbitrary exception/request content.

## Categories

- `provider_model`: model/provider authentication, quota, capacity, server/network, context and output failures.
- `runtime_worker`: runtime availability/compatibility, worker lease/lifecycle, sandbox, resource, timeout/stall, workspace and process failures.
- `control_policy`: authority, approval, budget, capability, dependency, configuration, stale-revision, cancellation/supersession and unknown canonical outcomes.
- `external_action`: external provider auth/rate-limit/conflict, unknown action timeout outcomes and verification failures.
- `unknown`: safe fallback when a source has not yet been mapped.

Reason codes are defined in `codex_web.failures.FailureReason`. Clients should compare the reason code, category and retryability values, not human-readable messages.

## Retryability

Every reason has exactly one retry contract:

- `transient`: automatic retry/fallback is permitted subject to the owning retry budget/idempotency rules.
- `after_remediation`: do not automatically retry. An operator/configuration/capacity change is required first.
- `reconcile_required`: the prior outcome is unknown or canonical/external state may disagree. Reconcile/verify before any replay.
- `not_retryable`: terminal by contract.

`FailureRecord.automatic_retry_allowed` is true only for `transient`.

### External actions

Unknown external side-effect outcomes are intentionally stricter than ordinary runtime failures. A timeout after provider execution begins is classified as `action_timeout_unknown_outcome` and `reconcile_required`.

The ordinary ActionIntent retry endpoint refuses `UNCERTAIN`, `REQUIRES_RECONCILIATION`, and reconcile-required classifications even when the provider supports idempotency. An explicit reconciliation pass may re-arm a provider-idempotent intent after checking durable receipts/provider state.

This prevents blind replay of an external side effect whose outcome is unknown.

## Provider/model mappings

Examples:

| Source evidence | Reason | Retryability |
| --- | --- | --- |
| HTTP 401/403 | `provider_auth_or_access` | after remediation |
| quota/credit exhaustion | `provider_quota_exhausted` | after remediation |
| 429/provider throttling | `provider_capacity_or_rate_limit` | transient |
| provider 5xx | `provider_server_error` | transient |
| connection/timeout | `provider_network` | transient |
| unknown/unavailable model | `model_unavailable` | after remediation |
| context window exceeded | `context_overflow` | after remediation |
| malformed/empty output | `malformed_or_empty_model_output` | transient |

Provider adapters attach the reason to their structured exceptions; ModelGateway persists the same `FailureRecord` on the invocation attempt and propagates it on final provider-unavailable errors.

## Worker/runtime mappings

Examples:

| Source evidence | Reason | Retryability |
| --- | --- | --- |
| expired/lost lease or stale fence | `worker_lease_lost` | transient |
| offline runtime/worker | `runtime_offline` | transient |
| revoked/quarantined worker | `worker_revoked_or_quarantined` | after remediation |
| sandbox unavailable | `sandbox_unavailable` | after remediation |
| OOM/resource limit | `resource_limit` | after remediation |
| execution timeout/stall | `execution_timeout` / `execution_stalled` | transient |
| workspace setup failure | `workspace_prepare_failed` | after remediation |
| missing executable | `runtime_missing_executable` | after remediation |
| unsupported capability | `capability_unavailable` | after remediation |

ExecutionWorker retry of LOST/FAILED assignments is allowed only when the canonical (or deterministically mapped legacy) failure is transient.

## Policy/control mappings

Authority/security denial is `authority_denied`. Missing/expired approvals are `approval_required_or_expired`; budget exhaustion is `budget_exhausted`; missing capability/dependency/configuration use their corresponding stable codes.

These are remediation failures, not transient retry signals.

## Work Item compatibility

Work Item execution retains its legacy `category`, `code`, `message`, and `retryable` projection for compatibility and now embeds a canonical `failure_reason.canonical` record.

Known historical aliases such as `provider/rate_limited`, worker lease/sandbox/timeout codes, and action conflicts are mapped deterministically. Unknown legacy values fall back safely to `unclassified`.

## Secret safety

Canonical failure records never persist raw exception text by default. Human summaries come from the taxonomy definition. Provider/runtime native code and status are bounded structured fields.

The `details` map:

- allows only scalar bounded values;
- drops keys shaped like token, secret, password, authorization, cookie, credential, or private-key material;
- is not a substitute for raw logs or request/response capture.

Legacy fields may remain for compatibility, but new consumers should use the canonical record.

## Metrics and UI

`FailureRecord.metric_labels()` returns only:

- category;
- reason;
- source subsystem;
- retryability.

It deliberately excludes execution IDs, workers, providers and other high-cardinality identifiers.

Run Timeline, Attention Inbox, dashboards and remediation UI should render the same `reason_code`, `summary`, `retryability` and `remediation_key` rather than translating free-form messages independently.

`failure_taxonomy_snapshot()` exposes the complete versioned set of definitions for code-owned consumers/tests.
