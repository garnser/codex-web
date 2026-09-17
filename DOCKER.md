# Docker deployment

The container image is self-contained: it runs codex-web and installs the Codex CLI directly from the official `@openai/codex` npm package during `docker build`. The host only needs Docker/Compose; it does not need Codex, Node.js, npm, or Python installed.

The container uses a non-root `codex` user, persists application state under `/app/data`, stores Codex CLI state under `/home/codex/.codex`, and expects developer repositories to be mounted below `/workspace`.

## Start with Docker Compose

```bash
cp .env.example .env
mkdir -p workspace
docker compose build
docker compose run --rm codex-web codex login
docker compose up -d
```

The UI is exposed on `127.0.0.1:8765` by default. Set `CODEX_WEB_BIND=0.0.0.0` only when a reverse proxy or trusted network boundary protects the service.

The image has a Docker healthcheck against `/api/livez`. `/api/healthz` remains the deeper readiness/daemon-health endpoint and can return 503 when the Codex app-server is unavailable.

## Workspace mounts and project paths

Set `CODEX_WORKSPACE` to the host directory that contains the repositories Codex should be allowed to work with:

```bash
CODEX_WORKSPACE=/srv/development docker compose up -d
```

Inside the container that directory is `/workspace`. Compose sets `CODEX_WEB_WORKSPACE_ROOT=/workspace`, so project records are stored as portable paths relative to that root. For example, a project entered as `product` is persisted as `product` while Codex receives `/workspace/product` as its runtime working directory. Nested paths such as `customer/api` work the same way.

When `CODEX_WORKSPACE` is an absolute host path, Compose also supplies it as `CODEX_WEB_WORKSPACE_SOURCE_ROOT`. Existing project records below that old/native prefix are migrated automatically. A stored `/srv/development/product`, for example, resolves to `/workspace/product` and is rewritten as the portable value `product` on load.

If an older project record contains an absolute path outside both `/workspace` and the configured source root, codex-web rejects it instead of guessing by basename. Set `CODEX_WEB_WORKSPACE_SOURCE_ROOT` to the previous common workspace directory, or update the project record explicitly. This keeps project paths from escaping the mounted workspace into `/app`, `/etc`, or another container filesystem location.

For a native/non-Compose deployment you can opt into the same behavior by setting an absolute `CODEX_WEB_WORKSPACE_ROOT`. If it is unset, codex-web retains the historical native behavior and stores normal absolute project paths.

The container runs as UID/GID 1000 by default. If the mounted workspace uses another owner, set `CODEX_UID` and `CODEX_GID` before building:

```bash
CODEX_UID=$(id -u) CODEX_GID=$(id -g) docker compose build
```

## Persistent data

Compose creates two named volumes:

- `codex-web-data` -> `/app/data` for the SQLite runtime database plus compatibility JSON/JSONL state.
- `codex-home` -> `/home/codex/.codex` for Codex CLI authentication and configuration.

Runtime state is stored in `/app/data/codex-web.db` using SQLite WAL mode. During the migration window, SQLite-backed state is also mirrored to legacy JSON files on every write. This keeps rollback to an earlier release safe while SQLite is introduced incrementally.

Back up the entire `codex-web-data` volume before destructive upgrades or storage migrations. A consistent backup should include `codex-web.db` together with its `-wal`/`-shm` files when the service is running, or be taken while the service is stopped.

## Secrets and webhooks

Copy `.env.example` to `.env` and populate only the integrations in use. Slack, Telegram, and GitLab webhook verification fail closed: an inbound request to an enabled webhook route without a configured verification secret receives HTTP 503.

Do not bake secrets into the image. For production, inject them through the orchestrator, a secrets manager, or an environment file with restricted permissions.

## Reproducible runtime versions

The repository separates human-maintained compatibility constraints from the exact runtime set:

- `requirements.txt` declares supported top-level Python dependency ranges.
- `requirements.lock` contains the complete Python runtime versions validated by CI and used by Docker.

Docker also defaults `CODEX_VERSION` to the stable Codex CLI version validated by CI for this repository revision. To intentionally test or upgrade Codex, override it explicitly:

```bash
CODEX_VERSION=<version> docker compose build
```

The version is installed inside the image with:

```bash
npm install -g "@openai/codex@${CODEX_VERSION}"
```

CI checks the installed `codex --version` against `.env.example`, so an accidental `latest` drift or broken installation fails before merge.

For a reproducible local Python environment use:

```bash
python -m pip install -r requirements.lock
```

## Direct Docker usage

```bash
docker build -t codex-web .
docker run --rm \
  -p 127.0.0.1:8765:8765 \
  -e CODEX_WEB_WORKSPACE_ROOT=/workspace \
  -v codex-web-data:/app/data \
  -v codex-home:/home/codex/.codex \
  -v "$PWD/workspace:/workspace" \
  codex-web
```

The container deliberately does not mount the Docker socket, SSH keys, or arbitrary host directories. Add only the credentials and mounts required for the repositories and integrations you intend Codex to access.
