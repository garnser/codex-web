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

Headless worker authentication is configured through the reference-only `mammouth.worker.api_key_secret` setting. The selected secret must use provider `mammouth-ai` (or `mammouth`) and purpose `mammouth_api_key`, explicitly allow the execution-worker identity, and expire within the assignment's bounded delegation window.

`MammouthAuthDelegationService` resolves that secret only inside `SecretBroker.use()`. It injects only `HOME` and `MAMMOUTH_API_KEY` into the future sandbox launch, binds the metadata-only grant to the canonical assignment/worker/fence, and fails closed when the lease, fence, secret rotation, expiry, tenant, or worker authority changes. Public status contains the secret reference identifier but never credential material. The browser must never receive Mammouth credentials.

## AgentRuntime lifecycle and sandbox status

`MammouthCliAgentRuntimeAdapter` now composes the CLI command/event contract with the provider-neutral AgentRuntime lifecycle. It covers structured event streaming, explicit session continuation, model selection, non-zero exits, synthetic terminal events and cancellation through the bounded CLI runner.

Execution remains deliberately fail-closed by default. The adapter requires an explicit `execution_authorizer` for the selected workspace before it will launch a Mammouth process. It is not registered as an execution-ready application runtime yet. Mammouth Code does not expose the same CLI-native sandbox flags used by the Codex CLI adapter, so registering it directly on the application host would risk bypassing codex-web's canonical worker/sandbox boundary. A CLI that is installed and authenticated therefore remains diagnostically usable but is not enough to make production execution eligible.

Before enabling end-to-end routing, #844 still requires:

1. connecting the implemented fenced credential delegation and AgentRuntime adapter to a Mammouth process launched inside the assignment's canonical worker/sandbox rather than as an unrestricted host subprocess;
2. wiring runtime/provider registration and Agent Profile selection only after that containment gate is satisfied;
3. exposing readiness/version/diagnostics in the existing Operations UI;
4. mapping assignment-level launch/exit failures into the canonical failure taxonomy when production routing is connected;
5. verifying cancellation kills the full Mammouth process tree inside the worker;
6. clean-host verification of install, authentication, execution, cancellation and explicit continuation.

## Upstream CLI basis

The implementation follows Mammouth Code's current open-source CLI contract: the `run` command supports non-interactive messages, `--format json`, `--dir`, `--model`, and explicit `--session` continuation. Keep adapter tests aligned with upstream before enabling the runtime by default.
