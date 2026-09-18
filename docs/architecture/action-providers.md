# Action providers and external side effects

Codex-web external mutations use a provider-neutral `ActionProvider` contract. Core autonomy, Executive, Goal, Decision, and Work Item logic must not consume provider-native request/response objects.

## Contract

ActionProvider contract version **1.0** declares a provider type/instance and a catalog of `ActionDefinition` records. Each action describes, before execution:

- risk class;
- required canonical resource types;
- required authority strings;
- whether a credential reference is required and its purpose;
- prepare/execute/dry-run/idempotency/rollback/verification/progress/evidence capabilities;
- timeout and retry bounds;
- reversibility;
- expected evidence.

Unsupported capabilities raise `UnsupportedActionCapabilityError`; callers must never guess support from provider type.

## Invocation model

`ActionRequest` contains only canonical scope and references:

- organization/workspace;
- optional Project;
- canonical Resource IDs;
- structured provider-neutral parameters;
- optional secret reference;
- optional idempotency key;
- dry-run/correlation/requester metadata.

Raw secret material is forbidden from the request. The execution service resolves a secret reference through the credential broker only inside the provider execution callback.

A provider binding may scope one provider instance to a workspace, Project and/or canonical Resources. Resource-scoped bindings require explicit targets, and requests cannot escape the configured resource set. A configured binding credential cannot be silently overridden per request.

## Registry and configuration

`ActionProviderRegistry` owns provider implementation registration and persisted `ActionProviderBinding` configuration. Implementations live in code; mutable provider bindings live in versioned SQLite state.

Resolution fails closed when:

- a provider implementation is not registered;
- a binding is missing, disabled or belongs to another tenant;
- Project or Resource scope differs;
- required Resource types are absent;
- required credential references are absent;
- requested capabilities are unsupported.

Provider/action risk and authority requirements are visible during `prepare`. Central authority enforcement is layered by the authority/policy milestone rather than duplicated inside provider implementations.

## Execution lifecycle

`ActionExecutionService` is the single provider-neutral side-effect boundary:

1. resolve binding/provider/action;
2. validate tenant/Project/Resource scope;
3. validate declared capabilities and credential references;
4. prepare a provider-neutral plan;
5. execute through the provider, resolving secret material only at the callback boundary;
6. verify when the capability exists;
7. rollback only when declared reversible and rollback-capable.

Autonomy exposes these same prepare/execute/verify/rollback methods through the service and does not call provider-native side-effect APIs directly.

## Evidence and results

Providers return `ActionResult`, `ActionVerification`, and `ActionEvidence` models. Provider-native response bodies must be normalized into these structures before they reach core logic.

Evidence is reference/summary/metadata oriented and must not contain credentials.

## Reference provider and conformance

The in-memory `ReferenceActionProvider` implements a reversible `reference.set` action with dry-run, idempotency, verification, rollback, and evidence. `ActionProviderConformanceSuite` validates contract version, unique action IDs and capability semantics, then exercises a provider through the shared lifecycle.

New real providers must pass the same suite before they are wired into autonomy.

## API and UI

The administration API exposes provider/binding catalogs and prepare previews:

- `GET /api/action-providers`
- `GET/POST /api/action-providers/bindings`
- `GET /api/action-providers/bindings/{binding_id}`
- `POST /api/action-providers/bindings/{binding_id}/prepare`

Direct execution remains a service-level boundary until central authority/action-intent enforcement is composed. #141 should project provider capabilities/bindings and requirements from these canonical APIs rather than hard-code provider features in the UI.
