# Extension and plugin lifecycle

## Status

Milestone 3 extension/platform contract.

This document defines the canonical package manifest, installation state,
authorization, compatibility and lifecycle boundary for third-party and
out-of-tree providers.

The central rule is:

> Installing extension code does not enable it and does not grant it authority.

## Package manifest vs canonical state

ExtensionManifest is immutable package-owned metadata validated before
extension code executes. It declares:

- reverse-DNS extension ID and SemVer package version;
- publisher and source provenance;
- SHA-256 package digest and optional signature metadata;
- supported codex-web extension-host version range;
- extension categories such as TaskSource, ActionProvider and model provider;
- requested and mandatory capabilities;
- subscribed/published event names;
- typed-configuration schema reference and logical secret slots;
- required resource scopes;
- bounded declarative UI contributions;
- migration and health-check metadata;
- category-specific runtime entrypoints.

The manifest never contains granted authority or raw secret material.

ExtensionInstallation is tenant/workspace-scoped canonical application state.
It records the installed manifest snapshot, package-verification result,
deployment mode, lifecycle, configuration-record references, secret-reference
bindings, health/circuit-breaker state, manifest history and operator-visible
failure reasons.

Capability grants are separate ExtensionCapabilityGrant records. This is
intentional: package declaration, installation, configuration, authorization
and enablement are independent auditable operations.

## Compatibility

The v1 extension host compatibility level is 3.0.0. This is a code-owned
extension-host contract version, not a marketing release number and not the
HTTP API contract version.

Manifest compatibility uses an intentionally small fail-closed grammar of exact
SemVer values and whitespace/comma-separated comparators, for example:

    >=3.0.0 <4.0.0

Unknown range syntaxes are rejected rather than guessed. An incompatible
package may be persisted in the incompatible lifecycle so an operator can
inspect why it cannot run, but it cannot be enabled.

## Local package discovery

Self-hosted package discovery uses a configured local package root. Each
immediate package directory has two files:

- manifest.json contains only the immutable ExtensionManifest declaration;
- payload.cwext contains opaque package bytes.

The manifest declares the SHA-256 digest of payload.cwext. Because the manifest
is outside the hashed payload, the control plane can parse metadata first and
then independently stream/hash the opaque payload.

Discovery is deliberately metadata-only:

- package count, manifest size and payload size are bounded;
- package directories and required files may not be symbolic links;
- resolved paths must remain below the configured package root;
- manifest JSON is validated before any runtime registration;
- payload.cwext is never extracted, imported or executed by discovery;
- the payload digest is computed server-side and compared with the manifest;
- malformed packages are returned as per-package discovery errors.

Package installation by package_ref re-runs discovery/hash verification before
persisting the installation, so a stale client-provided digest is never trusted
on that path. Package-based upgrades do the same and feed the freshly observed
manifest, digest result and package_ref into the same atomic upgrade lifecycle
used by the compatibility/manual path. The canonical installation retains the
package_ref for its current version, exact manifest history, manifest digest and
verifier result as provenance.

The older explicit manifest/observed-digest install API remains a self-hosted
administrative compatibility path; it does not satisfy hosted signature policy
and is not equivalent to server-observed package verification.

## Package integrity and signature policy

The package verifier is a code-owned boundary. The default self-hosted verifier
compares the observed package SHA-256 digest with the manifest declaration but
does not claim cryptographic signature verification.

- digest mismatch: install fails;
- invalid signature: install fails;
- self-hosted mode: unsigned or not-yet-verified signatures may be accepted
  explicitly, while their verification state remains visible;
- hosted mode: a cryptographically verified signature is required.

A manifest merely containing a signature string is not proof that the signature
was verified.

Future Sigstore/KMS/release-signing integrations must implement the verifier
interface rather than letting an API caller assert a verified state.

## Lifecycle

The canonical lifecycle vocabulary is:

- discovered;
- installed;
- configured;
- enabled;
- disabled;
- quarantined;
- upgrading;
- incompatible;
- deprecated;
- removed.

The initial service persists installs as installed or incompatible.
Configuration moves an installed extension to configured; configuration of a
disabled extension leaves it disabled.

Enablement requires all of the following:

- compatible host range;
- acceptable package verification for deployment mode;
- no unhealthy status;
- every mandatory declared capability has an active canonical grant;
- every declared required secret slot has a canonical secret reference;
- a typed configuration record is present when the manifest declares a
  configuration schema.

No extension may add a grant to itself. Human owners/admins or service actors
with extensions:admin own installation and authorization changes.

## Runtime registration

Package discovery does not load executable code. A separate trusted runtime
loader may register an already-loaded adapter through ExtensionRuntimeRegistry.

Registration is tenant-scoped and writes into the existing canonical
TaskSourceRegistry or ActionProviderRegistry rather than a shadow extension
dispatch table. The bridge validates exact extension ID, package version,
declared extension category and declared runtime capabilities before
registration.

TaskSource and ActionProvider registries support tenant-specific adapters while
retaining the existing global built-in fallback. This prevents one tenant's
extension from overwriting an identically named provider in another tenant.

Registration is process-local by design. After process restart, a trusted
loader must recreate runtime registrations from its executable/runtime source.
Canonical installation state, package provenance, grants, configuration,
health and audit remain persisted and authoritative.

Every actual dispatch re-checks current lifecycle and grants. A grant revoked
after registration therefore takes effect without restarting the process.
TaskSource dispatch uses the Work Item tenant/resource scope; ActionProvider
dispatch uses the authenticated binding/request tenant and target resources.

## Runtime authorization

ExtensionRuntimeDescriptor is the provider-neutral SDK/conformance seam used
before category-specific dispatch. It binds runtime code to:

- exact extension ID;
- exact installed package version;
- declared extension category;
- the capabilities the runtime intends to use.

The descriptor must match the installed manifest. Each attempted capability is
then resolved through ExtensionService.require_runtime_capability().

An extension must be enabled, the category must be declared, and an active
grant must exist. Resource-scoped grants additionally require the runtime target
resources to be a subset of the granted resource set.

This same boundary applies to TaskSource, ActionProvider, model-provider and
future extension categories. Category-specific conformance suites may add
protocol checks after this shared lifecycle/authority gate.

## Secrets and configuration

Manifest secret_refs are logical slot names only. Installation state maps those
slots to canonical SecretBroker reference IDs. Configuration validates that
every supplied slot was declared and that the requesting actor may use the
referenced secret.

Raw secret values are never stored in extension state or returned by extension
administration APIs.

Typed configuration is referenced by canonical configuration-record ID. The
extension registry does not create a second configuration system.

## Health and quarantine

Health state is canonical metadata, not an authorization source.

The reference circuit breaker quarantines an enabled extension after three
consecutive unhealthy reports. A quarantined extension cannot pass runtime
capability resolution.

Clearing quarantine is a separate administrator operation and leaves the
extension disabled; it must pass normal enablement gates again.

Reconfiguration also requires disable-first. Configuration changes cannot
silently move a live extension out of enabled state while its process-local
runtime registration remains attached.

ExtensionService emits post-commit lifecycle notifications. ExtensionRuntimeRegistry
subscribes to those notifications and removes TaskSource/ActionProvider runtime
registrations whenever an installation leaves enabled state through disable,
quarantine, upgrade or removal. Dispatch wrappers still re-check canonical
lifecycle/grants themselves, so listener cleanup is defense-in-depth rather than
the authorization source. Listener failures cannot roll back committed lifecycle
state and are retained as bounded in-memory diagnostics.

Revoking a mandatory capability grant from an enabled extension immediately
quarantines it, preventing code from continuing with authority that no longer
exists.

## Upgrade and removal

Upgrade is only allowed while the extension is not enabled.

An upgrade:

- requires the same extension ID and a different package version;
- re-runs digest/signature policy and compatibility checks;
- retains the previous manifest in immutable history;
- revokes grants for capabilities no longer requested;
- drops secret-slot bindings no longer declared;
- resets health state;
- returns to disabled when compatible or incompatible otherwise.

If the new manifest declares a migration entrypoint, the upgrade must reference
a valid canonical PASS Evidence record for the exact extension ID and target
version before the manifest replacement commits. The extension registry does
not trust a caller-supplied completion boolean. A package execution layer should
run migrations through the isolated worker boundary and publish the resulting
Evidence rather than executing migration code inside the control-plane process.

Removal uses a tombstone. Active grants are revoked and the installation moves
to removed. Hard deletion is intentionally unsupported until a canonical
reference graph can prove there are no retained Work Item, ActionProvider,
resource, evidence or audit references.

## Failure isolation

Extension lifecycle state is independent from provider-specific runtime
objects. A broken, incompatible, unhealthy or quarantined extension can be
disabled without corrupting unrelated control-plane functions.

Extension code does not gain direct access to canonical database state,
credentials, worker authority or ActionIntent authority by virtue of being
installed or enabled. Privileged external side effects must still flow through
ActionProvider/ActionIntent and canonical identity/resource/secret boundaries.

## UI impact

Platform administration UI work is tracked by #141/#125. The UI should expose:

- extension identity, publisher and provenance;
- digest/signature verification state;
- compatibility and current version;
- declared vs granted capabilities;
- resource scopes;
- configuration and secret-reference metadata without secret values;
- health, lifecycle, quarantine and failure reason;
- upgrade history and removal tombstone;
- audit events and links to provider/resource/action usage.

Installation, configuration, authorization and enablement must remain visibly
distinct operations.
