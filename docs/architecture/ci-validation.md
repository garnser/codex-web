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

The unit runner refuses a `data` symlink before importing the application. Local
qualification must use an isolated runner-local directory. Hosted CI replaces the
repository's dangling deployment symlink with such a directory during preparation.

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
