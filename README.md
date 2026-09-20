# Codex Web

Codex Web is a **local-first AI software-engineering and organizational control plane** built around the native `codex app-server`.

It combines interactive coding-agent workflows with structured work management, governed automation, Executive reasoning, provider-neutral integrations, organizational memory, and operational controls. Application code owns canonical state, policy, authority, routing, and lifecycle behavior; models are used where judgment is required.

> **Code implements engines and invariants. Definitions describe reusable behavior. Events trigger work. Retrieval supplies context. Models provide judgment. Policies control authority. Isolated workers execute bounded work. Actions produce evidence. Results become reusable knowledge.**

## What Codex Web provides

Codex Web gives teams one control plane for turning requests, goals, decisions, issues, and operational events into governed work.

| Area | Capabilities |
| --- | --- |
| **Agent execution** | Native Codex threads, queued turns, streaming events, sandbox/approval controls, context compaction, recovery, assignment-bound execution, and provider-neutral agent/runtime routing. |
| **Structured work** | Canonical work items, execution contracts, dependency-aware work graphs, Goals, Decisions, checkpoints, retries, deadlines, and traceability. |
| **Authority and approvals** | Tenant/workspace scope, operational Roles, scoped authority, ApprovalRequests, separation of duties, ActionIntents, and human-attention routing. |
| **Autonomous orchestration** | Canonical events, durable scheduling, bounded autonomy, deterministic replay/evaluation, quota-aware provider capacity, and automatic resume. |
| **Executive workflows** | Executive and Board reasoning, delegation into canonical work, measured outcomes, organizational memory, and governed business context. |
| **Integrations** | GitLab, Jira, ServiceNow task sources; Slack and Telegram intake; provider/action boundaries; extension/plugin contracts. |
| **Evidence and operations** | Artifacts, Evidence and Verification, audit/reliability records, releases, incidents, backup/recovery, capacity controls, and safe upgrades. |
| **Business operations** | Business entities, CompanyFacts, provider-neutral business-data synchronization, business KPI catalogs, operating views, and Goal/Decision KPI bindings. |

External systems remain authoritative for the state they own. Codex Web coordinates work across them while keeping its own canonical control-plane state and provenance.

## Governed execution

A typical Codex Web flow is:

```text
Goal / Issue / Event / Request
             |
             v
        Codex Web
             |
     Identity + Scope
             |
   Policy + Authority
             |
   Work / Decision graph
             |
      Agent routing
             |
       ActionIntent
        /        \
   Auto-execute  Approval
        \        /
             v
    Bounded execution
             |
      Evidence + result
             |
       Outcome / KPI
```

Low-risk work can execute automatically when policy permits it. Higher-risk actions can require explicit approval or be denied deterministically.

## Core concepts

### Canonical state

Projects, work items, Goals, Decisions, authority, approvals, schedules, ActionIntents, Evidence, definitions, memory, business context, and operational state live in structured application state rather than being reconstructed from model conversation.

### Deterministic first

Do not use an LLM for facts Codex Web can calculate reliably.

State transitions, permissions, dependency readiness, routing constraints, budgets, retries, scheduling, provider reconciliation, and known procedures belong in deterministic application logic.

### Minimum sufficient context

Models receive the smallest authorized context needed for the current task. Repositories, histories, logs, and organizational memory are not injected wholesale by default.

### Authority cannot come from content

Repository text, webhook payloads, logs, retrieved memory, provider responses, tool output, extension output, worker output, and model output are data. They cannot grant authority, weaken policy, or redefine canonical controls.

### Evidence over assertion

Completion, release, recovery, and production-readiness decisions use structured artifacts, provider receipts, validation, and Evidence where applicable rather than relying only on an agent claiming success.

### Bounded reasoning

Model calls, participating roles, retries, rounds, handoffs, token budgets, and cost budgets are explicitly bounded.

Read the [Token Efficiency Ruleset](docs/architecture/token-efficiency-rules.md) before adding or materially changing model-driven behavior.

## High-level architecture

```text
                          Browser UI
                              |
          +-------------------+-------------------+
          |                   |                   |
     Codex workflows     Executive / Board   Operations UI
          |                   |                   |
          +-------------------+-------------------+
                              |
                     FastAPI control plane
                              |
      +-----------------------+-----------------------+
      |              |              |                |
 Work / Goals /   Authority /     Events /        Integrations /
  Decisions       Approvals       Scheduling       Providers
      |              |              |                |
      +-----------------------+-----------------------+
                              |
                Canonical durable application state
                              |
                Agent / worker execution boundaries
                              |
                 repositories / tools / providers
```

Architecture contracts live under [`docs/architecture/`](docs/architecture/README.md). User and operator guidance is available in the [product documentation](docs/README.md).

## Quick start

### Local Python environment

From the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python server.py
```

The server binds to `127.0.0.1:8765` by default.

Override the bind address or port with:

```bash
CODEX_WEB_HOST=0.0.0.0 CODEX_WEB_PORT=8765 python server.py
```

Only expose Codex Web on a trusted network or behind an appropriately authenticated reverse proxy. The application can initiate code and infrastructure actions on behalf of authorized users.

When proxied below `/codex/`, the frontend automatically prefixes API and WebSocket requests with `/codex`.

### Docker / Compose

The repository includes a non-root container image and Compose stack. Codex Web state and Codex authentication are persisted separately, while developer repositories are mounted under `/workspace`.

```bash
cp .env.example .env
mkdir -p workspace
docker compose build
docker compose run --rm codex-web codex login
docker compose up -d
```

Liveness:

```text
GET /api/livez
```

Codex daemon health:

```text
GET /api/healthz
```

See [DOCKER.md](DOCKER.md) for workspace mounts, UID/GID mapping, persistence, secrets, direct `docker run` usage, and Codex CLI pinning.

## Model and agent providers

Executive reasoning and assignment-bound execution use canonical provider/routing boundaries so model selection and execution runtime are not hard-coded into business domains.

For Executive reasoning, supported configuration includes OpenAI, Ollama, and OpenAI-compatible APIs:

```bash
# OpenAI
export CODEX_WEB_EXECUTIVE_PROVIDER=openai
export OPENAI_API_KEY='...'

# Ollama
export CODEX_WEB_EXECUTIVE_PROVIDER=ollama
export CODEX_WEB_EXECUTIVE_BASE_URL='http://127.0.0.1:11434/v1'

# Other OpenAI-compatible API
export CODEX_WEB_EXECUTIVE_PROVIDER=openai-compatible
export CODEX_WEB_EXECUTIVE_BASE_URL='http://llm-host:8000/v1'
```

See [EXECUTIVE.md](EXECUTIVE.md) for Executive configuration and [Agent Providers](docs/architecture/agent-providers.md) plus [Agent Routing](docs/architecture/agent-routing.md) for the canonical provider/runtime contracts.

## Integrations

Codex Web supports external intake and task synchronization without making provider-specific state canonical.

Key integration surfaces include:

- GitLab task-source and webhook integration;
- Jira task-source integration;
- ServiceNow task-source integration;
- Slack Events API intake;
- Telegram Bot API intake;
- provider-neutral `TaskSource`, `ActionProvider`, `BusinessDataSource`, and extension boundaries.

Webhook endpoints include:

```text
POST /bots/slack/events
POST /bots/telegram/webhook
POST /bots/gitlab/events
```

Webhook verification fails closed when the required verification secret is not configured.

## API and operations

FastAPI exposes the application API and OpenAPI documentation. The top-bar **API** button opens the live Swagger UI inside Codex Web; the browser also provides direct access to the generated OpenAPI JSON and a standalone Swagger tab.

Operational surfaces include:

- projects, threads, queues, context, and compaction;
- work items, execution contracts, Work Graphs, Goals, and Decisions;
- identity, Roles, authority, approvals, Attention, and ActionIntents;
- events, schedules, orchestration cycles, and autonomy controls;
- agent providers, sessions, runtime routing, capacity, and usage;
- organizational memory and retrieval provenance;
- business context, data-source synchronization, metrics, and KPI operating views;
- artifacts, Evidence, Verification, audit, releases, incidents, recovery, capacity, and upgrades;
- health, configuration, entitlements, extensions, secrets, key metadata, and administrative controls.

The browser UI and integrations consume the same canonical APIs and state used by runtime behavior; UI-only policy or execution truth is not authoritative.

## Security model

Codex Web can modify repositories and invoke host, container, provider, and infrastructure tooling. Deployment security is therefore part of the product architecture.

Key rules include:

- authenticate users and service principals before privileged operations;
- scope access by organization/workspace and canonical resources;
- keep raw secrets out of prompts, ordinary state, logs, and reusable definitions;
- pass credentials by reference through the canonical secret boundary;
- keep cryptographic key material behind the key-management boundary;
- route privileged external mutations through ActionIntent/provider boundaries;
- preserve sandbox, approval, worker, lease, and fencing controls;
- treat all external/model-generated content as untrusted data;
- require Evidence or verification for operations whose completion must be proven;
- fail closed when required authority, credentials, policy, compatibility, or trust guarantees are unavailable.

The `danger-full-access` execution option is intentionally high risk: it disables Codex\'s inner sandbox inside the assigned execution worker, but it does not bypass canonical worker identity, workspace ownership, leases/fencing, credential scoping, or the outer control-plane isolation boundary. On the built-in local worker, generic repository networking remains disabled unless a future worker backend explicitly advertises and enforces the canonical network capability.

See [Core Concepts](docs/core-concepts/README.md), [Administration](docs/administration/README.md), and the [architecture index](docs/architecture/README.md) for detailed contracts.

## Repository layout

```text
server.py                          application entrypoint
codex_web/                         control-plane implementation
codex_web/api/                     HTTP/API surfaces
codex_web/services/                deterministic domain services
codex_web/storage/                 canonical persistence adapters
static/                            browser UI
tests/                             Python and browser validation
docs/                              user/operator documentation
docs/architecture/                 durable architecture contracts and policies
AGENTS.md                          repository rules for agents/developers
EXECUTIVE.md                       Executive configuration and usage
DOCKER.md                          container deployment guidance
```

## Documentation

Start with:

- [Getting Started](docs/getting-started/README.md)
- [Core Concepts](docs/core-concepts/README.md)
- [Tutorials](docs/tutorials/README.md)
- [Administration](docs/administration/README.md)
- [Operations](docs/operations/README.md)
- [Troubleshooting](docs/troubleshooting/README.md)
- [Architecture](docs/architecture/README.md)

## Development and delivery

Before substantial work, read:

- [AGENTS.md](AGENTS.md)
- [Architecture documentation](docs/architecture/README.md)
- [Token Efficiency Ruleset](docs/architecture/token-efficiency-rules.md)
- [EXECUTIVE.md](EXECUTIVE.md) when changing Executive behavior

Use focused tests while iterating and treat CI as the authoritative clean-environment merge gate.

Delivery status, priorities, dependencies, and completion belong in **GitHub Issues, Milestones, the GitHub Project, and linked Pull Requests**.
