# Install and start codex-web

> Applies to: current main  
> Audience: new users and administrators  
> Risk: local application startup

## Goal

Run codex-web locally and prove both HTTP liveness and Codex app-server readiness.

## Prerequisites

Choose one path:

- **Docker Compose:** Docker with Compose support.
- **Native Python:** Python matching the supported project runtime plus the Codex CLI available to the application.

You also need at least one repository directory that codex-web may access.

## Docker Compose

From the repository root:

```bash
cp .env.example .env
mkdir -p workspace
docker compose build
docker compose run --rm codex-web codex login
docker compose up -d
```

Open `http://127.0.0.1:8765`.

### Verify

```bash
curl -fsS http://127.0.0.1:8765/api/livez
curl -fsS http://127.0.0.1:8765/api/healthz
```

`/api/livez` proves the web process is alive. `/api/healthz` is the deeper Codex daemon/readiness check and may return 503 when the Codex app-server is unavailable.

For workspace mounts, UID/GID mapping, persistence and direct Docker usage, see [Docker deployment](../../DOCKER.md).

## Native Python

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python server.py
```

Open `http://127.0.0.1:8765` and run the same liveness/readiness checks above.

## Expected result

The browser loads, `/api/livez` succeeds, and `/api/healthz` reports a healthy Codex daemon.

## Failure modes

| Symptom | Likely cause | Recovery |
| --- | --- | --- |
| `/api/livez` fails | web process/container is not running | inspect process or `docker compose logs codex-web` |
| `/api/livez` passes but `/api/healthz` returns 503 | Codex app-server/authentication problem | confirm `codex login`, runtime version and logs |
| project path is rejected in Docker | path is outside the mounted workspace | set `CODEX_WORKSPACE` / workspace root correctly |
| webhook endpoint returns 503 | verification secret is intentionally fail-closed | configure the integration secret or leave the integration unused |

## Security

Keep the default loopback bind for the first run. Codex-web can execute code and later invoke external provider actions, so network exposure is a security decision rather than a convenience setting.

## Next

Continue to [First run](first-run.md).
