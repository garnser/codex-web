# Extension developer guide

Extensions add provider/integration/UI behavior behind codex-web compatibility,
identity, authority and lifecycle boundaries. Installing an extension never
grants its requested capabilities automatically.

## Manifest

An extension manifest should declare:

- stable extension ID and version;
- publisher/provenance metadata;
- codex-web compatibility range;
- entry points/contributions;
- requested capabilities;
- configuration schema;
- migration metadata;
- optional UI contributions.

Treat the manifest as untrusted package metadata until digest/signature/
provenance verification succeeds.

See [Extension contract](../architecture/extension-contract.md).

## Compatibility

Before publication test against every codex-web version range you claim to
support. Incompatible host/API/schema versions must fail visibly.

Do not use feature detection to silently reinterpret a contract with different
security semantics.

## SDK and conformance testing

Extension code should use shared provider/extension contracts instead of direct
access to control-plane storage or private service internals.

Conformance tests should cover:

- manifest/schema validation;
- lifecycle install/enable/disable/quarantine/remove;
- configuration validation;
- permission/capability grants and revocation;
- upgrade/migration paths;
- provider/action idempotency and receipts where applicable;
- secret references without raw-value exposure;
- compatibility rejection;
- uninstall/reference cleanup.

## Provenance and signing

Packages should have a stable digest and verifiable publisher/signature metadata
when the deployment policy requires it. Admin UI should show verification state,
not key material.

A changed package digest is a different artifact even if its semantic version is
unchanged.

## Installation vs authority

Keep these separate:

1. package discovered/downloaded;
2. package verified;
3. extension installed;
4. extension configured;
5. requested capabilities reviewed;
6. capabilities granted by canonical authority;
7. extension enabled.

An extension may not self-grant new authority during installation, startup or
migration.

## Configuration and secrets

Typed extension configuration belongs in canonical configuration state. Secret
values belong in the Secret boundary and are referenced by ID.

Do not store a token/password/private key in the extension manifest, Definition,
logs, browser storage or ordinary configuration.

## Lifecycle

Supported lifecycle operations should remain auditable:

- install;
- configure;
- enable/disable;
- upgrade;
- quarantine;
- rollback where compatible;
- uninstall/remove.

Quarantine is the safe default when compatibility/provenance/runtime behavior is
unsafe or unknown.

## Migrations

Declare whether a migration is compatible/idempotent/reversible. Version data
explicitly. For destructive or irreversible migrations, integrate with the
platform upgrade/recovery approval boundaries rather than implementing a private
extension-only bypass.

## UI contributions

UI contributions must:

- use canonical APIs;
- expose canonical vs provider-owned state clearly;
- respect identity/step-up/authority enforcement performed server-side;
- mask secret/key material;
- handle denied/degraded/quarantined/incompatible states;
- remain keyboard/responsive;
- provide contextual documentation/help.

Do not ship client-side authorization truth.

## Publication expectations

Before publishing an extension:

- run conformance/browser tests;
- document required capabilities/configuration/secrets;
- state compatibility range;
- document upgrade/rollback/uninstall behavior;
- include troubleshooting guidance;
- identify external side effects and reconciliation semantics;
- provide sanitized examples/demo fixtures where appropriate.

See [Extensions architecture](../architecture/extensions.md) and
[Security/trust boundaries](../architecture/security-trust-boundaries.md).

## BusinessDataSource extensions

Read/sync business-system integrations should implement the
[BusinessDataSource contract](../architecture/business-data-sources.md) rather
than direct database writes. Declare only supported discovery/read/event/delta
capabilities, normalize minimum-sufficient scalar fields, use SecretReferences
for provider credentials, and keep all external mutation in
ActionProvider/ActionIntent implementations. Register the adapter factory only
while the extension is compatible and enabled; quarantine must make the adapter
unavailable to synchronization.
