# Rootless Podman execution-worker qualification

Codex Web can run its local execution worker inside rootless Podman when the
outer container permits the namespace and mount syscalls that Bubblewrap uses
to create the inner execution sandbox.

The supported profile is intentionally **not** `--privileged`, does not mount
the Podman/Docker socket, and does not grant host root access.

## Start with the qualified profile

Build the application image normally, then use the Podman overlay:

```bash
cp .env.example .env
mkdir -p workspace
podman compose -f compose.yaml -f compose.podman.yaml build
podman compose -f compose.yaml -f compose.podman.yaml run --rm codex-web codex login
podman compose -f compose.yaml -f compose.podman.yaml up -d
```

The overlay sets:

- `CODEX_WEB_EXECUTION_CONTAINER_PROFILE=rootless-podman-bwrap`
- `seccomp=unconfined`
- `label=disable`

The service itself still runs as the non-root `codex` user.

## Why the outer seccomp profile is relaxed

Bubblewrap creates nested user, PID, mount, IPC, UTS, and network namespaces.
A default rootless Podman profile can reject the mount operation used to create
the sandbox `/proc`, commonly producing:

```text
bwrap: Can't mount proc on /newroot/proc: Operation not permitted
```

The Podman overlay relaxes only the outer container syscall filter so
Bubblewrap can establish its inner isolation boundary. Bubblewrap still:

- binds only the canonical execution workspace and explicitly authorized
  read-only repository context;
- creates a private PID/network namespace;
- uses a minimal process environment;
- applies resource limits;
- does not expose the host container socket or arbitrary host paths.

If disabling the outer seccomp profile is unacceptable for a deployment,
run the execution worker natively on the host or in a dedicated VM instead.
Do not enable privileged mode as an automatic fallback.

## Verify readiness

Runtime health reports execution readiness:

```bash
curl -fsS http://127.0.0.1:8765/api/healthz
```

The response includes `executionReadiness` with:

- `ready`
- a stable blocker code such as `worker_capability_missing`
- required and available worker capabilities
- local isolation backend/probe result
- detected container runtime/profile
- remediation guidance

Administrators can also inspect the worker-specific view:

```bash
curl -fsS \
  'http://127.0.0.1:8765/api/execution-workers/readiness?required_capability=git&required_capability=command_execution&execution_contract_version=thread-turn%2F1.0'
```

A worker advertises `command_execution` only when the Bubblewrap probe has
actually succeeded. A failed probe therefore leaves Project execution
not-ready before a turn acquires an ExecutionWorkspace or creates an assignment.

## Failure recovery

After changing Podman/security settings:

1. restart Codex Web so the local isolation probe reruns;
2. inspect `/api/healthz` or `/api/execution-workers/readiness`;
3. confirm `command_execution` appears in available capabilities;
4. retry the blocked operation.

Do not edit persisted worker capabilities manually. Startup reconciliation is
the authority for the built-in local worker capability set.
