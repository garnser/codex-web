# Skill Catalog and assignment contract

GitHub issue `#961` establishes the managed path from an external Skill source to a reviewed, revision-pinned execution input.

## Canonical ownership

Skill content remains an `agent.skill` Definition Registry record. The registry owns immutable revisions, draft/publish/supersede lifecycle, checksums, tenant scope, and exact references. Categories and tags are Definition payload data so operators can extend the taxonomy without a source-code change.

Skill Sources are canonical operational state. A source records its adapter type, location, selected ref, trust decision, lifecycle, synchronization result, and mappings from upstream identifiers to imported Definition records. Source configuration is separate from the imported Definition and cannot publish it.

The catalog-bundle adapter accepts a bounded typed payload containing multiple Skill manifests and an immutable upstream revision. It normalizes each manifest into a Definition draft with source ID, upstream identifier, source revision, digest, and import time. Adapter input is untrusted data. It cannot grant authority, change sandbox/network/resource scope, expose credentials, or execute helper assets.

## Synchronization and review

Synchronization is deterministic and does not call a model. Repeating the same upstream digest is idempotent. A changed digest creates a new inactive draft only when the latest canonical record is still the record produced by the previous sync. A newer local revision blocks synchronization, requiring an operator to choose an override or fork rather than silently losing local work.

Source trust and Skill publication are distinct. Approved source metadata does not make imported instructions executable. A human with canonical Skill authority must review and publish the exact draft before it can be assigned.

Imported drafts pass through the revision-bound security scanning and policy gate described in [skill-security.md](skill-security.md). A changed upstream revision cannot reuse an earlier scan decision.

## Thread assignment and execution

Thread assignments are persisted in canonical Thread execution settings as exact `DefinitionReference` values. The Thread Skills API validates tenant visibility, published history, active lifecycle, effective window, checksum, and schema before saving. The API projects explicit and inherited assignments separately and returns the effective set with origin.

The execution assignment stores the exact effective Skill references and their assignment sources. Worker capability selection includes every effective Skill requirement. Runtime context composition uses those same pinned references, applies the bounded Skill context limit, labels the content as untrusted guidance, and never auto-executes helper assets. Execution and Work Item provenance therefore retain the Definition record IDs that influenced the run.

## UI behavior

The Skill workspace supports search, category/source/lifecycle filters, provenance, source revision, consumers, exact revisions, and source synchronization. Thread settings expose a searchable categorized picker and identify explicit and inherited Skills. Delayed requests remain scoped to the active Thread through the canonical Thread endpoints; no browser-only assignment state is authoritative.

The initial adapter accepts catalog bundles supplied to the API. Provider-specific retrieval, verification, and scheduling build on the same source and synchronization contract and must cross the governed provider/action and worker boundaries when they add external reads or executable scanning.
