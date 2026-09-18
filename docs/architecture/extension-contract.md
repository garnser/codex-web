# Extension contract foundation

Status: M3 foundation contract for issue #169. This document is normative for extension packaging and lifecycle until a typed SDK supersedes individual sections.

## Goals and invariants

codex-web uses one extension envelope for TaskSource, ActionProvider, model-provider, secret/config-backend, worker, and future provider categories. Installing code is never authorization. Extensions cannot bypass canonical identity, tenant, ActionIntent/ActionProvider, secret-reference, resource, evidence, or audit boundaries. UI contributions are declarative only and are never authorization truth.

The control plane owns extension state. A failed, incompatible, or quarantined extension must fail closed without destabilizing unrelated control-plane functions.

## Canonical manifest

Every package MUST expose an immutable manifest before activation. Unknown required fields or unsupported manifest schema versions fail discovery explicitly.

```yaml
schema_version: codex-web.extension/v1
id: com.example.github-task-source
version: 1.2.3
publisher:
  id: com.example
  name: Example, Inc.
provenance:
  source: oci://registry.example/extensions/github@sha256:...
  digest: sha256:...
  signature: sigstore:...
compatibility:
  codex_web: ">=3.0.0 <4.0.0"
types: [task_source]
capabilities:
  requested: [network.github.read, task.import]
events:
  subscribes: [task.sync.requested]
  publishes: [task.discovered]
configuration:
  schema: schemas/config.json
  secret_refs: [github_token]
resources:
  scopes: [tenant]
ui:
  contributions: []
migrations:
  entrypoint: migrations
health:
  timeout_seconds: 5
  interval_seconds: 60
entrypoints:
  task_source: extension.task_source:GitHubTaskSource
```

`id` is reverse-DNS-style, globally stable, and MUST NOT be reused for a different publisher/product. `version` uses SemVer. Package bytes are bound to `provenance.digest`; mutable tags are metadata only. Publisher/provenance verification follows the supply-chain policy from #139/#161. Deployment policy decides whether unsigned packages may be installed, but unsigned/untrusted status is always visible and auditable.

Compatibility is evaluated before configuration or code execution. `types` selects SDK conformance suites. Requested capabilities are declarations, not grants. Configuration contains typed non-secret values plus opaque secret references governed by #157/#132; plaintext secret material is never persisted in the manifest.

## Installation, authorization, and lifecycle

These are distinct auditable operations:

1. **discover** verifies manifest shape and package integrity without executing extension code.
2. **install** makes verified package bytes available and records provenance/compatibility. No capability is granted.
3. **configure** validates typed configuration and resolvable secret references without exposing secret values.
4. **authorize** records a tenant-scoped grant that is a subset of `capabilities.requested` and canonical operator policy.
5. **enable** is permitted only when compatible, configured, healthy enough for startup, and all mandatory capabilities are granted.

Canonical lifecycle states are `discovered`, `installed`, `configured`, `enabled`, `disabled`, `quarantined`, `upgrading`, `incompatible`, `deprecated`, and `removed`. Transitions are control-plane commands with actor, tenant, prior state, target state, reason, manifest digest, and resulting evidence/audit IDs. Restart/reconciliation MUST reconstruct state from canonical records rather than extension-local flags.

`quarantined` is a fail-closed state: no new extension calls are dispatched, existing leases/calls are cancelled at the provider boundary where safe, credentials are not exposed, and health probes may run only through the bounded health interface. Re-enable requires an explicit authorized transition after the quarantine cause is cleared.

## Capability and isolation model

Capabilities are namespaced strings with typed scope data in the authorization record. The effective capability set is the intersection of manifest requests, tenant grants, deployment policy, and runtime/provider restrictions. An extension MUST NOT mint or widen grants.

Extensions execute behind a host boundary appropriate to their type. The host enforces per-call timeout/cancellation, bounded payloads, network allowlists derived from granted capabilities, tenant context, secret handles, resource handles, and structured audit/evidence. Privileged mutations still flow through ActionIntent/ActionProvider; extension code cannot call a lower-level mutation path to evade approval or policy.

Repeated timeout, crash, protocol violation, integrity failure, or health failure feeds a bounded failure counter/circuit breaker. Opening the breaker prevents dispatch; policy may then quarantine the extension. Failures are attributed to the extension identity/version and do not mark unrelated providers unhealthy.

## Compatibility and upgrade

Compatibility uses the manifest's codex-web version range plus the version-skew rules in #138/#168. An incompatible extension is retained as installed metadata but cannot be enabled. Negotiation is deterministic: there is no best-effort fallback to an older protocol after a required contract mismatch.

Upgrade is staged: verify new package -> check compatibility -> validate migration plan -> enter `upgrading` -> quiesce old version -> run idempotent, versioned migrations -> health-check new version -> atomically switch active manifest digest -> enable. On pre-switch failure, the old version remains canonical. Post-switch rollback is allowed only when the migration declares rollback safety; otherwise the extension remains disabled/quarantined with explicit operator recovery evidence.

## Disable, uninstall, and references

Disable preserves configuration, grants, and canonical references but prevents dispatch. Uninstall first disables/quiesces the extension, then evaluates references to provider/resource identities it owns. Required live references block removal. Optional historical references are tombstoned with extension ID, version, digest, and human-readable type so audit/evidence remains interpretable. Removal never silently cascades deletion of canonical tasks, resources, approvals, or evidence.

## UI contributions

Permitted UI contributions are bounded declarative descriptors (navigation label, route identifier, form/schema reference, read-only status panel metadata). Arbitrary JavaScript, authorization predicates, secret access, and privileged mutation handlers are forbidden. Core UI derives authorization and lifecycle truth from canonical APIs.

## SDK contract

The common SDK host will expose `ExtensionContext` containing extension identity/version/digest, tenant identity, effective capability handles, typed configuration access, opaque secret handles, resource handles, structured logging, evidence/audit emitter, cancellation/deadline, and event publisher/subscriber handles. Provider-specific interfaces are adapters over this context.

Each extension type MUST pass the shared conformance suite: manifest validation; install-without-grant; denied-capability fail-closed behavior; tenant isolation; timeout/cancellation; health failure/circuit breaker; quarantine; incompatible-version rejection; upgrade migration idempotency; disable/uninstall reference handling; and secret non-disclosure. Reference implementations for at least TaskSource and ActionProvider MUST pass the same shared lifecycle suite before #169 is complete.

## Follow-up implementation slices

The next implementation should add typed manifest/lifecycle models plus schema validation, then a registry/repository and transition service, then host capability handles and conformance tests. After that, migrate one TaskSource and one ActionProvider as reference extensions, add quarantine/health behavior, and expose the bounded management API/UI. This sequencing keeps the contract testable without introducing a parallel authorization or secret model.
