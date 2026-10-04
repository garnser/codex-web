# Skill security scanning and assignment gates

Status: implemented foundation for issue #962.

External Skill content is untrusted instructional and potentially executable data. A successful import does not grant trust, publication, assignment, tool authority, secret access, or execution authority. Security state is pinned to an exact canonical Skill Definition record ID and checksum so a later upstream or local revision cannot inherit an older decision.

## Canonical state and policy

Normalized scan results are persisted separately from immutable Skill Definitions. Each result identifies the tenant, Skill ID, exact Definition record/checksum, provider and scanner version, scan mode and time, risk score, highest severity, normalized findings, policy outcome, and optional canonical report-artifact reference. Raw reports and credentials are not copied into Skill definitions.

The effective `security.skill-policy/default` policy is a published workspace-scoped Definition Registry record. It controls ingestion scans, publication and assignment requirements, fail-closed scanner-error behavior, risk/severity/category limits, optional semantic scanning, and whether audited time-bounded overrides are permitted. Executions continue to pin the exact Skill Definition reference; scan and policy evidence can therefore be reconstructed without mutating historical meaning.

Imported Skills require a current-revision acceptable scan by default. Locally authored Skills remain configurable. A changed record has `stale` security state until that exact checksum is scanned. Scanner errors fail closed by default. Baselines suppress only reviewed finding fingerprints; they do not lower the independent risk-score limit. Overrides require policy enablement and create canonical audit events.

## Provider and extension boundary

`SkillScannerRegistry` is the provider-neutral scanner seam. Registrations are tenant-scoped, with an explicit global fallback for a built-in adapter; one tenant's extension cannot replace or execute another tenant's scanner. `skill_scanner` is a declared extension category with the same installation, package verification, compatibility, grants, enablement, health, quarantine, and runtime re-authorization rules as other extensions. Installing or registering a scanner never grants its requested capabilities.

The NVIDIA SkillSpector adapter consumes bounded JSON and supports static and semantic modes. Static scanning is the default and requires no model. Semantic scanning is an optional policy choice; any provider credential remains behind canonical secret references and cannot enter a Skill, scan request, finding, log, or report metadata.

CLI execution is an isolated-worker transport. The adapter uses an argument vector without a shell, a private temporary input directory, a fixed environment allowlist, a 300-second maximum timeout, a 512 KiB input bound, and bounded stdout/stderr. The control-plane composition registers the provider identity but deliberately fails execution until a trusted isolated worker loader supplies the transport. This preserves the control-plane/execution-plane boundary instead of spawning untrusted scanner work inside the application process.

## Lifecycle and UI

Catalog ingestion invokes the configured provider automatically and records scan-error/quarantine state without publishing the imported draft. Publication, Agent Profile attachment, Thread assignment, and runtime reference validation use the same deterministic gate. No LLM decides permission or known scan state.

The Skill Catalog shows security status, exact-revision currency, scanner/version/mode, score/severity, findings, report-artifact reference, and assignment decision. Operators can filter by security state, severity, risk range, finding category, and scanner; run static or semantic scans; and publish versioned policy changes. The Thread picker shows status and denial reason and disables new assignment of blocked Skills while allowing an existing blocked assignment to be removed.

Audit records cover first scan, re-scan, scanner version and finding changes, quarantine, baselines, and overrides. Future scanner providers must reuse these records and gates rather than adding a second trust or policy system.
