# Mammouth Code CLI runtime

Tracking: #844. Generic CLI execution architecture: #651.

## Current adapter contract

`codex_web.mammouth_cli_runtime.MammouthCliAdapter` defines the provider-neutral CLI command and event contracts. `codex_web.services.mammouth_agent_runtime.MammouthCliAgentRuntimeAdapter` exposes those contracts through AgentRuntime.

The AgentRuntime adapter creates a stable logical session ID before the first turn and maps it to Mammouth's native session ID when the JSON stream supplies one. Resume always uses the explicit `--session <session-id>` argument; it never infers continuation from the working directory. Native event payload metadata is retained after credential/error-field redaction.

Every turn requires an explicitly injected `run_command` executor. It receives the CLI command, canonical session and provider-native session binding, assignment/execution/workspace/worker IDs, output callback and timeout. There is no host subprocess runner default. The application can connect this seam to the canonical assignment-bound sandbox executor when that integration is ready; until then the adapter fails closed without an injected executor.

Readiness likewise requires an injected CLI probe and execution executor. Missing dependencies produce unavailable health and turns are rejected before executor invocation.

`MammouthCliAdapter` uses the provider-neutral CLI runtime contract.

- Executable: `mammouth` by default; an explicit executable path can override discovery.
- Readiness probe: `mammouth models mammouth-ai`.
- Non-interactive execution: `mammouth run --format json --dir <workspace> <prompt>`.
- Model selection: `--model`; unqualified model names are scoped to `mammouth-ai/` and no static model catalog is embedded.
- Continuation: `--session <session-id>`; codex-web does not use folder-relative `--continue` because that could attach an unrelated canonical Run to the last session in a directory.
- JSON output follows the upstream `mammouth run --format json` contract verified at commit `04a555694f0f8f5207be7dad45c04e5491cccced`: events have `sessionID` and `type` values `step_start`, `step_finish`, `text`, `reasoning`, `tool_use`, or `error`; message identity is in `part.messageID`.
- `step_start` starts a canonical turn and agent-message item; `text` and `reasoning` become deltas; `step_finish` completes the message item; and `tool_use` becomes dynamic-tool-call item start/completion events. `error` becomes a failed turn without exposing provider error details.
- Mammouth exits when the underlying session becomes idle but does not emit a `session.status` event. Only a successful process exit produces the synthetic `turn/completed`; non-zero exit and provider errors fail the turn. `start_turn` waits for a JSON event/session identity and yields after signaling start so caller active-turn bookkeeping precedes fast terminal events.
- Event payloads are projected from recognized fields and recursively sanitized before publication; credential-bearing and error fields/values are not retained.

## Authentication and credentials

The adapter does not read Mammouth configuration files and does not copy API keys into codex-web state. The generic CLI probe only executes Mammouth's supported CLI command and returns a bounded readiness state without exposing provider stdout/stderr.

Headless worker authentication is configured through the reference-only `mammouth.worker.api_key_secret` setting. The selected secret must use provider `mammouth-ai` (or `mammouth`) and purpose `mammouth_api_key`, explicitly allow the execution-worker identity, and expire within the assignment's bounded delegation window.

`MammouthAuthDelegationService` resolves that secret only inside `SecretBroker.use()`. It injects only `HOME` and `MAMMOUTH_API_KEY` into the future sandbox launch, binds the metadata-only grant to the canonical assignment/worker/fence, and fails closed when the lease, fence, secret rotation, expiry, tenant, or worker authority changes. The delegation window is 24 hours, so the configured secret may live up to one day and must be rotated before its expiry. Public status contains the secret reference identifier but never credential material. The browser must never receive Mammouth credentials.

## AgentRuntime lifecycle and sandbox status

`MammouthCliAgentRuntimeAdapter` composes the CLI command/event contract with the provider-neutral AgentRuntime lifecycle: structured event streaming, explicit session continuation, model selection, non-zero exit mapping, synthetic terminal events and cancellation. This wired adapter supersedes the earlier unwired `mammouth_cli_agent_runtime` iteration, whose `execution_authorizer` gate is replaced by the stronger injected-executor boundary below.

The runtime is registered as an execution-ready application runtime. Because Mammouth Code does not expose CLI-native sandbox flags, every turn executes through the canonical worker boundary instead of a host subprocess:

1. `MammouthCliSandboxTurnExecutor` (`codex_web.services.mammouth_worker_session`) is injected as the adapter's `run_command` executor. Each turn re-validates the fenced assignment lease, resolves repository mounts through `LocalExecutionWorkerRuntime.repository_mounts`, and launches the CLI via `BubblewrapExecutionBackend.spawn_interactive` with the assignment's CPU/address-space/process/wall limits.
2. Model egress stays brokered. Each turn starts an assignment-bound `AssignmentBoundAgentModelEgressBroker` and wraps the CLI in the same Unix-socket HTTPS relay used by the Codex app-server path. The sandbox network namespace remains isolated; only the resolved model-gateway endpoints plus `api.mammouth.ai` are reachable, and every CONNECT re-validates the assignment.
3. Credentials cross only inside `MammouthAuthDelegationService.use()`. The raw key exists only in the sandboxed process environment, never in codex-web state, command logs, events, or the browser.
4. Per-assignment ephemeral worker HOME directories (from `MammouthWorkerHomeRegistry`) are mounted writable so Mammouth session state persists across the turns of one assignment and is removed with it. Continuation always uses explicit `--session <session-id>`.
5. The CLI executable's directory is mounted read-only at its host path so the resolved executable is available inside the sandbox.
6. Timeout and cancellation kill the full sandboxed process group through `backend.terminate_process`; non-zero exits and timeouts fail the turn through the canonical `turn/failed` taxonomy.

`MammouthCliAgentRuntimeAdapter` is registered in the AgentRuntime registry for `mammouth-ai/mammouth-cli` with brokered-model-egress networking, the `mammouth-ai` Agent Provider is seeded, assignment-bound session routing covers the runtime binding, and runtime events are projected into the canonical thread bus. Runtime shutdown stops all assignment-bound Mammouth sessions.

Still outstanding for #844:

1. exposing Mammouth readiness/version/diagnostics in the existing Operations UI;
2. durable provider-native session mapping across adapter restarts (the canonical-to-native session alias is currently process-local);
3. upstream Mammouth CLI support for scrubbing `MAMMOUTH_API_KEY` from the CLI's own tool/shell child processes. The outer sandbox and brokered egress bound the blast radius, but in-sandbox child processes can currently read the delegated key from their inherited environment.

Clean-host verification status: install, authentication, execution, streaming, cancellation, and explicit `--session` continuation were verified end to end against a live worker sandbox on the activation host; repeat the verification pass when provisioning a new worker host.

## Upstream CLI basis

The implementation follows Mammouth Code's current open-source CLI contract: the `run` command supports non-interactive messages, `--format json`, `--dir`, `--model`, and explicit `--session` continuation. Keep adapter tests aligned with upstream before enabling the runtime by default.
