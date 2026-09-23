# CLI-backed AI runtimes

codex-web can execute supported AI providers through an installed provider CLI instead of requiring codex-web to own a direct provider API credential.

## Codex CLI

The first supported CLI runtime is `openai/codex-cli`.

Prerequisites:

1. Install the Codex CLI so `codex` is executable.
2. Authenticate it with the CLI's normal login flow.
3. Confirm `codex login status` succeeds.
4. Keep the execution worker/runtime healthy and the selected Project/repository execution-ready.

The runtime probes the CLI through its supported readiness command and does not read or copy the CLI credential store. A successful local CLI login is enough for this runtime; `OPENAI_API_KEY` is not required by codex-web for the CLI-backed path.

## Execution model

CLI execution still goes through canonical codex-web execution state before the provider process starts:

- Project and repository authority is resolved first.
- An execution assignment and isolated repository workspace are created.
- Sandbox and approval policy are mapped to provider CLI arguments.
- The CLI runs with the canonical workspace as its working directory.
- Structured CLI events are projected into the existing thread/Run event pipeline.
- Cancellation terminates the owned provider process tree.
- Assignment lease/fence and completion state remain canonical.

The initial Codex CLI runtime currently fails closed when an execution requires coordinated writable repositories or secondary repository mounts that cannot be represented safely by the CLI topology.

## Authentication and environment

Provider authentication remains owned by the provider CLI. codex-web does not persist provider tokens from the CLI.

The Codex CLI probe and subprocess receive only the explicit environment allowlist required for local CLI operation:

- `HOME`
- `CODEX_HOME`
- `PATH`

Unrelated parent-process environment variables are not forwarded by default. Direct API keys are not required for CLI execution.

This is different from the assignment-bound app-server path, where codex-web may use delegated SecretReferences. Runtime selection must be explicit; missing credentials in one mode do not silently cause fallback to another authentication mode.

## Readiness states

CLI readiness distinguishes:

- executable not installed
- executable misconfigured
- installed but unauthenticated
- readiness probe unavailable
- ready

A CLI being present on disk is not treated as proof of authentication or authorization. Canonical Project/repository/worker/policy checks still apply independently.

## Troubleshooting

If the CLI runtime is unavailable:

1. Run `codex login status` as the same operating-system user running codex-web.
2. Verify `codex` is on that process user's `PATH`, or configure the explicit executable path.
3. Verify `HOME`/`CODEX_HOME` point to the intended CLI login context.
4. Check runtime/worker readiness and the selected repository/sandbox policy.
5. Do not add an API key merely to work around a failed CLI login; repair the selected authentication mode instead.

The CLI runtime does not expose raw readiness stdout/stderr or credential material through public diagnostics.
