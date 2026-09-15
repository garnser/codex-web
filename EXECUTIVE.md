# Executive Control Plane

This feature integrates the SaaS-oriented Executive layer derived from OpenExecutive into codex-web as part of the existing application.

- **Executive reasoning**: strategy, product, engineering, revenue, finance, customer success, security, and multi-specialist Board reviews.
- **Codex execution**: repository and infrastructure work using codex-web's existing native threads, queues, sandbox modes, browser approvals, and operational integrations.
- **One frontend**: an **Executive** button is added to the existing codex-web top bar. It opens a drawer inside the same UI; there is no separate executive application or route.
- **One server entrypoint**: continue to start codex-web with `server.py`. The root module is now a thin composition/compatibility layer; the existing backend implementation lives in `codex_web/application.py` and the Executive feature is attached as a router/service module.

## Run

Install the updated requirements and start the normal server entrypoint:

```bash
cd /home/nbingester/codex-web
. .venv/bin/activate
pip install -r requirements.txt
python server.py
```

The systemd unit continues to start `server.py` using the project virtual environment.

## LLM providers

### OpenAI

```bash
export CODEX_WEB_EXECUTIVE_PROVIDER=openai
export OPENAI_API_KEY='...'
export CODEX_WEB_EXECUTIVE_MODEL='gpt-5.6-terra'
python server.py
```

Native OpenAI uses the Responses API.

### Ollama on the same machine

Ollama exposes an OpenAI-compatible API. The integration uses Chat Completions for local/OpenAI-compatible providers so it works with a broader range of local servers.

```bash
ollama pull gpt-oss:20b
export CODEX_WEB_EXECUTIVE_PROVIDER=ollama
export CODEX_WEB_EXECUTIVE_MODEL='gpt-oss:20b'
export CODEX_WEB_EXECUTIVE_BASE_URL='http://127.0.0.1:11434/v1'
python server.py
```

No real API key is required for Ollama; the OpenAI SDK is given a dummy local key automatically when none is configured.

### Ollama running on a Mac while codex-web runs elsewhere

Point codex-web at the Mac's reachable address:

```bash
export CODEX_WEB_EXECUTIVE_PROVIDER=ollama
export CODEX_WEB_EXECUTIVE_MODEL='gpt-oss:20b'
export CODEX_WEB_EXECUTIVE_BASE_URL='http://MAC-IP-OR-DNS:11434/v1'
```

Ollama normally needs to be configured to listen on an address reachable from the codex-web host. Prefer a private LAN/VPN/Tailscale address or an SSH tunnel rather than exposing port 11434 to the public Internet.

### Other OpenAI-compatible servers

```bash
export CODEX_WEB_EXECUTIVE_PROVIDER=openai-compatible
export CODEX_WEB_EXECUTIVE_BASE_URL='http://llm-host:8000/v1'
export CODEX_WEB_EXECUTIVE_MODEL='your-model-name'
# Optional if your server requires a key:
export OPENAI_API_KEY='...'
```

This path works with servers that implement the OpenAI Chat Completions API. If a compatible server rejects `reasoning_effort`, codex-web automatically retries without it.

## Configuration

- `CODEX_WEB_EXECUTIVE_PROVIDER` — `openai`, `ollama`, or `openai-compatible`; default `openai`.
- `CODEX_WEB_EXECUTIVE_MODEL` — default `gpt-5.6-terra` for OpenAI and `gpt-oss:20b` for Ollama.
- `CODEX_WEB_EXECUTIVE_BASE_URL` — OpenAI-compatible `/v1` base URL; Ollama defaults to `http://127.0.0.1:11434/v1`.
- `CODEX_WEB_EXECUTIVE_API_KEY_ENV` — environment-variable name containing the provider key; default `OPENAI_API_KEY`.
- `CODEX_WEB_EXECUTIVE_REASONING_EFFORT` — default `medium`.
- `CODEX_WEB_EXECUTIVE_TEXT_VERBOSITY` — default `medium` for native OpenAI.
- `CODEX_WEB_EXECUTIVE_BOARD_SPECIALISTS` — default `3`, clamped to 2–5.
- `CODEX_WEB_EXECUTIVE_MAX_HISTORY` — default `24` messages per executive session.

Company context and executive-to-Codex thread mappings are stored under the existing codex-web `data/` directory.

## Application layout

The Executive integration deliberately does not introduce a second FastAPI application.

```text
server.py                         thin launcher/composition layer
codex_web/application.py          existing codex-web application/runtime
codex_web/executive.py            Executive domain models, routing and service logic
codex_web/executive_integration.py provider integration + FastAPI router registration
static/executive-ui.js             Executive drawer UI
```

This is the first decomposition step for the legacy monolithic server. Further routers/services can move out of `codex_web/application.py` incrementally without changing the deployment entrypoint or the historical `import server` compatibility used by tests and integrations.

## Single-UI workflow

1. Open the normal codex-web page.
2. Select the project you are working on as usual.
3. Click **Executive** in the existing top bar.
4. Choose an executive or switch to **Board review**.
5. Ask the business/product/engineering question.
6. Use **Delegate to Codex** on an executive response to turn the recommendation into implementation work.

The drawer picks the currently active codex-web project when it opens. Delegation also reads the existing codex-web sandbox and approval-policy selectors, so the executive layer cannot silently bypass execution controls.

## Data boundary

The executive drawer does **not** send codex-web operational context to the configured LLM unless **Include Codex operational context in LLM request** is checked. When enabled, it sends a compact summary containing project names, active/queued turn counts, and recent open work-item metadata. It does not automatically attach source files or repository contents.

With Ollama/local providers, executive prompts remain on the configured local/private LLM endpoint. Codex itself continues to use whatever model/authentication its native `codex app-server` configuration uses; the Executive provider and Codex execution provider are intentionally independent.

## Delegation model

Each executive role gets a reusable Codex thread per codex-web project. Delegation sets role-specific developer instructions and enters the task through codex-web's existing `start_turn` path. This preserves queueing, recovery, sandbox and approval behavior.

The executive roles are AI Chief of Staff, CTO / Principal Architect, VP Engineering, Chief Product Officer, Chief Revenue Officer, CMO / Growth, CFO / SaaS FinOps, Head of Customer Success, and Security & Compliance Lead.

Board mode asks several relevant specialists in parallel and has the Chief of Staff synthesize one decision and action plan.

## API

- `GET /api/executive/agents`
- `GET|POST /api/executive/context`
- `GET /api/executive/runtime`
- `POST /api/executive/chat`
- `POST /api/executive/delegate`

FastAPI exposes these in the existing `/docs` OpenAPI UI.
