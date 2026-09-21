# Install and start Codex Web

> Applies to: current main  
> Audience: new users and administrators  
> Risk: local application startup

## Goal

Start Codex Web and verify the first two readiness layers: process liveness and application/runtime readiness. Project execution readiness is verified only after a Project exists.

## Prerequisites

Choose one path:

- **Docker Compose:** Docker with Compose support.
- **Native Python:** Python matching the supported project runtime plus the Codex CLI available to the application.

You also need at least one Git repository beneath the workspace root you intend Codex Web to access.

## Docker Compose

From the repository root:

```bash
cp .env.example .env
mkdir -p workspace
docker compose build
docker compose run --rm codex-web codex login
docker compose up -d
```

Place or mount the repository beneath the configured workspace root (Compose uses `/workspace`).

Open `http://127.0.0.1:8765`.

### Verify process and application readiness

```bash
curl -fsS http://127.0.0.1:8765/api/livez
curl -fsS http://127.0.0.1:8765/api/readyz
```

**Expected result:** both requests succeed. `/api/livez` proves only that the web process is alive; `/api/readyz` verifies application/runtime readiness and canonical state-store health.

`/api/healthz` is useful for deeper component/Codex-daemon diagnostics, but it is not a Project execution-readiness check.

For workspace mounts, UID/GID mapping, persistence and direct Docker usage, see [Docker deployment](../../DOCKER.md).

## Native Python

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
codex login
python server.py
```

If Codex authentication is already configured for the account running the service, the login step can be skipped.

Open `http://127.0.0.1:8765` and run the same `/api/livez` and `/api/readyz` checks.

## Failure modes

| Symptom | Likely cause | Recovery |
| --- | --- | --- |
| `/api/livez` fails | web process/container is not running | inspect the process or `docker compose logs codex-web` |
| `/api/livez` passes but `/api/readyz` fails | application/runtime/state-store prerequisite is unavailable | inspect `/api/healthz`, logs and runtime/storage configuration |
| Codex runtime reports authentication failure | Codex CLI is not authenticated for the service account | run `codex login` in the same native/container identity |
| repository path is rejected later | path is outside the approved workspace root | fix `CODEX_WORKSPACE` / `CODEX_WEB_WORKSPACE_ROOT`; do not mount the entire host |
| webhook endpoint returns 503 | verification secret is intentionally fail-closed | configure that integration or leave it unused |

## Security

Keep the default loopback bind for the first run. Codex Web can execute code and later invoke external provider actions, so network exposure is a security decision rather than a convenience setting.

## Next

Continue to [First run](first-run.md), where the first Project is bootstrapped and its execution readiness is verified.
