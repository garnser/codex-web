# Entitlements, quotas, and usage metering

## Status

Canonical entitlements and quota foundation for issue #164.

This contract answers a product/service question that is deliberately separate from authorization:

> Is this tenant entitled to this capability, and is the requested consumption within its configured service quota?

An entitlement can never grant human RBAC, agent authority, provider permission, resource access, or policy approval. A caller must pass both the normal authorization/policy path and the entitlement boundary.

## Modes

Every tenant/workspace resolves one explicit mode:

- `self_hosted_unlimited` — compatibility/default mode for self-hosted installations. Capability and quota checks allow execution without commercial configuration.
- `enforced` — capability entitlements and configured quotas are evaluated deterministically.

Missing tenant settings resolve to `self_hosted_unlimited`, preserving existing installations without silently inventing a plan.

## Capabilities

Capability entitlements are tenant/workspace scoped and carry:

- stable ID and capability key;
- enabled/disabled state;
- source/provenance label;
- optional activation and expiry timestamps;
- actor and update timestamp.

In enforced mode, a missing, disabled, not-yet-active, or expired capability is denied before the caller reaches an expensive/provider action where the integration supports preflight.

## Quotas

Quota policies are independent of capability entitlement and authorization.

Policies define:

- metric key;
- numeric limit;
- aggregation window: lifetime, hour, day, or calendar month in UTC;
- behavior: hard stop, degraded, grace, or notify;
- warning fraction;
- source/provenance.

A hard-stop quota denies projected consumption when it would exceed the configured limit. Degraded/grace/notify policies surface the exceeded state but remain allowed so the owning product workflow can apply its documented behavior.

## Usage events

Usage is append-only and contains attribution metadata, never prompt/secret content:

- tenant/workspace;
- stable idempotency key;
- metric and numeric amount;
- event and receipt timestamps;
- actor/source;
- optional project, resource, Work Item, and ActionIntent references.

Idempotency is tenant-scoped. Replaying the same logical event does not double count, even if entitlement configuration changed after the original event was accepted. Reusing an idempotency key with different metric/amount/attribution is rejected as a conflict rather than silently rewriting accounting history.

Aggregation uses `occurred_at`, not arrival time, so late events are assigned to the window in which the usage actually happened. Duplicate and late records can therefore be reconciled without inventing new consumption.

## Atomic consumption

`EntitlementService.consume` evaluates capability + quota and records the usage event in one SQLite transaction. This prevents two concurrent local consumers from both observing the same remaining quota and independently exceeding a hard limit.

Idempotent retries reuse the prior event rather than charging twice.

## ActionIntent enforcement

The first canonical enforcement integration is external side-effect execution:

- creation rechecks the `external_actions` capability;
- immediately before provider execution, the ActionIntent service atomically consumes one `external_action_attempts` unit;
- a hard quota denial happens before provider execution and releases the worker lease as a retryable failed intent;
- the usage key is `action-intent:<id>:attempt:<n>`, so retries meter distinct external attempts while replay of the same attempt is idempotent.

This entitlement check is **in addition to** identity, authority, policy, security-boundary, resource, credential, and provider checks. Enabling `external_actions` cannot grant action authority.

## API

```text
GET /api/entitlements/status
GET|PUT /api/entitlements/mode

GET /api/entitlements/capabilities
PUT /api/entitlements/capabilities/{capability}

GET /api/entitlements/quotas
PUT /api/entitlements/quotas/{metric}

GET /api/entitlements/usage
POST /api/entitlements/usage
POST /api/entitlements/usage/reconcile
GET /api/entitlements/usage/export
```

Administrative mutations require a tenant owner/admin or a service principal with `entitlements:admin`. Direct usage ingestion requires `entitlements:meter` or `entitlements:admin`; internal canonical integrations use the transactional consume boundary after their own authorization checks.

## Model usage metering

The model gateway emits successful provider usage through the same canonical usage ledger using metadata-only metrics:

- `model_input_tokens`;
- `model_output_tokens`;
- `model_cost_usd` when registry pricing plus provider usage permit an actual-cost calculation.

Usage idempotency is keyed by canonical model invocation ID plus metric. Model metering does not copy prompts, responses, provider credentials, or arbitrary model metadata into entitlement records. A metering sink failure does not rewrite a provider-successful model invocation; reconciliation can repair usage independently.

## Privacy and governance

Meter events intentionally do not contain prompt text, response text, secret values, request payloads, or provider credentials. They are quantitative attribution records.

Future hosted billing/export adapters consume this canonical boundary rather than becoming the entitlement source of truth.

## UI impact

Issue #141/#125 should expose:

- current mode and capability entitlements;
- quota policy and consumption;
- warning/exceeded state and deterministic denial reason;
- reset window;
- self-hosted/unlimited state distinctly from permission/policy.

The UI must not imply that entitlement equals authorization.
