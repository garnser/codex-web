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

CLI execution is an isolated-worker transport. The adapter uses an argument vector without a shell, a private temporary input directory, a fixed environment allowlist, a 300-second maximum timeout, a 512 KiB input bound, and bounded stdout/stderr. The application supplies the canonical local isolated-worker transport described below. Missing transport or scanner prerequisites are visible blockers; there is no unsandboxed fallback.

## Lifecycle and UI

Catalog ingestion invokes the configured provider automatically and records scan-error/quarantine state without publishing the imported draft. Publication, Agent Profile attachment, Thread assignment, and runtime reference validation use the same deterministic gate. No LLM decides permission or known scan state.

The Skill Catalog shows security status, exact-revision currency, scanner/version/mode, score/severity, findings, report-artifact reference, and assignment decision. Operators can filter by security state, severity, risk range, finding category, and scanner; run static or semantic scans; and publish versioned policy changes. The Thread picker shows status and denial reason and disables new assignment of blocked Skills while allowing an existing blocked assignment to be removed.

Audit records cover first scan, re-scan, scanner version and finding changes, quarantine, baselines, and overrides. Future scanner providers must reuse these records and gates rather than adding a second trust or policy system.

## Request-driven local transport

The application composes the built-in provider with `LocalExecutionWorkerRuntime`.
It does not supply a control-plane subprocess runner. The existing operator
Configuration Registry owns `skills.scanner.runtime_root` (a dedicated installed
runtime directory containing `bin/skillspector`) and `skills.scanner.project_id`
(the same-tenant canonical Project owning scratch workspaces and evidence).
Neither value grants worker, network, credential, publication or assignment
authority. Installation is an independent operator prerequisite; this integration
never installs or downloads scanner code. Configuration publication/supersession,
rollback and live resolution use the existing Configuration APIs and administration
UI. Each report records the effective configuration record IDs/revisions.

Provider inspection requires existing Skill administrator assurance and performs
one bounded isolated `--version` operation. A static scan performs at most one
version discovery followed by one scan, without retries. Both use canonical
`skill_scan` subjects, scratch ExecutionWorkspaces, published exact
`orchestration-only` profile references and `skill-scan/1.0` assignments. This is
an explicit operation contract advertised by compatible local workers, not a
new profile or an authority grant. Tenant mismatch, missing installation or
configuration, incompatible profile/worker/version, missing isolation, exhausted
concurrency and maintenance admission remain fail-closed blockers.

Discovery has a 10-second wall/CPU limit; scanning has 120 seconds. Each assignment
has 2 GiB address-space, 64 MiB writable disk and 64-process headroom limits. The
scanner receives a read-only installed runtime and bounded private input mount,
with a read-only scratch workspace, private HOME/tmp, no network, no repository,
no provider/broker channel and no secret references. Live identity, capability,
contract compatibility, workspace/resource lease and assignment fence checks
remain required. Output is bounded by the canonical backend and then restricted
to 2,000,000 bytes stdout and 64,000 bytes stderr; overflow is an error. Scratch
workspaces are released after each request. No idle timer, model invocation or
hidden scan retry is introduced.

Before reserving a scanner workspace or assignment, the current worker service
prepares the existing readiness-origin completion index under scanner Upgrade
admission. It retains the same per-call eight-page, 128-row bounds and durable
process-epoch cursor. A cold catalog that needs more preparation returns
`scanner_completion_preparation_progressing` with no scanner assignment or
command created. A distinct authorized request may resume that cursor; it does
not replay an earlier command. Invalid preparation fails closed as
`scanner_completion_preparation_unavailable`. Both are retained as scan errors,
including integer-zero assignment/start observations, rather than scan PASS.
The existing Skill security error surface displays the safe next action.

Preparation grants no retirement authority. Canonical completion still checks
the current origin index under its writer, exact worker identity, fence, lease
and assignment deadline. Discovery/scanning command limits, assignment and
workspace deadlines remain unchanged. A command returning zero after custody
expires is still refused; historical lost or unknown assignments are not
completed or retried by this preparation.

Scanner failure Evidence retains the observed local execution phase separately
from scan acceptance. `before_backend` means the worker did not enter its backend
call; `backend_unresolved` does not establish whether a process started or exited.
A returned backend result supplies exit code, elapsed time, timeout, limit and
truncation metadata. If an owner poll fails after spawn, the existing backend
kills the process group and joins that same process. Only a successful join may
supply `backend_stopped_after_error`, with the actual observed wait status,
elapsed time and already-known wall/limit flags. That status can describe our
cleanup signal; it is not proof of the scanner's natural outcome. Output is not
read on this error path, so truncation remains null. A failed join retains the
unresolved phase and unknown status. The original refusal or cancellation still
propagates; even an observed exit zero cannot become an accepted scan.
A later lease/poll or completion failure preserves the observation with
`scanner_completion_returned=false`; it does not fabricate a terminal assignment
or accepted scan. Ordinary backend callers omit the optional in-memory observer
and retain their existing error behavior. This adds no persisted state version,
scan controller, retry, deadline extension or authority bypass. A returned canonical completion rejected
by the transport is identified separately. Raw stdout/stderr, command arguments
and exception text are excluded from this failure metadata. Historical failures
without these observations remain unknown; installing the reader does not
reconstruct an earlier process result or prove scanner readiness.

The code-owned wire adapter currently supports SkillSpector 2.12.0, verified
against upstream commit `240421219409dc0bafef9f618a10d80c2d7e83a3`:
`src/skillspector/cli.py`, `nodes/report.py`, `models.py`, `__init__.py` and
`pyproject.toml`. Static arguments are `scan SKILL.md --format json --no-llm`.
The version callback prints `SkillSpector v2.12.0`; single-skill stdout JSON uses
`risk_assessment.score`, `issues`, nested issue `location`,
`analysis_completeness`, `execution_successful` and `metadata`. Exit 1 can mean
findings/threshold failure and still contains a valid report; exit 2 means
execution failure. A completed command alone cannot establish a successful scan:
invalid/incomplete reports, version disagreement or model activity fail closed.
Semantic mode is explicitly unavailable in this credential-free, network-disabled
transport; enabling it would require a separately governed model/credential
transport. No ambient provider login or credential is borrowed.

Normalized reports discard raw snippets, matched content, telemetry and arbitrary
upstream artifact references, apply the canonical boundary redactor, and create
canonical content-backed Report Artifacts and verification Evidence. They record
the exact Skill record/checksum, input digest, scanner version/launcher digest,
configuration revisions, execution-profile reference, assignment, workspace,
worker and fence. The launcher digest is **not** package signature verification
or qualification of all installed dependencies. Scanner execution is untrusted;
only canonical policy evaluates publication/assignment. Transport failure records
retain their assignment/evidence links when available and never claim findings.

The Skills UI exposes current provider readiness, version and actionable blockers,
refreshes only on request/scope changes, and displays exact report attribution.
`scan_error` uses null risk/severity and empty findings; legacy numeric/critical
error sentinels are normalized in API projections. Policy outcome remains
independent and fail-closed. Genuine critical findings remain critical. This UI
work is included in issue #1471's delivery scope under cross-cutting #127; no
separate status catalog or UI enforcement path is introduced.

### Persisted compatibility and rollback boundary

Worker state advances from 1.11 to 1.12 and workspace state from 1.7 to 1.8 to
admit the explicit `skill_scan` subject without inventing a Thread or Work Item.
Forward migrations preserve every existing subject/record and do not mutate
caller input; legacy Work Item, Thread and bootstrap subjects remain readable.
The exact new execution contract is `skill-scan/1.0`; older workers that do not
advertise it are ineligible. Scan-result schema 1.1 adds attribution/error codes
and permits null risk/severity for execution errors. Legacy unversioned scan
records load through the existing model defaults, with error sentinels normalized
at the public projection.

Older binaries cannot read the new subject or scan-result shape. **Mixed old/new
writers are unsupported.** Drain old application writers and workers before
upgrading, retain a verified database backup, then start the new control plane.
After new records are written, binary-only local rollback is unavailable; restore
the pre-upgrade database and matching application together under the canonical
upgrade/maintenance procedure. Do not claim that a Git revert makes persisted
state compatible. Tests cover previous-state migration, preservation of previous
subjects and explicit rejection by the previous subject/version readers.

### Enforced scanner admission and policy

`SkillSecurityService.required_mode` resolves source trust from the canonical
same-tenant Skill Source store. Missing sources are untrusted. A manual static
request is promoted to semantic when effective policy requires semantic assurance;
publication and assignment independently recheck mode and provider against current
policy. Earlier static evidence cannot satisfy a later semantic requirement or
source trust revocation. Semantic transport remains unavailable without any model
or network dispatch. Existing audited overrides remain explicit policy decisions.
The detail UI exposes the canonical required mode and denial reason.

The application registers the code-owned `skill-scan-contract/1.0` Upgrade handler.
Before any new scan-result write (including an error) or discovery workspace
reservation, canonical Upgrade admission requires a successfully evidenced,
irreversible contract step for this local database and covered tenant.
The database marker survives replacement of the process instance; its drain
authorization remains bound to the enabling service instance. The step
requires service maintenance, no active admissions/assignments, a single observed
control-plane writer version, no active old scanner-incompatible workers, canonical
irreversible approval consumption and successful RecoveryService verification of
the exact pre-upgrade backup. Shared/replicated transitions are refused by this
local integration. Execute the step through the existing Upgrade API and finish
post-upgrade verification before using scans; scanner/configuration publication
cannot enable the boundary. No upgrade is executed automatically by inspection.

Compatible pending inputs may remain held during this transition only when they
have never been claimed (zero fence, no worker/lease, execution timestamps,
failure or output references). Maintenance fences their claims. This preserves
historical custody and unknown external outcomes without replay or cancellation.
All other nonterminal assignment states block activation. A deterministic first
attempt refused at this assignment gate can be cancelled through the narrowly
proven Upgrade cancellation disposition described in `safe-upgrades.md`; its
failed attempt and consumed approval remain recorded, and a fresh exact-release
plan needs its own approval.

The completed step and rollback denial are persisted before scanner writes become
eligible. The same Upgrade transaction disables binary rollback on prior plans;
subsequent plans inherit the denial while this boundary remains in the database.
This is conservative: only restoration of the pre-boundary database together with
a matching binary reestablishes compatibility. Restoration removes the enablement
step too; it cannot silently replay scans or authorize new-format writes. Existing
Upgrade UI step/Evidence and rollback fields are the operator surface. Maintenance
admission covers the complete operation, including version discovery and result
persistence, so a closed drain creates no scanner subject or scan-result rows.

Runtime roots must be direct dedicated children of `/opt/codex-skill-runtimes`.
Only `bin`, `lib`, `lib64` and `pyvenv.cfg` are admitted at the root; bounded recursive
inventory rejects hidden/credential filenames, non-regular entries and escaping
symlinks (except system Python interpreter links). The fixed launcher is a regular
executable. Both provider settings and worker dispatch validate this contract.
A project checkout, home directory, generic environment or broad host root is not
a scanner installation. Operators must supply a dedicated credential-free runtime;
this validation neither installs dependencies nor proves NVIDIA qualification.
Live canonical worker identity/membership, compatibility and leases are checked
before launch, after command preparation immediately before spawn, during polling
and before accepting terminal output. Worker completion independently checks live
identity, including an immediate-exit process that skipped the polling loop.

Dedicated Python runtime preparation must preserve the public CA bundles used
by dependencies during offline imports. A regular `cacert.pem` is admitted only
when it is at most 512,000 bytes and contains one to 512 parseable X.509
certificates with CA Basic Constraints, or legacy self-issued v1 roots that
predate extensions. Separators may contain only blank lines or comments.
Mixed/private key material, malformed certificates, leaf certificates, hidden
files and certificate symlinks remain refused. Other credential filenames retain
their prohibition. This checks public data type, not certificate trust or TLS
signature eligibility. Bundles grant no worker network access or control-plane
trust and never enter the application's certificate store. Standard virtualenv
packaging entries outside the dedicated-runtime contract still require explicit
preparation; admitting certificates does not admit an arbitrary environment.
