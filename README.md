# Codex Web

Codex Web is a **local-first AI software-engineering control plane** built around the native `codex app-server`, with a growing Executive and autonomous-company layer on top.

It started as a browser client for Codex threads. It is now evolving into a unified system for:

- interactive and delegated Codex execution;
- projects, workspaces, threads, queues, approvals, recovery, and context management;
- structured work-item execution and external task-source reconciliation;
- Executive and Board-level reasoning inside the same application;
- Slack, Telegram, and GitLab-driven work intake;
- deterministic orchestration around model calls rather than model-driven application state;
- governed expansion toward goals, decisions, authority, organizational memory, and controlled production autonomy.

The product direction is intentionally broader than "chat with a coding agent": **codex-web is becoming an operating system for an AI-assisted software company**, while keeping Codex as the execution plane and normal application code as the source of truth for state, routing, policy, and lifecycle behavior.

> **Code implements engines and invariants. Definitions describe reusable behavior. Events trigger work. Retrieval supplies context. Models provide judgment. Policies control authority. Isolated workers execute bounded work. Actions produce evidence. Results become reusable knowledge.**

## What exists today

### Unified Codex execution UI

The existing browser application remains the primary interface. It manages native Codex app-server threads rather than introducing a parallel chat runtime.

Current capabilities include:

- named projects and workspace roots;
- native Codex thread creation, listing, resume, archive, and unarchive;
- streaming turn events;
- queued turn execution;
- browser approval prompts for command and file-change requests;
- sandbox and approval-policy controls;
- stale-turn and runtime recovery behavior;
- automatic and manual Codex-native context compaction;
- thread naming and external-conversation bindings;
- health, runtime, and operational APIs.

The deployment entrypoint remains:

```bash
python server.py
```

`server.py` is intentionally thin. The main application/runtime is composed from the `codex_web` package so the historical entrypoint can remain stable while legacy monolithic behavior is progressively extracted into services.

### Canonical work items

Codex Web has a structured work-item model rather than treating every autonomous task as an unstructured prompt.

The current work-item lifecycle includes:

- implementation ownership;
- validation handoff and validation execution;
- recoverable blockers with an explicit action owner;
- ready-to-close and terminal closure states;
- semantic terminal outcomes such as completed, failed, and cancelled;
- retry policy, deadlines, structured failures, and execution metadata;
- compact execution checkpoints for resume without replaying full history;
- model/token/cost attribution hooks;
- append-only history/audit events.

Work-item stage mutation is routed through the canonical transition service instead of being reimplemented independently by each integration.

See [Canonical work-item lifecycle](docs/architecture/work-item-lifecycle.md).

### External task sources

External issue/task systems are treated through a provider-neutral `TaskSource` boundary rather than baking GitLab semantics into the core work model.

The architecture supports:

- stable external source identity and provenance;
- deterministic provider-to-canonical projection;
- discovery/read/event capabilities;
- duplicate, stale-event, conflict, and drift handling;
- exactly one configured authoritative external task source per project;
- provider capability declarations instead of assuming every system behaves like GitLab;
- shared adapter conformance rules.

GitLab is the current operational task-source provider and migration path behind this boundary.

See [Authoritative task-source contract](docs/architecture/task-source-contract.md) and [GitLab task-source adapter](docs/architecture/gitlab-task-source-adapter.md).

### Executive control plane

Codex Web includes an Executive layer derived from OpenExecutive and integrated into the **same UI and server**.

The Executive drawer supports:

- strategy, product, engineering, revenue, finance, customer-success, security, and cross-functional reasoning;
- individual executive/specialist roles;
- bounded multi-specialist Board reviews with synthesis;
- OpenAI, Ollama, and other OpenAI-compatible model providers;
- delegation from an Executive recommendation into a normal Codex thread;
- preservation of the active Codex sandbox and approval-policy controls during delegation;
- opt-in operational context rather than automatically sending repository or application state to the Executive model.

Executive reasoning and Codex execution are deliberately separate concerns: the Executive layer can recommend and delegate, while execution continues through the canonical Codex paths.

See [EXECUTIVE.md](EXECUTIVE.md).

### Slack, Telegram, and GitLab intake

Codex Web can receive external events and route them into Codex-backed work.

Current integration surfaces include:

- Slack Events API webhooks;
- Telegram Bot API webhooks;
- GitLab project/group webhooks;
- external-conversation to Codex-thread bindings;
- owner-label routing for GitLab events;
- Support ServiceDesk intake from GitLab issues;
- durable intake deduplication;
- scheduled and manual missed-ticket sweeps.

Webhook verification fails closed when the corresponding verification secret is not configured.

Provider webhook endpoints:

```text
POST /bots/slack/events
POST /bots/telegram/webhook
POST /bots/gitlab/events
```

Common verification variables:

```text
SLACK_SIGNING_SECRET
TELEGRAM_WEBHOOK_SECRET
CODEX_WEB_GITLAB_WEBHOOK_SECRET
GITLAB_WEBHOOK_SECRET
```

The integrations are part of the control plane; they are not allowed to become a second work-item, policy, or execution system.

### Context compaction and long-running execution

Long-running Codex threads use the native Codex `thread/compact/start` operation.

Automatic compaction defaults to 75% of the model context window and only runs when a thread is idle with no queued turn or unresolved approval.

```bash
CODEX_WEB_AUTO_COMPACT_PERCENT=75
CODEX_WEB_COMPACT_COOLDOWN_SECONDS=300
```

Set `CODEX_WEB_AUTO_COMPACT_PERCENT=0` to disable automatic compaction while keeping manual compaction available.

Runtime/API surfaces include:

```text
GET  /api/threads/{thread_id}/context
POST /api/threads/{thread_id}/compact
```

Work items also carry compact canonical checkpoints so autonomous execution can resume from structured state instead of replaying entire conversations.

### Durable state

Codex Web currently uses SQLite as the primary durable control-plane store.

The storage layer provides transactional updates, WAL mode, schema-version checks, health checks, transactional backups, and compatibility JSON mirrors where rollback support is still needed.

The current deployment model is intentionally **single-instance**. Moving durable state to PostgreSQL alone would not make active/active operation safe; shared worker ownership, leases, coordination, and fencing are roadmap requirements before replicated execution is supported.

See [Storage scaling path](docs/architecture/storage-scaling.md).

## What the project is becoming

The long-term target is a governed, event-driven software-company operating system in which normal code owns deterministic state and models are invoked only where judgment is actually required.

The roadmap is organized into M1–M12:

| Milestone | Focus |
| --- | --- |
| M1 | Executive Contract Foundation |
| M2 | Work Item Lifecycle + Task Sources |
| M3 | Platform, Identity & Safe Execution Foundation |
| M4 | Dependency-Aware Work Graphs |
| M5 | First-Class Goals |
| M6 | Role Authority + Permission Contracts |
| M7 | Event-Driven Autonomous Orchestration |
| M8 | First-Class Decisions |
| M9 | Executive Management |
| M10 | Organizational Memory |
| M11 | Controlled Production Autonomy |
| M12 | Product Documentation + Adoption |

The roadmap does **not** mean every item above is already implemented. GitHub Issues, Milestones, the GitHub Project, and linked Pull Requests are the delivery source of truth.

Tracking bootstrap issue `#126` defines the roadmap structure and issue assignment. The architecture index describes the durable contracts and dependency order.

M3 is deliberately foundational. Later autonomous behavior must build on canonical identity, tenant/workspace scope, secrets-by-reference, provider-neutral actions, safe execution boundaries, evidence, versioned contracts, configuration, model governance, data governance, and other shared primitives rather than inventing feature-local substitutes.

A major architectural rule is:

> **Definitions are data; engines are code.**

Mutable reusable operational definitions are intended to live in a versioned database-backed Definition Registry, while schemas, interpreters, migrations, cryptographic verification, protocol versions, and hard security invariants remain code-owned. This foundation is tracked by issue `#170`.

## Architecture principles

### Deterministic first

Do not use an LLM for facts codex-web can calculate reliably.

State transitions, permissions, routing, dependencies, budgets, retries, scheduling, provider reconciliation, and known procedures belong in deterministic application logic.

### Idle means zero

Autonomous reasoning is event- or request-driven. An idle deployment should consume approximately zero model tokens.

### Minimum sufficient context

Models should receive the smallest authorized context needed for the current decision or execution step. Full repositories, logs, histories, and company memory are not injected by default.

### Structured state over prose

Projects, work items, execution metadata, goals, decisions, authority, approvals, actions, evidence, and reusable definitions belong in canonical structured state rather than being reconstructed from chat history.

### Evidence over assertion

Completion and release decisions should increasingly be based on artifacts, provider receipts, validation, and structured evidence rather than an agent merely claiming success.

### Authority cannot come from content

Repository text, webhook payloads, logs, retrieved memory, provider responses, model output, and other untrusted content are data. They cannot grant authority, weaken policy, or redefine canonical controls.

### Bounded reasoning

Role participation, model calls, retries, rounds, handoffs, context, token use, and cost must be explicitly bounded.

Read the full [Token Efficiency Ruleset](docs/architecture/token-efficiency-rules.md) before adding or materially changing LLM-driven behavior.

## High-level architecture

```text
                         Browser UI
                             |
          +------------------+------------------+
          |                                     |
     Codex workflows                     Executive / Board
          |                                     |
          +------------------+------------------+
                             |
                    FastAPI application
                             |
        +--------------------+--------------------+
        |                    |                    |
   Projects/threads      Work-item state      Integrations
   queues/approvals      lifecycle/events     Slack/Telegram/GitLab
        |                    |                    |
        +--------------------+--------------------+
                             |
                    Durable SQLite state
                             |
                     codex app-server
                             |
                   repositories / tools
```

The current application is a single control-plane deployment. The roadmap introduces stronger control-plane/execution-plane separation and isolated worker trust boundaries before production-grade autonomous or multi-instance execution is considered complete.

## Repository layout

```text
server.py                         stable launcher / composition entrypoint
codex_web/application.py          main application/runtime
codex_web/executive.py            Executive domain and routing logic
codex_web/executive_integration.py Executive provider/API integration
codex_web/services/               extracted deterministic services
static/                           unified browser UI
tests/                            Python and browser validation
docs/architecture/                durable architecture contracts and policies
AGENTS.md                         mandatory repository rules for agents/developers
EXECUTIVE.md                      Executive control-plane documentation
DOCKER.md                         container deployment and workspace guidance
```

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

Override the bind address/port with:

```bash
CODEX_WEB_HOST=0.0.0.0 CODEX_WEB_PORT=8765 python server.py
```

Only expose the application on a trusted network or behind an appropriate authenticated reverse proxy. Codex Web can initiate code and infrastructure actions on behalf of its users.

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

The container healthcheck uses:

```text
GET /api/livez
```

Deeper Codex daemon health is available from:

```text
GET /api/healthz
```

See [DOCKER.md](DOCKER.md) for workspace migration, UID/GID mapping, persistence, secrets, direct `docker run` usage, and Codex CLI pinning.

## Executive providers

Executive reasoning can use a provider independently of the model/authentication used by native Codex execution.

Examples:

```bash
# OpenAI
export CODEX_WEB_EXECUTIVE_PROVIDER=openai
export OPENAI_API_KEY='...'

# Ollama / local OpenAI-compatible API
export CODEX_WEB_EXECUTIVE_PROVIDER=ollama
export CODEX_WEB_EXECUTIVE_BASE_URL='http://127.0.0.1:11434/v1'

# Other OpenAI-compatible provider
export CODEX_WEB_EXECUTIVE_PROVIDER=openai-compatible
export CODEX_WEB_EXECUTIVE_BASE_URL='http://llm-host:8000/v1'
```

See [EXECUTIVE.md](EXECUTIVE.md) for models, configuration variables, data-boundary behavior, roles, delegation, and API details.

## API and operations

FastAPI exposes the application API and OpenAPI documentation.

Important operational surfaces include:

- liveness and Codex daemon health;
- project and thread management;
- thread context/compaction;
- work-item lifecycle, execution metadata, checkpoints, history, and usage;
- Executive agents, runtime, context, chat, and delegation;
- bot bindings and inbound routing;
- GitLab Support ServiceDesk sweep operations.

The browser UI and integrations should consume the same canonical APIs and state used by runtime behavior; UI-only policy or execution truth is explicitly discouraged.

## Security model

Codex Web is powerful enough to modify repositories and invoke host/container tooling, so deployment security is part of the product architecture.

Current expectations include:

- run locally, on a trusted network, or behind an authenticated reverse proxy;
- fail closed for webhook verification when secrets are not configured;
- preserve Codex sandbox and approval controls during Executive delegation;
- keep raw secrets out of prompts, ordinary state, logs, and reusable definitions;
- treat repository content, webhook payloads, logs, provider responses, tool output, extension output, worker output, and model output as untrusted data;
- do not infer authority from model output or external content.

The roadmap strengthens these guarantees with canonical identity, tenant isolation, credential brokering, key management, provider-neutral action intents, isolated workers, evidence, data governance, compatibility contracts, and production qualification.

## Development and delivery

Before substantial work, read:

- [AGENTS.md](AGENTS.md)
- [Architecture documentation](docs/architecture/README.md)
- [Token Efficiency Ruleset](docs/architecture/token-efficiency-rules.md)
- [EXECUTIVE.md](EXECUTIVE.md) when changing Executive behavior

Delivery status belongs in GitHub, not in repository checkbox roadmaps.

The validation model is tiered:

- focused tests during development;
- the full Python suite and relevant browser specs before meaningful PR updates when practical;
- Docker validation when runtime/container changes require it;
- CI as the authoritative clean-environment gate.

Current CI is organized around independent unit/static, Chromium, and Docker smoke validation.

## Project tracking

Architecture documents define durable technical truth. They intentionally do not duplicate completion status.

Use:

- **GitHub Milestones** for delivery phases and dependency order;
- **GitHub Issues** for independently completable work packages and acceptance criteria;
- **GitHub Project** for status, priority, risk, area, dependencies, and cross-milestone visibility;
- **Pull Requests** for implementation and validation evidence.

See tracking issue `#126` and [the architecture index](docs/architecture/README.md) for the M1–M12 structure.

---

Codex Web should remain useful as an interactive coding interface at every stage of this evolution. The autonomous-company layers are expected to **reuse and strengthen** the existing execution, approval, work-item, state, and integration primitives—not replace them with a second system.
