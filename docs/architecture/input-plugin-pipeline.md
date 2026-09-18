# Input plugin composition pipeline

The input plugin pipeline is a deterministic pre-provider composition boundary. It lets bounded plugins normalize, enrich, compose, optimize, or provider-decorate model input without making plugin output authoritative for identity, policy, approval, secrets, sandboxing, tenant scope, or provider execution.

The core rule is:

> Input plugins may change how a request is expressed. They do not change what codex-web is authorized to do.

## Pipeline phases

Plugins execute in this fixed phase order:

1. `normalize`
2. `enrich`
3. `compose`
4. `optimize`
5. `provider_decorate`

Within a phase, configured order is deterministic and ties are resolved by plugin ID/version. The engine does not ask a model how to order or authorize plugins.

## Field classes

The engine owns three mutation classes.

### Composable

Plugins may directly propose and apply these fields after schema/size validation:

- system prompt
- messages
- context blocks
- text verbosity
- output-contract guidance

These affect model input only. They never become authority or policy evidence.

### Gated

Plugins may propose changes to:

- model class
- reasoning effort
- output-token budget
- cost budget
- preferred providers

A proposal is inert unless a code-owned validator accepts it. The default validator rejects every gated proposal. This makes "plugin suggested a larger budget/model" different from "core accepted that request."

The model gateway then runs its normal tenant policy, residency/compliance, capability, provider-health, context-window, pricing, cost, and fallback checks on the effective request.

### Protected

Plugins can never mutate tenant/workspace/actor identity, prompt-template pinning, required capabilities/residency/compliance, timeout/fallback policy, Work/Goal/Decision/execution attribution, purpose, authority/policy/approval references, sandbox, secrets, or service scopes.

Protected **and unknown** field mutations always fail closed. A plugin's configured failure policy cannot weaken this structural rule.

## Failure behavior

Each registration declares one failure policy:

- `fail_closed`: plugin failure aborts composition;
- `fail_open`: record a metadata-only failure and continue with the unchanged input;
- `skip`: record the failed plugin as skipped and continue unchanged.

No policy permits partially trusted plugin output. Security-classification violations always fail closed.

Plugin exception messages are not persisted in provenance because they may contain prompt or secret-derived content. Only the exception type is retained.

## Bounds and token efficiency

Each plugin has explicit maximum patch bytes and maximum added input characters. The engine records estimated token counts before and after each plugin using deterministic character accounting. This is measurement, not automatic optimization/rerouting.

A plugin does not get an implicit right to expand cost/output budgets. Gated budget changes remain proposals until core validation.

The no-plugin pipeline is an identity function. Existing provider execution is therefore unchanged when no plugins are configured.

## Provenance

Each plugin step records metadata only:

- plugin ID/version/transport;
- phase;
- exact Definition Registry reference when supplied;
- input/patch/output SHA-256;
- outcome;
- applied fields;
- gated proposals/rejections;
- warnings;
- character/token estimates;
- start/completion/duration.

Prompt/message/context bodies are deliberately excluded.

The model-gateway durable contract is versioned to `1.1` to attach this provenance and gated-proposal hashes to invocation records. Existing `1.0` state migrates with empty plugin provenance.

## Trust boundary at the model gateway

The asynchronous model invocation path composes input before deterministic routing. Plugin-added context blocks and output-contract guidance are wrapped as `TOOL_OUTPUT` untrusted data before they are sent to a model. They may influence model wording, but they cannot grant policy/authority or satisfy approvals.

Provider credentials remain SecretBroker references until the provider boundary. The pipeline never receives provider secret material.

## Definition Registry and transports

This core slice intentionally keeps the **engine and security invariants in code**. Registration/order/conditions/settings belong in versioned Definition Registry data in the next slice.

Future adapters may include built-in, SKILL.md, command, HTTP, and MCP transports. External transports must project only the minimum required input and retain the same timeout/output-size/protected-field constraints. They must not receive secret values or gain authority through transport choice.

Prompt Master is a reference integration target, not a required dependency and not part of the execution kernel.
