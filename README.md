# Codex Web Local

A small local browser client for `codex app-server`.

It supports:

- Projects as named workspace roots.
- Native Codex app-server threads.
- Thread list, create, resume, read, archive, and unarchive.
- Streaming turn events.
- Browser approval prompts for command and file-change requests.
- Scaffolded Slack and Telegram inbound bot webhooks that map external
  conversations to Codex threads.

Run it:

```bash
cd /home/nbingester/codex-web
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python server.py
```

The server binds to `127.0.0.1:8765` by default. Override with:

```bash
CODEX_WEB_HOST=0.0.0.0 CODEX_WEB_PORT=8765 python server.py
```

Only expose it on a trusted network. This interface can drive Codex actions on the host.

When proxied below `/codex/`, the frontend automatically prefixes API and
WebSocket calls with `/codex`.

## Bot scaffold

The bot bridge is provider-neutral at the core: an external conversation is
bound to one Codex thread, and inbound messages start turns on that thread.
One external conversation can have multiple Codex thread bindings. When there
are multiple bindings, inbound messages must start with the thread prefix:

- `Thread name: message`
- `Thread name - message`
- `[Thread name] message`
- `@Thread name message`

Outbound-ready events are recorded with the same `Thread name: ...` prefix in
`data/bot_events.jsonl`. Provider posting is still an adapter step.

Inspection and local testing endpoints:

```bash
curl http://127.0.0.1:8765/api/bots
curl http://127.0.0.1:8765/api/bots/bindings
curl -X POST http://127.0.0.1:8765/api/bots/inbound \
  -H 'Content-Type: application/json' \
  -d '{"provider":"slack","external_conversation_id":"C123","text":"Ops: hello"}'
curl -X POST http://127.0.0.1:8765/api/bots/bindings \
  -H 'Content-Type: application/json' \
  -d '{"provider":"slack","external_conversation_id":"C123","thread_name":"Ops"}'
```

Thread rename endpoint:

```bash
curl -X POST http://127.0.0.1:8765/api/threads/THREAD_ID/name \
  -H 'Content-Type: application/json' \
  -d '{"name":"Ops"}'
```

Provider webhook endpoints:

- Slack Events API: `POST /bots/slack/events`
- Telegram Bot API webhook: `POST /bots/telegram/webhook`
- GitLab project/group webhooks: `POST /bots/gitlab/events`

Optional verification environment variables:

- `SLACK_SIGNING_SECRET` verifies Slack request signatures.
- `TELEGRAM_WEBHOOK_SECRET` verifies Telegram's
  `X-Telegram-Bot-Api-Secret-Token` header.
- `CODEX_WEB_GITLAB_WEBHOOK_SECRET` or `GITLAB_WEBHOOK_SECRET` verifies
  GitLab's `X-Gitlab-Token` header.

GitLab events are routed to agent threads from `owner::<agent>` labels. Merge
request and pipeline events without an owner label go to Quinn. Configure
channel preference overrides with `CODEX_WEB_AGENT_CHANNELS`, either as JSON
(`{"dana":"C0B9C6MGZ5X"}`) or comma pairs (`dana:C0B9C6MGZ5X,james:C0B9591ESTB`).

Important: `/codex` is currently nginx-allowlisted to local networks. Real
Slack and Telegram webhooks need either a separate public nginx location for
the two webhook paths or an external relay that can reach this host. Outbound
posting back to Slack/Telegram is intentionally left as the next adapter step;
the current scaffold creates/resumes Codex threads and starts turns from inbound
messages.
