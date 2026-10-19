# PR validation and test ownership

The `tests` workflow starts on every pull request and main push. It does not use
workflow-level path exclusions. Main pushes always run the full stack.

## Selection

`scripts/ci/selection.py` examines the complete immutable PR base/head diff,
including both sides of renames and deleted files. The checkout remains GitHub's
PR merge revision: selection does not switch testing to a different tree.

Only explicit root community/prose files, existing contribution templates and
Markdown within the listed user-documentation directories qualify for focused
documentation validation. The executable allowlist lives in that script. New
paths are not implicitly trusted as documentation.

Architecture documents, AGENTS/EXECUTIVE instructions, Docker documentation,
screenshot manifests/assets, browser fixtures, scripts, workflow configuration,
dependencies, runtime code and unknown paths require full validation. Type
changes, symlinks and executable prose also require the full stack. Missing,
empty, malformed or unavailable diffs fall back to full testing. Reduced testing
is not selected from a PR title, label or only its newest commit.

Focused validation runs documentation foundation checks and CI selection/gate
regressions with the standard library, without installing application dependencies
or starting browsers, databases or containers. Documentation checks still catch
broken links, missing required guides and violated content contracts.

## Full-stack ownership

| Owner | Coverage |
| --- | --- |
| Unit/static | Broad Python discovery and all frontend JavaScript syntax checks |
| GitLab qualification | `tests.test_gitlab_sync_scaling` |
| Repository-target qualification | `tests.test_explicit_repository_target_qualification`, named coordinated-scope checks and repository-target/preflight browser specs |
| Distributed backends | PostgreSQL/Redis-backed semantics; preserved separately from default unit environments |
| Functional Chromium | Three file-level shards, each with one worker; accessibility runs once on shard one |
| Visual Chromium | Both visual-baseline spec files |
| Documentation screenshots | Deterministic screenshot generation and its existing privacy/content assertions |
| Docker | Image build, version/liveness, clean-install/restart and packaged bootstrap validation |
| Rootless Podman | Real isolation environment and fail-closed qualification |

`scripts/ci/run_unit_tests.py` excludes only the two modules owned by dedicated
qualification jobs. Other modules and discovery errors remain in the broad
suite. Named cross-cutting checks may still overlap; removing those small repeats
would add brittle case-level selection. Container and distributed executions are
retained because their environments add distinct validation.

Broad discovery and explicit qualification use the canonical `tests.*` package
identities. Reusable fixtures are imported as modules (for example,
`from tests import test_project_readiness as fixture`), rather than exposing an
imported `TestCase` class at a consumer module's top level. Standard unittest
otherwise collects that imported class again. The original owning module keeps
all its cases; inherited backend contract classes remain distinct owners.
Discovery regressions audit concrete source class/method identities without
filtering any cases from the runner.

The repository-target job runs its module's own qualification cases, an explicit
project-readiness case and named coordinated-scope checks. The reused binding
fixture's ordinary cases belong to the broad suite. The named overlaps above
remain intentional; an imported fixture class is not qualification evidence.
The broad runner emits full case IDs and named skip reasons, including optional
backend/native exclusions. Discovery failures remain failing cases even when
the broken module would otherwise belong to a dedicated qualification job.

Readiness settlement qualification runs the composed provider, broker and intent
cases on SQLite and again against real PostgreSQL in the distributed job. Its
additional broad-suite cases retain the same required unit/static check with a
15-minute job bound. The earlier 10-minute job cancelled after a completed
4,169-test Python stage before JavaScript checks could finish; added transaction
coverage needs room for the entire gate. Test selection, assertions, application
deadlines and other operational limits remain unchanged.

The unit runner resolves its repository root from its own file and refuses that
repository's `data` symlink before discovery or application imports, whether the
link exists or dangles. Discovery, import precedence and the process working
directory then use that same root. Invoking the runner from another directory
cannot substitute that directory's data check or test inventory. The runner does
not mutate a data link or provision storage. Local qualification must prepare a
fresh isolated runner-local directory and SQLite database; direct focused
`unittest` commands require the same preparation because they bypass this runner.
Hosted CI replaces the repository's dangling deployment symlink with such a
directory during preparation. Restore a preserved local link only after all owned
test/server processes end, retaining failed database/log snapshots separately.

`playwright.ci.config.js` excludes five explicitly owned spec files from the
functional shards. Titles never determine ownership. Each full run verifies that
the three shards plus dedicated spec selections form an exact, non-overlapping
union of the original browser inventory. Empty selections fail. Tests stay serial
inside a shard; each shard has its own runner, HTTP server and diagnostic artifacts.
Accessibility and functional diagnostics use separate output directories so one
invocation cannot erase the other's failure evidence.

## Required check evidence

The existing required names are retained: unit-static, gitlab-sync-qualification,
explicit-repository-target-qualification, distributed-backends,
chromium-functional, chromium-visual, chromium-screenshots, docker-smoke and
rootless-podman-worker. The chromium aggregate remains available too. CodeQL
configuration and branch protection are unchanged.

Named test jobs verify the selection and expected documentation outcome before
their runtime steps. A prose-only job reports inapplicability and skips its heavy
steps; it does not install application dependencies or start service containers.
Full runs execute every runtime step under that same required name. Functional
Chromium retains a strict aggregate over all three shards, while the chromium
aggregate checks all owners. Missing, failed, cancelled or unexpectedly skipped
results fail the applicable gate. Matrix success means all its shards/suites
passed. An inapplicable check is explicit in its summary, not a claimed test pass.

The selection summary records the complete PR diff and tested checkout revision.
Diagnostic and screenshot artifacts remain attached to actual test jobs. Never
reuse green results from another revision as proof for the current PR.

## Qualification and limits

Workflow changes themselves select full validation. Before accepting a selection
policy change, run selection/gate regressions, confirm browser inventory coverage
and observe green Actions on the changed workflow. Exercise the reduced path on a
prose-only PR as well. Changes to shared code still run broad tests: there is no
filename-to-unit-test inference or automatic model-based selection.

Compare successful current runs against similar full-stack baseline runs. Sharding
primarily reduces wall time; additional shard runners may increase runner minutes
for code PRs. Gates are folded into existing jobs to avoid redundant wrapper
runners. Prose-only PRs avoid the heavy suites. Full
main runs provide continuing regression coverage but do not substitute for
applicable pre-merge checks.

The administrative broker runtime tests compose the database-backed Agent Profile,
Definition, worker claim/start and ActionIntent services behind the actual Unix
socket broker. A synthetic conformance provider supplies controlled receipts and
unknown outcomes without external effects. Its reusable global execution-profile
template has no administrative targets; only the exact Project catalog supplies
the administrative envelope. These checks cover execution-time revocation and
retained idempotency/history, including a retry through a replacement Unix socket
broker that preserves the unknown receipt without another provider execution.
A fresh interpreter separately verifies persisted receipt history. Original-provider
input delivery and genuine independent review require their own evidence.

## Procedure fixture coverage

`test_procedure_execution` owns the complete backup, isolated diagnostic,
mutation, HTTP verification and conditional rollback workflow, including unknown
effects, restart, authority and secret-reference boundaries.
`FullProcedureOutcomeTests` runs that complete contract before concurrent outcome
conversion; the canonical playbook browser fixture also uses this full fixture.

The remaining `ProcedureOutcomeTests` qualify outcome policy after a smaller real
backup, mutation and HTTP verification procedure. They retain private SQLite,
provider receipts, artifacts, governance, scan/publication checks and actual
process/concurrency tests. Their prerequisite does not repeat the unrelated
diagnostic and rollback qualification. No completed checkpoint or receipt is
fabricated or shared between cases. Each fixture owns its mutable database,
repository, HTTP server and cleanup; a short server shutdown poll and joined
thread avoid unnecessary teardown waits without changing HTTP behavior.

## Readiness fixture coverage

`test_pull_request_ready` prepares its scoped execution profile before the real
worker assignment is created, claimed and started. Shared broker fixtures can
prepare that foundation without starting an unused metadata endpoint or binding
an unrelated issue-creation provider. The readiness fixture starts its own
assignment-bound endpoint with canonical ActionIntents, SecretBroker and the
GitHub HTTP adapter. The default administrative fixture still qualifies issue
creation, and the metadata fixture still owns its endpoint lifecycle tests.

Every readiness case owns its database, assignment, socket, credential backend
and provider state, including a reusable broker HTTP client. An idle SQLite
connection keeps that case's WAL alive between service transactions, avoiding
repeated journal teardown/recovery. It holds no transaction; service operations
still open their own connections and execute the real storage guards. It closes
before the private directory is removed. The PostgreSQL subclass uses no such
connection and runs the same contracts with a private schema and real
single-connection pool; its independent writer uses a separate pool to prove
transaction-lock custody. Fixture preparation does not cache completed actions
or replace admission, revocation or settlement checks.
The successful transition/idempotent duplicate case explicitly closes SQLite's
idle handle before the duplicate request, retaining cold-reopen qualification.

Full-shape preservation has a separate [serialized qualification owner](full-shape-qualification.md).
Default gates report it as NOT EXECUTED; 29 conditional skips represent three
distinct scale methods plus 26 inherited ordinary cases, not scale evidence.
