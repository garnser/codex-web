# Mammouth Code CLI runtime

Tracking: #844. Generic CLI execution architecture: #651.

## Current adapter contract

`codex_web.mammouth_cli_runtime.MammouthCliAdapter` uses the provider-neutral CLI runtime contract.

- Executable: `mammouth` by default; an explicit executable path can override discovery.
- Readiness probe: `mammouth models mammouth-ai`.
- Non-interactive execution: `mammouth run --format json --dir <workspace> <prompt>`.
- Model selection: `--model`; unqualified model names are scoped to `mammouth-ai/` and no static model catalog is embedded.
- Continuation: `--session <session-id>`; codex-web does not use folder-relative `--continue` because that could attach an unrelated canonical Run to the last session in a directory.
- JSON output is projected into the canonical `AgentRuntimeEvent` shape while preserving Mammouth's provider-native session metadata.

## Authentication and credentials

The adapter does not read Mammouth configuration files and does not copy API keys into codex-web state. The generic CLI probe only executes Mammouth's supported CLI command and returns a bounded readiness state without exposing provider stdout/stderr.

Headless workers may inject credentials only through the generic CLI runner's explicit environment allowlist/secret-reference mechanism. The browser must never receive Mammouth credentials.

## Sandbox and integration status

The adapter intentionally is not registered as an execution-ready application runtime yet. Mammouth Code does not expose the same CLI-native sandbox flags used by the Codex CLI adapter, so registering it directly on the application host would risk bypassing codex-web's canonical worker/sandbox boundary.

Before enabling end-to-end routing, #844 still requires:

1. launching Mammouth inside the assignment's canonical worker/sandbox rather than as an unrestricted host subprocess;
2. wiring runtime/provider registration and Agent Profile selection;
3. exposing readiness/version/diagnostics in the existing Operations UI;
4. mapping non-zero exits into the canonical failure taxonomy;
5. verifying cancellation kills the full Mammouth process tree inside the worker;
6. clean-host verification of install, authentication, execution, cancellation and explicit continuation.

## Upstream CLI basis

The implementation follows Mammouth Code's current open-source CLI contract: the `run` command supports non-interactive messages, `--format json`, `--dir`, `--model`, and explicit `--session` continuation. Keep adapter tests aligned with upstream before enabling the runtime by default.
