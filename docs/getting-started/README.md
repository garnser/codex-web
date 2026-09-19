# Getting Started

The safest first experience is a local, single-instance deployment with no external action providers enabled.

## Recommended sequence

1. [Install and start](installation.md).
2. [Verify the first run](first-run.md).
3. [Complete the first successful task](../tutorials/first-successful-task.md).
4. Read [Core Concepts](../core-concepts/README.md) before enabling external providers or broader autonomy.
5. Read [Trust and credentials](../administration/trust-and-credentials.md) before privileged actions.

## Minimal safe starter configuration

The repository defaults are intentionally conservative:

- bind to `127.0.0.1:8765`;
- SQLite canonical state;
- in-process event transport;
- local deployment mode;
- project sandbox `workspace-write`;
- approval policy `on-request`;
- no inbound webhook credential unless you configure that integration;
- no requirement to enable production autonomy.

For Docker, start from `.env.example` and keep:

```text
CODEX_WEB_BIND=127.0.0.1
CODEX_WEB_PORT=8765
CODEX_WEB_STATE_BACKEND=sqlite
CODEX_WEB_EVENT_TRANSPORT=in-process
CODEX_WEB_DEPLOYMENT_MODE=local
```

Do not expose the service publicly until authentication, network boundaries and privileged provider credentials are intentionally configured.
