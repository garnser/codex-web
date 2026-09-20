# Rootless Podman execution-worker qualification

Rootless Podman is supported for running the Codex Web **control plane**, but
the built-in local Bubblewrap execution worker is currently **not supported
inside a rootless Podman container**.

This is a deliberate fail-closed qualification result, not a request to run
the container privileged.

## Qualification result

CI qualifies the same non-root Bubblewrap probe used by the local execution
backend under rootless Podman.

Two candidate outer-container profiles were tested:

1. default/rootless container with relaxed seccomp/SELinux filtering;
2. the same container with container-scoped `CAP_SYS_ADMIN` inside Podman's
   rootless user namespace.

Neither provides a supported nested Bubblewrap boundary on the qualified
Ubuntu/Podman environment:

- without `SYS_ADMIN`, Bubblewrap fails mounting its private `/proc` with
  `Operation not permitted`;
- with namespaced `SYS_ADMIN`, Bubblewrap refuses the unexpected capability
  configuration rather than establishing the sandbox.

Codex Web therefore does not publish a rootless-Podman Compose profile that
claims local command execution is safe or functional.

## What still works

The Codex Web control plane itself may run under rootless Podman. On startup,
the local Bubblewrap probe runs and the built-in worker advertises only the
capabilities the probe actually established.

When nested Bubblewrap cannot initialize:

- `command_execution` is absent from the worker capability set;
- execution readiness is degraded;
- new turns that require command execution receive a structured
  `execution_preflight_blocked` response before an ExecutionWorkspace or
  assignment is created;
- health/operator endpoints explain the missing capability and remediation.

There is no automatic privileged-container fallback.

## Supported execution alternatives

For deployments that run the control plane with rootless Podman, place the
execution worker on a boundary where Bubblewrap can establish its namespaces:

- run the local execution worker natively on a supported Linux host; or
- run it in a dedicated VM/worker host.

Do not grant the control-plane container `--privileged`, host
`CAP_SYS_ADMIN`, a Docker/Podman socket, or broad host filesystem mounts as
an automatic recovery mechanism.

A future independently qualified worker backend may add another supported
containerized execution profile without changing this fail-closed contract.

## Verify readiness

Runtime health reports execution readiness:

```bash
curl -fsS http://127.0.0.1:8765/api/healthz
```

The response includes `executionReadiness` with:

- `ready`;
- a stable blocker code such as `worker_capability_missing`;
- required and available worker capabilities;
- local isolation backend/probe result;
- detected container runtime/profile;
- remediation guidance.

Administrators can also inspect the worker-specific view:

```bash
curl -fsS \
  'http://127.0.0.1:8765/api/execution-workers/readiness?required_capability=git&required_capability=command_execution&execution_contract_version=thread-turn%2F1.0'
```

A worker advertises `command_execution` only when the Bubblewrap probe has
actually succeeded.

## Failure recovery

After moving execution to a supported host/boundary:

1. restart or reconcile the execution worker so its isolation probe reruns;
2. inspect `/api/healthz` or `/api/execution-workers/readiness`;
3. confirm `command_execution` appears in available capabilities;
4. retry the blocked operation.

Do not edit persisted worker capabilities manually. Worker startup/probe
reconciliation is authoritative.
